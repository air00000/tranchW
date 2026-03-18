from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from .models import EvaluationResult, Penalty, Snapshot


Number = int | float


@dataclass(slots=True)
class TransformSpec:
    type: str
    a: float
    b: float
    m: float | None = None


class RuleEvaluator:
    def __init__(self, ruleset: dict[str, Any]) -> None:
        self.ruleset = ruleset
        self.aliases: dict[str, str] = ruleset.get("metric_aliases", {})
        self.op_policy = ruleset["operational_policy"]
        self.hard_veto_rules = ruleset["hard_veto"]["rules"]
        self.prefilter_rules = ruleset["candidate_prefilters"]["rules"]
        self.score_model = ruleset["score_model"]
        self.score_threshold = float(self.score_model["candidate_score_threshold"])
        self.completeness_min = float(self.op_policy.get("score_metrics_completeness_min", 0.9))
        self.require_all_hard_veto_metrics_present = bool(self.op_policy.get("require_all_hard_veto_metrics_present", True))
        self.data_staleness_max_sec = float(self.op_policy.get("data_staleness_max_sec", 10))

        self.hard_veto_metrics = {self._canonical(rule["metric"]) for rule in self.hard_veto_rules}
        self.score_metrics = {
            self._canonical(component["metric"])
            for block in self.score_model["blocks"].values()
            for component in block["components"]
        }
        self.prefilter_metrics = {self._canonical(rule["metric"]) for rule in self.prefilter_rules}

    def _canonical(self, metric: str) -> str:
        return self.aliases.get(metric, metric)

    def _get_raw(self, metrics: dict[str, Any], metric: str) -> Any:
        metric = self._canonical(metric)
        if metric in metrics:
            return metrics[metric]
        for alias, target in self.aliases.items():
            if target == metric and alias in metrics:
                return metrics[alias]
        return None

    def resolve_metrics(self, snapshot: Snapshot) -> dict[str, Any]:
        raw = dict(snapshot.metrics)
        resolved = dict(raw)

        def set_if_missing(name: str, value: Any) -> None:
            if value is not None and name not in resolved:
                resolved[name] = value

        liq_usd = self._as_number(self._get_raw(raw, "liq_usd"))
        fdv_usd = self._as_number(self._get_raw(raw, "fdv_usd"))
        if liq_usd is not None and fdv_usd not in (None, 0):
            set_if_missing("liq_to_fdv", liq_usd / fdv_usd)

        token_age_minutes = self._as_number(self._get_raw(raw, "token_age_minutes"))
        volume_usd_effective = self._as_number(self._get_raw(raw, "volume_usd_effective"))
        if volume_usd_effective is not None and token_age_minutes is not None and token_age_minutes > 0:
            effective_window_minutes = min(token_age_minutes, 15)
            if effective_window_minutes > 0:
                set_if_missing("volume_equiv_15m_usd", volume_usd_effective * 15 / effective_window_minutes)

        unique_buyers_5m = self._as_number(self._get_raw(raw, "unique_buyers_5m"))
        unique_sellers_5m = self._as_number(self._get_raw(raw, "unique_sellers_5m"))
        if unique_buyers_5m is not None and unique_sellers_5m is not None:
            set_if_missing("buyers_to_sellers_5m", unique_buyers_5m / max(unique_sellers_5m, 1))

        buy_volume_5m = self._as_number(self._get_raw(raw, "buy_volume_usd_5m"))
        sell_volume_5m = self._as_number(self._get_raw(raw, "sell_volume_usd_5m"))
        if buy_volume_5m is not None and sell_volume_5m is not None:
            set_if_missing("buy_to_sell_volume_5m", buy_volume_5m / max(sell_volume_5m, 1))

        current_price = self._as_number(self._get_raw(raw, "current_price"))
        vwap_5m = self._as_number(self._get_raw(raw, "vwap_5m"))
        if current_price is not None and vwap_5m is not None:
            set_if_missing("must_be_above_vwap_5m", current_price > vwap_5m)

        rolling_5m_high_close = self._as_number(self._get_raw(raw, "rolling_5m_high_close"))
        current_close = self._as_number(self._get_raw(raw, "current_close"))
        if rolling_5m_high_close not in (None, 0) and current_close is not None:
            set_if_missing(
                "pullback_from_high_5m_pct",
                (rolling_5m_high_close - current_close) / rolling_5m_high_close * 100,
            )

        candle_1m_high = self._as_number(self._get_raw(raw, "candle_1m_high"))
        candle_1m_open = self._as_number(self._get_raw(raw, "candle_1m_open"))
        if candle_1m_high is not None and candle_1m_open not in (None, 0):
            set_if_missing("blowoff_1m_pct", (candle_1m_high - candle_1m_open) / candle_1m_open * 100)

        trades_5m = self._as_number(self._get_raw(raw, "trades_5m"))
        if trades_5m is not None:
            set_if_missing("trades_per_min_5m", trades_5m / 5)

        if current_close is not None:
            set_if_missing("current_1m_close", current_close)
        if current_price is not None and "current_1m_close" not in resolved:
            set_if_missing("current_1m_close", current_price)

        prev5_avg = self._as_number(self._get_raw(raw, "previous_5_completed_1m_volume_usd_avg"))
        prev5_list = self._get_raw(raw, "previous_5_completed_1m_volume_usd")
        if prev5_avg is None and isinstance(prev5_list, list):
            numeric = [self._as_number(v) for v in prev5_list]
            numeric = [v for v in numeric if v is not None]
            if numeric:
                set_if_missing("previous_5_completed_1m_volume_usd_avg", sum(numeric) / len(numeric))

        if snapshot.data_age_sec is not None:
            set_if_missing("data_age_sec", float(snapshot.data_age_sec))

        return resolved

    def evaluate(self, snapshot: Snapshot) -> EvaluationResult:
        metrics = self.resolve_metrics(snapshot)
        hard_veto_pass, failed_veto_rules, hard_veto_missing = self._evaluate_hard_veto(metrics)
        candidate_prefilters_pass, failed_prefilters = self._evaluate_prefilters(metrics)
        completeness = self._score_completeness(metrics)
        subscores = self._compute_subscores(metrics)
        score_base = self._compute_score_base(subscores)
        penalties = self._evaluate_penalties(metrics)
        score_final = self._clamp(score_base + sum(p.delta for p in penalties), 0.0, 1.0)
        coordinated_related_selling = self._evaluate_immediate_reject(metrics)

        reason_codes: list[str] = []
        if hard_veto_missing:
            reason_codes.append(self.ruleset["missing_data_policy"]["missing_hard_veto_reason_code"])
        reason_codes.extend(failed_veto_rules)
        if completeness < self.completeness_min:
            reason_codes.append(self.ruleset["missing_data_policy"]["missing_score_reason_code"])
        if failed_prefilters:
            reason_codes.append("CANDIDATE_PREFILTER_FAIL")
        if candidate_prefilters_pass and hard_veto_pass and completeness >= self.completeness_min and score_final >= self.score_threshold:
            reason_codes.append("CANDIDATE_SCORE_PASS")
        reason_codes.extend([pen.code for pen in penalties])
        if coordinated_related_selling:
            reason_codes.append("COORDINATED_RELATED_SELLING")

        # de-duplicate while preserving order
        unique_reason_codes: list[str] = []
        seen: set[str] = set()
        for code in reason_codes:
            if code not in seen:
                seen.add(code)
                unique_reason_codes.append(code)

        return EvaluationResult(
            resolved_metrics=metrics,
            hard_veto_pass=hard_veto_pass,
            failed_veto_rules=failed_veto_rules,
            hard_veto_missing_metrics=hard_veto_missing,
            candidate_prefilters_pass=candidate_prefilters_pass,
            failed_prefilters=failed_prefilters,
            score_metrics_completeness=completeness,
            penalties=penalties,
            score_base=score_base,
            score_final=score_final,
            subscores=subscores,
            reason_codes=unique_reason_codes,
            coordinated_related_selling=coordinated_related_selling,
        )

    def _evaluate_hard_veto(self, metrics: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
        failed: list[str] = []
        missing: list[str] = []
        for rule in self.hard_veto_rules:
            metric = self._canonical(rule["metric"])
            value = metrics.get(metric)
            if value is None:
                missing.append(metric)
                continue
            if not self._compare(value, rule["op"], rule["value"]):
                failed.append(rule["code"])
        if self.require_all_hard_veto_metrics_present and missing:
            return False, failed, missing
        return (not failed) if not self.require_all_hard_veto_metrics_present else (not failed and not missing), failed, missing

    def _evaluate_prefilters(self, metrics: dict[str, Any]) -> tuple[bool, list[str]]:
        failed: list[str] = []
        for rule in self.prefilter_rules:
            metric = self._canonical(rule["metric"])
            value = metrics.get(metric)
            if value is None or not self._compare(value, rule["op"], rule["value"]):
                failed.append(metric)
        return not failed, failed

    def _score_completeness(self, metrics: dict[str, Any]) -> float:
        present = 0
        total = max(len(self.score_metrics), 1)
        for metric in self.score_metrics:
            if metrics.get(metric) is not None:
                present += 1
        return present / total

    def _compute_subscores(self, metrics: dict[str, Any]) -> dict[str, float]:
        subscores: dict[str, float] = {}
        for block_name, block in self.score_model["blocks"].items():
            weighted_sum = 0.0
            weight_sum = 0.0
            for component in block["components"]:
                metric_name = self._canonical(component["metric"])
                value = metrics.get(metric_name)
                if value is None:
                    continue
                transform = component["transform"]
                transformed = self._transform(
                    value=float(value),
                    spec=TransformSpec(
                        type=transform["type"],
                        a=float(transform["a"]),
                        b=float(transform["b"]),
                        m=(float(transform["m"]) if transform.get("m") is not None else None),
                    ),
                )
                weight = float(component["weight"])
                weighted_sum += transformed * weight
                weight_sum += weight
            subscores[block_name.lower()] = (weighted_sum / weight_sum) if weight_sum > 0 else 0.0
        return subscores

    def _compute_score_base(self, subscores: dict[str, float]) -> float:
        weights = self.score_model["block_weights"]
        mapping = {
            "Momentum": subscores.get("momentum", 0.0),
            "Flow": subscores.get("flow", 0.0),
            "Liquidity": subscores.get("liquidity", 0.0),
            "SmartMoney": subscores.get("smartmoney", 0.0),
        }
        total = 0.0
        for block_name, value in mapping.items():
            total += float(weights.get(block_name, 0.0)) * value
        return self._clamp(total, 0.0, 1.0)

    def _evaluate_penalties(self, metrics: dict[str, Any]) -> list[Penalty]:
        penalties: list[Penalty] = []
        params = self.ruleset["anti_cabal_anti_sybil"]["params"]

        young_wallet_share = self._as_number(metrics.get("young_wallet_buy_volume_share_5m_pct"))
        buy_size_cv_5m = self._as_number(metrics.get("buy_size_cv_5m"))
        if (
            young_wallet_share is not None
            and buy_size_cv_5m is not None
            and young_wallet_share >= float(params["sybil_buy_share_5m_min_pct"])
            and buy_size_cv_5m <= float(params["sybil_buy_size_cv_5m_max"])
        ):
            penalties.append(Penalty(code="SYBIL_PATTERN", delta=-0.12))

        same_funder_smart_count = self._as_number(metrics.get("smart_wallets_same_funder_cluster_count"))
        if same_funder_smart_count is not None and same_funder_smart_count >= float(params["smart_same_funder_cluster_wallets_min"]):
            penalties.append(Penalty(code="SMART_CLUSTER_OVERLAP", delta=-0.15))

        return penalties

    def _evaluate_immediate_reject(self, metrics: dict[str, Any]) -> bool:
        params = self.ruleset["anti_cabal_anti_sybil"]["params"]
        related_wallet_count = self._as_number(metrics.get("related_wallet_count_2m"))
        combined_sell_usd = self._as_number(metrics.get("related_wallet_combined_sell_usd_2m"))
        candidate_buy_vol_usd = self._as_number(metrics.get("buy_usd_volume_during_candidate_window"))

        if related_wallet_count is None or combined_sell_usd is None or candidate_buy_vol_usd in (None, 0):
            return False
        sell_share_pct = combined_sell_usd / candidate_buy_vol_usd * 100
        return (
            related_wallet_count >= float(params["related_sell_cluster_min_wallets"])
            and combined_sell_usd >= float(params["related_sell_combined_usd_min"])
            and sell_share_pct >= float(params["related_sell_vs_candidate_buy_vol_min_pct"])
        )

    @staticmethod
    def _as_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            number = float(value)
            return number if isfinite(number) else None
        return None

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _transform(self, value: float, spec: TransformSpec) -> float:
        if spec.type == "linear":
            return self._linear(value, spec.a, spec.b)
        if spec.type == "inverse_linear":
            return 1.0 - self._linear(value, spec.a, spec.b)
        if spec.type == "triangular":
            if spec.m is None:
                raise ValueError("Triangular transform requires m")
            return self._triangular(value, spec.a, spec.m, spec.b)
        raise ValueError(f"Unknown transform type: {spec.type}")

    def _linear(self, x: float, a: float, b: float) -> float:
        if a == b:
            return 1.0 if x >= b else 0.0
        return self._clamp((x - a) / (b - a), 0.0, 1.0)

    def _triangular(self, x: float, a: float, m: float, b: float) -> float:
        if x <= a or x >= b:
            return 0.0
        if a < x < m:
            return (x - a) / (m - a)
        if m <= x < b:
            return (b - x) / (b - m)
        return 0.0

    def _compare(self, lhs: Any, op: str, rhs: Any) -> bool:
        if op == "==":
            return lhs == rhs
        lhs_num = self._as_number(lhs)
        rhs_num = self._as_number(rhs)
        if lhs_num is None or rhs_num is None:
            return False
        if op == ">=":
            return lhs_num >= rhs_num
        if op == "<=":
            return lhs_num <= rhs_num
        if op == ">":
            return lhs_num > rhs_num
        if op == "<":
            return lhs_num < rhs_num
        raise ValueError(f"Unsupported comparison operator: {op}")
