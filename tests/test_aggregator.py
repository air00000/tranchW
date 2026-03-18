from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sol_runner_bot.aggregator import (  # noqa: E402
    AggregatorConfig,
    AggregatorRuntimeConfig,
    AuthConfig,
    ListenConfig,
    LiveSnapshotAggregator,
    build_app,
)
from sol_runner_bot.config import ProviderConfig  # noqa: E402
from sol_runner_bot.models import Snapshot  # noqa: E402


class DummyProvider:
    def __init__(self) -> None:
        self.interval_sec = 0.01
        self.calls = 0

    async def poll_once(self, _session):
        self.calls += 1
        ts = 1_700_000_000_000 + self.calls
        return [
            Snapshot(
                ts_ms=ts,
                slot=123,
                token="mint-b",
                symbol="BBB",
                pool="pool-b",
                data_age_sec=0.0,
                source="dummy",
                metrics={"liq_usd": 1_000.0, "volume_equiv_15m_usd": 5_000.0},
            ),
            Snapshot(
                ts_ms=ts,
                slot=124,
                token="mint-a",
                symbol="AAA",
                pool="pool-a",
                data_age_sec=0.0,
                source="dummy",
                metrics={"liq_usd": 10_000.0, "volume_equiv_15m_usd": 1_000.0},
            ),
        ]


def make_config() -> AggregatorConfig:
    return AggregatorConfig(
        provider=ProviderConfig(kind="dex_jup_gmgn_mvp", poll_interval_sec=0.01),
        listen=ListenConfig(host="127.0.0.1", port=0),
        auth=AuthConfig(api_key="secret"),
        runtime=AggregatorRuntimeConfig(
            readiness_max_staleness_sec=10.0,
            error_backoff_sec=0.01,
            max_returned_snapshots=10,
        ),
    )


class AggregatorRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_loop_populates_cache(self):
        service = LiveSnapshotAggregator(make_config(), provider=DummyProvider())
        await service.start()
        try:
            await asyncio.sleep(0.03)
            snapshots = service.cache.list_snapshots()
            self.assertEqual(len(snapshots), 2)
            self.assertEqual(snapshots[0].token, "mint-a")
            self.assertTrue(service.cache.is_ready(10.0))
            self.assertIsNone(service.cache.last_error)
        finally:
            await service.close()

    async def test_http_endpoints_require_api_key_and_support_filters(self):
        app = await build_app(make_config(), provider=DummyProvider())
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            await asyncio.sleep(0.03)

            health_resp = await client.get("/health")
            self.assertEqual(health_resp.status, 200)
            health = await health_resp.json()
            self.assertTrue(health["ok"])

            unauthorized = await client.get("/snapshots")
            self.assertEqual(unauthorized.status, 401)

            resp = await client.get("/snapshots?limit=1&tokens=mint-a", headers={"X-API-Key": "secret"})
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertEqual(len(body["snapshots"]), 1)
            self.assertEqual(body["snapshots"][0]["token"], "mint-a")

            one = await client.get("/snapshots/mint-b", headers={"X-API-Key": "secret"})
            self.assertEqual(one.status, 200)
            payload = await one.json()
            self.assertEqual(payload["token"], "mint-b")
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
