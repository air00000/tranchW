from __future__ import annotations

import argparse
import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from aiohttp import web

from .config import ProviderConfig, _expand
from .models import Snapshot
from .providers import DexJupGmgnMvpProvider


HEADER_API_KEY = "X-API-Key"
APP_SERVICE_KEY = web.AppKey("service", object)
APP_CONFIG_KEY = web.AppKey("config", object)


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class ListenConfig:
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass(slots=True)
class AuthConfig:
    api_key: str | None = None


@dataclass(slots=True)
class AggregatorRuntimeConfig:
    readiness_max_staleness_sec: float = 90.0
    error_backoff_sec: float = 5.0
    max_returned_snapshots: int = 100


@dataclass(slots=True)
class AggregatorConfig:
    provider: ProviderConfig
    listen: ListenConfig = field(default_factory=ListenConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    runtime: AggregatorRuntimeConfig = field(default_factory=AggregatorRuntimeConfig)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "AggregatorConfig":
        raw = Path(path).read_text(encoding="utf-8")
        data = _expand(yaml.safe_load(raw) or {})

        provider_raw = data.get("provider", {})
        provider = ProviderConfig(
            kind=provider_raw.get("kind", "dex_jup_gmgn_mvp"),
            file_path=provider_raw.get("file_path"),
            poll_url=provider_raw.get("poll_url"),
            poll_interval_sec=float(provider_raw.get("poll_interval_sec", 15.0)),
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

        listen = ListenConfig(**dict(data.get("listen", {})))
        auth = AuthConfig(**dict(data.get("auth", {})))
        runtime = AggregatorRuntimeConfig(**dict(data.get("runtime", {})))
        return cls(provider=provider, listen=listen, auth=auth, runtime=runtime)


@dataclass(slots=True)
class SnapshotCache:
    snapshots_by_key: dict[str, Snapshot] = field(default_factory=dict)
    last_success_ms: int | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    total_polls: int = 0
    total_successful_polls: int = 0
    last_cycle_duration_ms: int | None = None

    def replace(self, snapshots: list[Snapshot], duration_ms: int) -> None:
        self.snapshots_by_key = {self._key(snapshot): snapshot for snapshot in snapshots}
        self.last_success_ms = now_ms()
        self.last_error = None
        self.consecutive_errors = 0
        self.total_polls += 1
        self.total_successful_polls += 1
        self.last_cycle_duration_ms = duration_ms

    def record_error(self, error: str, duration_ms: int) -> None:
        self.last_error = error
        self.consecutive_errors += 1
        self.total_polls += 1
        self.last_cycle_duration_ms = duration_ms

    def is_ready(self, max_staleness_sec: float) -> bool:
        if self.last_success_ms is None:
            return False
        return (now_ms() - self.last_success_ms) / 1000.0 <= max_staleness_sec

    def list_snapshots(self, *, tokens: set[str] | None = None, limit: int | None = None) -> list[Snapshot]:
        values = list(self.snapshots_by_key.values())
        if tokens:
            values = [snapshot for snapshot in values if snapshot.token in tokens]
        values.sort(
            key=lambda snapshot: (
                self._metric(snapshot, "liq_usd"),
                self._metric(snapshot, "volume_equiv_15m_usd"),
                self._metric(snapshot, "fdv_usd"),
            ),
            reverse=True,
        )
        if limit is not None:
            values = values[: max(limit, 0)]
        return values

    def get_snapshot(self, token: str) -> Snapshot | None:
        matches = [snapshot for snapshot in self.snapshots_by_key.values() if snapshot.token == token]
        if not matches:
            return None
        matches.sort(key=lambda snapshot: self._metric(snapshot, "liq_usd"), reverse=True)
        return matches[0]

    @staticmethod
    def _metric(snapshot: Snapshot, key: str) -> float:
        value = snapshot.metrics.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _key(snapshot: Snapshot) -> str:
        return f"{snapshot.token}:{snapshot.pool}"


class LiveSnapshotAggregator:
    def __init__(
        self,
        config: AggregatorConfig,
        *,
        provider: DexJupGmgnMvpProvider | Any | None = None,
    ) -> None:
        self.config = config
        self.provider = provider or self._build_provider(config.provider)
        self.cache = SnapshotCache()
        self.started_at_ms = now_ms()
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None

    @staticmethod
    def _build_provider(config: ProviderConfig) -> DexJupGmgnMvpProvider:
        if config.kind != "dex_jup_gmgn_mvp":
            raise ValueError("Aggregator currently supports only provider.kind=dex_jup_gmgn_mvp")
        return DexJupGmgnMvpProvider(
            interval_sec=config.poll_interval_sec,
            timeout_sec=config.timeout_sec,
            chain_id=config.chain_id,
            discovery_sources=config.discovery_sources,
            watchlist_tokens=config.watchlist_tokens,
            max_tokens=config.max_tokens,
            min_liq_usd=config.min_liq_usd,
            min_fdv_usd=config.min_fdv_usd,
            jupiter_api_base=config.jupiter_api_base,
            jupiter_api_key=config.jupiter_api_key,
            dexscreener_api_base=config.dexscreener_api_base,
            buy_probe_usd=config.buy_probe_usd,
            sell_probe_usd=config.sell_probe_usd,
            sell_guard_probe_usd=config.sell_guard_probe_usd,
            gmgn_enrichment_url=config.gmgn_enrichment_url,
            gmgn_headers=config.gmgn_headers,
            request_concurrency=config.request_concurrency,
        )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run_loop(), name="sol_runner_snapshot_aggregator")

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _run_loop(self) -> None:
        assert self._session is not None
        while True:
            started = time.time()
            try:
                snapshots = await self.provider.poll_once(self._session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                duration_ms = int((time.time() - started) * 1000)
                self.cache.record_error(str(exc), duration_ms)
                sleep_for = max(
                    getattr(self.provider, "interval_sec", self.config.provider.poll_interval_sec) - (time.time() - started),
                    self.config.runtime.error_backoff_sec,
                )
            else:
                duration_ms = int((time.time() - started) * 1000)
                self.cache.replace(snapshots, duration_ms)
                sleep_for = max(
                    getattr(self.provider, "interval_sec", self.config.provider.poll_interval_sec) - (time.time() - started),
                    0.0,
                )
            await asyncio.sleep(sleep_for)

    def health_payload(self) -> dict[str, Any]:
        snapshots = self.cache.list_snapshots(limit=self.config.runtime.max_returned_snapshots)
        return {
            "ok": True,
            "ready": self.cache.is_ready(self.config.runtime.readiness_max_staleness_sec),
            "uptime_sec": round((now_ms() - self.started_at_ms) / 1000.0, 2),
            "provider_kind": self.config.provider.kind,
            "snapshot_count": len(snapshots),
            "last_success_ms": self.cache.last_success_ms,
            "last_cycle_duration_ms": self.cache.last_cycle_duration_ms,
            "last_error": self.cache.last_error,
            "consecutive_errors": self.cache.consecutive_errors,
            "total_polls": self.cache.total_polls,
            "total_successful_polls": self.cache.total_successful_polls,
        }


def _check_auth(request: web.Request, config: AggregatorConfig) -> None:
    expected = (config.auth.api_key or "").strip()
    if not expected:
        return
    provided = (request.headers.get(HEADER_API_KEY) or "").strip()
    if provided != expected:
        raise web.HTTPUnauthorized(text=f"missing or invalid {HEADER_API_KEY}")


async def build_app(
    config: AggregatorConfig,
    *,
    provider: DexJupGmgnMvpProvider | Any | None = None,
) -> web.Application:
    service = LiveSnapshotAggregator(config, provider=provider)
    await service.start()

    app = web.Application()
    app[APP_SERVICE_KEY] = service
    app[APP_CONFIG_KEY] = config

    async def on_cleanup(_: web.Application) -> None:
        await service.close()

    app.on_cleanup.append(on_cleanup)

    async def health(_: web.Request) -> web.Response:
        return web.json_response(service.health_payload())

    async def ready(_: web.Request) -> web.Response:
        payload = service.health_payload()
        if payload["ready"]:
            return web.json_response(payload)
        raise web.HTTPServiceUnavailable(text=yaml.safe_dump(payload, sort_keys=False))

    async def snapshots(request: web.Request) -> web.Response:
        _check_auth(request, config)
        limit_raw = request.query.get("limit")
        limit = None
        if limit_raw is not None:
            try:
                limit = min(int(limit_raw), config.runtime.max_returned_snapshots)
            except ValueError as exc:
                raise web.HTTPBadRequest(text="limit must be an integer") from exc
        tokens_param = (request.query.get("tokens") or "").strip()
        tokens = {token.strip() for token in tokens_param.split(",") if token.strip()} or None
        payload = {
            "snapshots": [snapshot.to_dict() for snapshot in service.cache.list_snapshots(tokens=tokens, limit=limit)],
            "updated_at_ms": service.cache.last_success_ms,
            "source": config.provider.kind,
        }
        return web.json_response(payload)

    async def snapshot_by_token(request: web.Request) -> web.Response:
        _check_auth(request, config)
        token = request.match_info["token"]
        snapshot = service.cache.get_snapshot(token)
        if snapshot is None:
            raise web.HTTPNotFound(text=f"unknown token: {token}")
        return web.json_response(snapshot.to_dict())

    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_get("/snapshots", snapshots)
    app.router.add_get("/snapshots/{token}", snapshot_by_token)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP snapshot aggregator for DexScreener + Jupiter + optional GMGN")
    parser.add_argument("--config", required=True, help="Path to aggregator YAML config")
    args = parser.parse_args()

    config = AggregatorConfig.load(args.config)
    web.run_app(build_app(config), host=config.listen.host, port=config.listen.port)


if __name__ == "__main__":
    main()
