from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sol_runner_bot.providers.dex_jup_gmgn_mvp import DexJupGmgnMvpProvider, TokenRuntime


class LiveMvpProviderTests(unittest.TestCase):
    def test_token_runtime_derives_recent_metrics(self):
        runtime = TokenRuntime(interval_sec=15.0)
        ts0 = 1_700_000_000_000
        # Warm up a 5 minute window with steady growth.
        buy_total = 0.0
        sell_total = 0.0
        num_buys = 0.0
        num_sells = 0.0
        derived = {}
        price = 1.0
        for step in range(25):  # > 6 minutes
            ts = ts0 + step * 15_000
            price *= 1.01
            buy_total += 1200
            sell_total += 600
            num_buys += 12
            num_sells += 6
            derived = runtime.ingest(
                ts_ms=ts,
                price=price,
                rolling_buy_volume_usd=buy_total,
                rolling_sell_volume_usd=sell_total,
                rolling_num_buys=num_buys,
                rolling_num_sells=num_sells,
            )
        self.assertIsNotNone(derived["ret_1m_pct"])
        self.assertIsNotNone(derived["ret_5m_pct"])
        self.assertGreater(derived["current_1m_volume_usd"], 0)
        self.assertTrue(derived["previous_5_completed_1m_volume_usd"])
        self.assertGreater(derived["vwap_5m"], 0)
        self.assertGreaterEqual(derived["rolling_5m_high_close"], derived["current_close"])

    def test_gmgn_normalization_supports_aliases(self):
        provider = DexJupGmgnMvpProvider(gmgn_enrichment_url="https://example.invalid/{token}")
        payload = {
            "burnt_pct": 98.5,
            "insiderSupplyPct": 12.0,
            "sameFunderTop20Pct": 15.0,
            "first150BundlePct": 7.5,
            "smartBuysObserved": 4,
            "smartQualityAvg": 0.78,
            "smartClusterConcentration": 0.18,
            "copyTradeScore": 0.12,
        }
        metrics = provider._normalize_gmgn_metrics(payload)
        self.assertEqual(metrics["lp_locked_or_burned_pct"], 98.5)
        self.assertEqual(metrics["insider_cluster_supply_pct"], 12.0)
        self.assertEqual(metrics["same_funder_cluster_top20_pct"], 15.0)
        self.assertEqual(metrics["same_slot_cluster_ratio_first_150_trades_pct"], 7.5)
        self.assertEqual(metrics["smart_buys_observed_window"], 4)
        self.assertEqual(metrics["smart_quality_avg"], 0.78)
        self.assertEqual(metrics["smart_cluster_concentration"], 0.18)
        self.assertEqual(metrics["copy_trade_score"], 0.12)


if __name__ == "__main__":
    unittest.main()
