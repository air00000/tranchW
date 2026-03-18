from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class ProviderConfig:
    kind: str
    file_path: str | None = None
    poll_url: str | None = None
    poll_interval_sec: float = 5.0
    headers: dict[str, str] | None = None
    timeout_sec: float = 10.0
    chain_id: str = "solana"
    discovery_sources: list[str] = field(default_factory=list)
    watchlist_tokens: list[str] = field(default_factory=list)
    max_tokens: int = 30
    min_liq_usd: float = 10000.0
    min_fdv_usd: float = 0.0
    jupiter_api_base: str = "https://api.jup.ag"
    jupiter_api_key: str | None = None
    dexscreener_api_base: str = "https://api.dexscreener.com"
    buy_probe_usd: float = 5000.0
    sell_probe_usd: float = 5000.0
    sell_guard_probe_usd: float = 1000.0
    gmgn_enrichment_url: str | None = None
    gmgn_headers: dict[str, str] | None = None
    request_concurrency: int = 8


@dataclass(slots=True)
class TelegramConfig:
    enabled: bool = False
    bot_token: str | None = None
    chat_id: str | None = None
    parse_mode: str = "HTML"
    disable_notification: bool = True
    topic_id: int | None = None


@dataclass(slots=True)
class WebhookConfig:
    enabled: bool = False
    url: str | None = None
    timeout_sec: float = 10.0
    headers: dict[str, str] | None = None


@dataclass(slots=True)
class StorageConfig:
    sqlite_path: str = "./state.db"


@dataclass(slots=True)
class RuntimeConfig:
    dispatch_rejects: bool = False
    dispatch_candidates: bool = True
    event_log_jsonl: str | None = "./events.jsonl"


@dataclass(slots=True)
class BotConfig:
    ruleset_path: str
    provider: ProviderConfig
    telegram: TelegramConfig
    webhook: WebhookConfig
    storage: StorageConfig
    runtime: RuntimeConfig


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def load_config(path: str | os.PathLike[str]) -> BotConfig:
    raw = Path(path).read_text(encoding="utf-8")
    data = _expand(yaml.safe_load(raw))

    provider_raw = data.get("provider", {})
    telegram_raw = data.get("telegram", {})
    webhook_raw = data.get("webhook", {})
    storage_raw = data.get("storage", {})
    runtime_raw = data.get("runtime", {})

    provider = ProviderConfig(
        kind=provider_raw.get("kind", "file_replay"),
        file_path=provider_raw.get("file_path"),
        poll_url=provider_raw.get("poll_url"),
        poll_interval_sec=float(provider_raw.get("poll_interval_sec", 5.0)),
        headers=provider_raw.get("headers"),
        timeout_sec=float(provider_raw.get("timeout_sec", 10.0)),
        chain_id=provider_raw.get("chain_id", "solana"),
        discovery_sources=list(provider_raw.get("discovery_sources", [])),
        watchlist_tokens=list(provider_raw.get("watchlist_tokens", [])),
        max_tokens=int(provider_raw.get("max_tokens", 30)),
        min_liq_usd=float(provider_raw.get("min_liq_usd", 10000.0)),
        min_fdv_usd=float(provider_raw.get("min_fdv_usd", 0.0)),
        jupiter_api_base=provider_raw.get("jupiter_api_base", "https://api.jup.ag"),
        jupiter_api_key=provider_raw.get("jupiter_api_key"),
        dexscreener_api_base=provider_raw.get("dexscreener_api_base", "https://api.dexscreener.com"),
        buy_probe_usd=float(provider_raw.get("buy_probe_usd", 5000.0)),
        sell_probe_usd=float(provider_raw.get("sell_probe_usd", 5000.0)),
        sell_guard_probe_usd=float(provider_raw.get("sell_guard_probe_usd", 1000.0)),
        gmgn_enrichment_url=provider_raw.get("gmgn_enrichment_url"),
        gmgn_headers=provider_raw.get("gmgn_headers"),
        request_concurrency=int(provider_raw.get("request_concurrency", 8)),
    )

    telegram = TelegramConfig(
        enabled=bool(telegram_raw.get("enabled", False)),
        bot_token=telegram_raw.get("bot_token"),
        chat_id=telegram_raw.get("chat_id"),
        parse_mode=telegram_raw.get("parse_mode", "HTML"),
        disable_notification=bool(telegram_raw.get("disable_notification", True)),
        topic_id=(int(telegram_raw["topic_id"]) if telegram_raw.get("topic_id") is not None else None),
    )

    webhook = WebhookConfig(
        enabled=bool(webhook_raw.get("enabled", False)),
        url=webhook_raw.get("url"),
        timeout_sec=float(webhook_raw.get("timeout_sec", 10.0)),
        headers=webhook_raw.get("headers"),
    )

    storage = StorageConfig(sqlite_path=storage_raw.get("sqlite_path", "./state.db"))
    runtime = RuntimeConfig(
        dispatch_rejects=bool(runtime_raw.get("dispatch_rejects", False)),
        dispatch_candidates=bool(runtime_raw.get("dispatch_candidates", True)),
        event_log_jsonl=runtime_raw.get("event_log_jsonl", "./events.jsonl"),
    )

    ruleset_path = data.get("ruleset_path")
    if not ruleset_path:
        raise ValueError("ruleset_path is required")

    return BotConfig(
        ruleset_path=ruleset_path,
        provider=provider,
        telegram=telegram,
        webhook=webhook,
        storage=storage,
        runtime=runtime,
    )
