from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .evaluator import RuleEvaluator
from .models import AlertEvent, EvaluationResult, RuntimeOptions, Snapshot, TokenState
from .state_store import SqliteStateStore


class SolRunnerAlertBot:
    def __init__(
        self,
        ruleset: dict[str, Any],
        store: SqliteStateStore,
        runtime: RuntimeOptions | None = None,
    ) -> None:
        self.ruleset = ruleset
        self.evaluator = RuleEvaluator(ruleset)
        self.store = store
        self.runtime = runtime or RuntimeOptions()
        self.ruleset_version = str(ruleset["ruleset_version"])
        self.watch_ttl_ms = int(float(ruleset["operational_policy"]["candidate_watch_ttl_min"]) * 60_000)
        self.cooldown_ms = int(float(ruleset["operational_policy"]["cooldown_min"]) * 60_000)
        self.rearm_gain_pct = float(ruleset["operational_policy"]["rearm_new_high_over_last_alert_min_pct"])
        self.continuation_params = ruleset["continuation_trigger"]["params"]
        self.event_log_path = Path(self.runtime.write_all_events_jsonl) if self.runtime.write_all_events_jsonl else None
        if self.event_log_path:
            self.event_log_path.parent.mkdir(parents=True, exist_ok=True)

    def process_snapshot(self, snapshot: Snapshot) -> list[AlertEvent]:
        dedupe_key = self._dedupe_key(snapshot)
        state = self.store.get_state(dedupe_key) or TokenState(dedupe_key=dedupe_key, selected_pool=snapshot.pool)
        state.last_updated_ms = snapshot.ts_ms
        evaluation = self.evaluator.evaluate(snapshot)
        events: list[AlertEvent] = []

        if state.status == "REJECTED":
            self.store.save_state(state, snapshot.ts_ms)
            return events

        if state.status == "WATCH_CANDIDATE" and state.candidate_expires_at_ms and snapshot.ts_ms > state.candidate_expires_at_ms:
            state = TokenState(dedupe_key=dedupe_key, selected_pool=snapshot.pool, status="NEW", last_updated_ms=snapshot.ts_ms)

        if state.status == "COOLDOWN":
            self._maybe_rearm(state, evaluation)

        if state.status in {"NEW", "REARMED"}:
            events.extend(self._handle_new_or_rearmed(state, snapshot, evaluation))
        elif state.status == "WATCH_CANDIDATE":
            events.extend(self._handle_watch_candidate(state, snapshot, evaluation))

        self.store.save_state(state, snapshot.ts_ms)
        for event in events:
            self.store.record_event(dedupe_key, event)
            self._append_event_log(event)
        return [e for e in events if self._dispatch_enabled(e)]

    def _maybe_rearm(self, state: TokenState, evaluation: EvaluationResult) -> None:
        current_close = self._current_close(evaluation)
        if (
            state.cooldown_until_ms is not None
            and state.last_alert_price not in (None, 0)
            and state.last_updated_ms is not None
            and state.last_updated_ms >= state.cooldown_until_ms
            and current_close is not None
            and current_close >= state.last_alert_price * (1 + self.rearm_gain_pct / 100.0)
        ):
            state.status = "REARMED"

    def _handle_new_or_rearmed(
        self,
        state: TokenState,
        snapshot: Snapshot,
        evaluation: EvaluationResult,
    ) -> list[AlertEvent]:
        if not evaluation.hard_veto_pass:
            state.status = "REJECTED"
            return [
                self._build_event(
                    snapshot=snapshot,
                    evaluation=evaluation,
                    event="reject",
                    reason_codes=evaluation.reason_codes or ["HARD_VETO_DATA_MISSING"],
                    reason="hard veto failed",
                )
            ]
        if evaluation.score_metrics_completeness < self.evaluator.completeness_min:
            state.status = "REJECTED"
            reason_codes = list(evaluation.reason_codes)
            if "SCORE_METRICS_INCOMPLETE" not in reason_codes:
                reason_codes.append("SCORE_METRICS_INCOMPLETE")
            return [
                self._build_event(
                    snapshot=snapshot,
                    evaluation=evaluation,
                    event="reject",
                    reason_codes=reason_codes,
                    reason="score metrics completeness below threshold",
                )
            ]
        if (
            evaluation.candidate_prefilters_pass
            and evaluation.score_final >= self.evaluator.score_threshold
            and self._is_fresh(evaluation)
        ):
            state.status = "WATCH_CANDIDATE"
            state.selected_pool = snapshot.pool
            state.candidate_ts_ms = snapshot.ts_ms
            state.candidate_expires_at_ms = snapshot.ts_ms + self.watch_ttl_ms
            current_close = self._current_close(evaluation)
            state.local_high_ref = current_close
            state.frozen_local_high_ref = None
            state.pullback_started = False
            state.deepest_pullback_pct = 0.0
            return [
                self._build_event(
                    snapshot=snapshot,
                    evaluation=evaluation,
                    event="runner_candidate",
                    reason_codes=["CANDIDATE_SCORE_PASS"],
                    reason="candidate matched all entry conditions",
                )
            ]
        return []

    def _handle_watch_candidate(
        self,
        state: TokenState,
        snapshot: Snapshot,
        evaluation: EvaluationResult,
    ) -> list[AlertEvent]:
        if not evaluation.hard_veto_pass:
            state.status = "REJECTED"
            return [
                self._build_event(
                    snapshot=snapshot,
                    evaluation=evaluation,
                    event="reject",
                    reason_codes=evaluation.reason_codes or ["HARD_VETO_DATA_MISSING"],
                    reason="hard veto failed during watch",
                )
            ]
        if evaluation.coordinated_related_selling:
            state.status = "REJECTED"
            return [
                self._build_event(
                    snapshot=snapshot,
                    evaluation=evaluation,
                    event="reject",
                    reason_codes=["COORDINATED_RELATED_SELLING"],
                    reason="coordinated related selling detected",
                )
            ]

        current_close = self._current_close(evaluation)
        if current_close is None:
            return []

        if state.local_high_ref is None:
            state.local_high_ref = current_close

        if not state.pullback_started:
            if current_close > (state.local_high_ref or current_close):
                state.local_high_ref = current_close
            pullback_pct = self._pullback_pct(state.local_high_ref, current_close)
            if pullback_pct >= float(self.continuation_params["continuation_pullback_min_pct"]):
                state.pullback_started = True
                state.frozen_local_high_ref = state.local_high_ref
                state.deepest_pullback_pct = pullback_pct
            return []

        frozen_ref = state.frozen_local_high_ref or state.local_high_ref
        state.deepest_pullback_pct = max(state.deepest_pullback_pct, self._pullback_pct(frozen_ref, current_close))

        rebreak_ok = current_close > frozen_ref * (1 + float(self.continuation_params["continuation_rebreak_above_local_high_pct"]) / 100.0)
        pullback_window_ok = (
            state.deepest_pullback_pct >= float(self.continuation_params["continuation_pullback_min_pct"])
            and state.deepest_pullback_pct <= float(self.continuation_params["continuation_pullback_max_pct"])
        )
        current_1m_volume = self._to_float(evaluation.resolved_metrics.get("current_1m_volume_usd"))
        prev5_avg = self._to_float(evaluation.resolved_metrics.get("previous_5_completed_1m_volume_usd_avg"))
        vol_ok = (
            current_1m_volume is not None
            and prev5_avg not in (None, 0)
            and current_1m_volume >= prev5_avg * float(self.continuation_params["continuation_vol_1m_vs_prev5_min"])
        )
        current_1m_bsv = self._to_float(evaluation.resolved_metrics.get("current_1m_buy_to_sell_volume"))
        flow_ok = current_1m_bsv is not None and current_1m_bsv >= float(self.continuation_params["continuation_buy_sell_1m_min"])
        freshness_ok = self._is_fresh(evaluation)

        if rebreak_ok and pullback_window_ok and vol_ok and flow_ok and freshness_ok:
            state.status = "COOLDOWN"
            state.cooldown_until_ms = snapshot.ts_ms + self.cooldown_ms
            state.last_alert_ts_ms = snapshot.ts_ms
            state.last_alert_price = current_close
            state.local_high_ref = current_close
            state.frozen_local_high_ref = None
            state.pullback_started = False
            state.deepest_pullback_pct = 0.0
            return [
                self._build_event(
                    snapshot=snapshot,
                    evaluation=evaluation,
                    event="runner_alert",
                    reason_codes=["CONTINUATION_CONFIRMED"],
                    reason="continuation confirmed",
                )
            ]

        # If price reclaimed the prior high without confirmation, restart pullback tracking from the new close.
        if current_close > frozen_ref:
            state.local_high_ref = current_close
            state.frozen_local_high_ref = None
            state.pullback_started = False
            state.deepest_pullback_pct = 0.0
        return []

    def _build_event(
        self,
        snapshot: Snapshot,
        evaluation: EvaluationResult,
        event: str,
        reason_codes: list[str],
        reason: str,
    ) -> AlertEvent:
        subscores = {
            "momentum": round(evaluation.subscores.get("momentum", 0.0), 4),
            "flow": round(evaluation.subscores.get("flow", 0.0), 4),
            "liquidity": round(evaluation.subscores.get("liquidity", 0.0), 4),
            "smart_money": round(evaluation.subscores.get("smartmoney", 0.0), 4),
        }
        event_obj = AlertEvent(
            ts_ms=snapshot.ts_ms,
            slot=snapshot.slot,
            ruleset_version=self.ruleset_version,
            event=event,
            token=snapshot.token,
            symbol=snapshot.symbol,
            pool=snapshot.pool,
            score_base=round(evaluation.score_base, 4),
            score_final=round(evaluation.score_final, 4),
            subscores=subscores,
            penalties=[p.to_dict() for p in evaluation.penalties],
            hard_veto_pass=evaluation.hard_veto_pass,
            failed_veto_rules=evaluation.failed_veto_rules,
            reason_codes=reason_codes,
            metrics=evaluation.resolved_metrics,
            reason=reason,
        )
        return event_obj

    def _dispatch_enabled(self, event: AlertEvent) -> bool:
        if event.event == "reject":
            return self.runtime.dispatch_rejects
        if event.event == "runner_candidate":
            return self.runtime.dispatch_candidates
        return True

    def _append_event_log(self, event: AlertEvent) -> None:
        if not self.event_log_path:
            return
        with self.event_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def _dedupe_key(self, snapshot: Snapshot) -> str:
        return f"{snapshot.token}:{snapshot.pool}:{self.ruleset_version}"

    def _current_close(self, evaluation: EvaluationResult) -> float | None:
        return self._to_float(evaluation.resolved_metrics.get("current_1m_close"))

    @staticmethod
    def _pullback_pct(reference: float | None, current_close: float | None) -> float:
        if reference in (None, 0) or current_close is None:
            return 0.0
        return max((reference - current_close) / reference * 100.0, 0.0)

    def _is_fresh(self, evaluation: EvaluationResult) -> bool:
        age = evaluation.resolved_metrics.get("data_age_sec")
        age_f = self._to_float(age)
        if age_f is None:
            return True
        return age_f <= float(self.ruleset["operational_policy"]["data_staleness_max_sec"])

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return float(value)
        return None
