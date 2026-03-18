from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(slots=True)
class Snapshot:
    ts_ms: int
    slot: int
    token: str
    symbol: str
    pool: str
    metrics: dict[str, Any]
    data_age_sec: float | None = None
    source: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(
            ts_ms=int(data["ts_ms"]),
            slot=int(data.get("slot", 0)),
            token=str(data["token"]),
            symbol=str(data.get("symbol", "?")),
            pool=str(data["pool"]),
            metrics=dict(data.get("metrics", {})),
            data_age_sec=(float(data["data_age_sec"]) if data.get("data_age_sec") is not None else None),
            source=data.get("source"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Penalty:
    code: str
    delta: float

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "delta": self.delta}


@dataclass(slots=True)
class EvaluationResult:
    resolved_metrics: dict[str, Any]
    hard_veto_pass: bool
    failed_veto_rules: list[str]
    hard_veto_missing_metrics: list[str]
    candidate_prefilters_pass: bool
    failed_prefilters: list[str]
    score_metrics_completeness: float
    penalties: list[Penalty]
    score_base: float
    score_final: float
    subscores: dict[str, float]
    reason_codes: list[str]
    coordinated_related_selling: bool


@dataclass(slots=True)
class AlertEvent:
    ts_ms: int
    slot: int
    ruleset_version: str
    event: str
    token: str
    symbol: str
    pool: str
    score_base: float
    score_final: float
    subscores: dict[str, float]
    penalties: list[dict[str, Any]]
    hard_veto_pass: bool
    failed_veto_rules: list[str]
    reason_codes: list[str]
    metrics: dict[str, Any]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_ms": self.ts_ms,
            "slot": self.slot,
            "ruleset_version": self.ruleset_version,
            "event": self.event,
            "token": self.token,
            "symbol": self.symbol,
            "pool": self.pool,
            "score_base": self.score_base,
            "score_final": self.score_final,
            "subscores": self.subscores,
            "penalties": self.penalties,
            "hard_veto_pass": self.hard_veto_pass,
            "failed_veto_rules": self.failed_veto_rules,
            "reason_codes": self.reason_codes,
            "metrics": self.metrics,
            "reason": self.reason,
        }


@dataclass(slots=True)
class TokenState:
    dedupe_key: str
    status: str = "NEW"
    selected_pool: str | None = None
    candidate_ts_ms: int | None = None
    candidate_expires_at_ms: int | None = None
    cooldown_until_ms: int | None = None
    last_alert_ts_ms: int | None = None
    last_alert_price: float | None = None
    local_high_ref: float | None = None
    frozen_local_high_ref: float | None = None
    pullback_started: bool = False
    deepest_pullback_pct: float = 0.0
    last_updated_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TokenState":
        return cls(**data)


@dataclass(slots=True)
class RuntimeOptions:
    dispatch_rejects: bool = False
    dispatch_candidates: bool = True
    write_all_events_jsonl: str | None = None
