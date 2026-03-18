from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import aiohttp

from ..models import Snapshot


SOL_MINT = "So11111111111111111111111111111111111111112"
PREFERRED_QUOTES = {
    SOL_MINT,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
}
PREFERRED_QUOTE_SYMBOLS = {"SOL", "USDC", "USDT"}


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        value = float(value)
        if math.isfinite(value):
            return value
        return None
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass(slots=True)
class MinuteBucket:
    minute_id: int
    open_price: float
    high_price: float
    close_price: float
    buy_volume_usd: float = 0.0
    sell_volume_usd: float = 0.0
    num_buys: float = 0.0
    num_sells: float = 0.0

    @property
    def total_volume_usd(self) -> float:
        return self.buy_volume_usd + self.sell_volume_usd


@dataclass(slots=True)
class Point:
    ts_ms: int
    price: float
    buy_volume_usd: float
    sell_volume_usd: float
    num_buys: float
    num_sells: float


class Rolling5mIncrement:
    def __init__(self, window_intervals: int) -> None:
        self.window_intervals = max(window_intervals, 1)
        self.prev_total: float | None = None
        self.intervals: deque[float] = deque()

    def advance(self, rolling_total: float) -> float:
        if self.prev_total is None:
            self.prev_total = max(rolling_total, 0.0)
            return 0.0
        expired = self.intervals[0] if len(self.intervals) >= self.window_intervals else 0.0
        interval = rolling_total - self.prev_total + expired
        if not math.isfinite(interval) or interval < 0:
            interval = 0.0
        if len(self.intervals) >= self.window_intervals:
            self.intervals.popleft()
        self.intervals.append(interval)
        self.prev_total = max(rolling_total, 0.0)
        return interval

    def rolling_sum(self) -> float:
        return float(sum(self.intervals))


@dataclass(slots=True)
class TokenRuntime:
    interval_sec: float
    price_points: deque[Point] = field(default_factory=deque)
    minute_buckets: "OrderedDict[int, MinuteBucket]" = field(default_factory=OrderedDict)
    buy_volume_recon: Rolling5mIncrement | None = None
    sell_volume_recon: Rolling5mIncrement | None = None
    num_buys_recon: Rolling5mIncrement | None = None
    num_sells_recon: Rolling5mIncrement | None = None

    def __post_init__(self) -> None:
        window_intervals = max(int(round(300 / max(self.interval_sec, 1.0))), 1)
        self.buy_volume_recon = Rolling5mIncrement(window_intervals)
        self.sell_volume_recon = Rolling5mIncrement(window_intervals)
        self.num_buys_recon = Rolling5mIncrement(window_intervals)
        self.num_sells_recon = Rolling5mIncrement(window_intervals)

    def ingest(
        self,
        *,
        ts_ms: int,
        price: float,
        rolling_buy_volume_usd: float,
        rolling_sell_volume_usd: float,
        rolling_num_buys: float,
        rolling_num_sells: float,
    ) -> dict[str, Any]:
        buy_inc = self.buy_volume_recon.advance(max(rolling_buy_volume_usd, 0.0))
        sell_inc = self.sell_volume_recon.advance(max(rolling_sell_volume_usd, 0.0))
        num_buys_inc = self.num_buys_recon.advance(max(rolling_num_buys, 0.0))
        num_sells_inc = self.num_sells_recon.advance(max(rolling_num_sells, 0.0))

        point = Point(
            ts_ms=ts_ms,
            price=price,
            buy_volume_usd=buy_inc,
            sell_volume_usd=sell_inc,
            num_buys=num_buys_inc,
            num_sells=num_sells_inc,
        )
        self.price_points.append(point)
        min_ts = ts_ms - 6 * 60_000
        while self.price_points and self.price_points[0].ts_ms < min_ts:
            self.price_points.popleft()

        minute_id = ts_ms // 60_000
        bucket = self.minute_buckets.get(minute_id)
        if bucket is None:
            bucket = MinuteBucket(minute_id=minute_id, open_price=price, high_price=price, close_price=price)
            self.minute_buckets[minute_id] = bucket
        else:
            bucket.high_price = max(bucket.high_price, price)
            bucket.close_price = price
        bucket.buy_volume_usd += buy_inc
        bucket.sell_volume_usd += sell_inc
        bucket.num_buys += num_buys_inc
        bucket.num_sells += num_sells_inc
        while len(self.minute_buckets) > 8:
            self.minute_buckets.popitem(last=False)

        return self._derived(ts_ms=ts_ms)

    def _price_at_or_before(self, threshold_ms: int) -> float | None:
        candidate: float | None = None
        for point in self.price_points:
            if point.ts_ms <= threshold_ms:
                candidate = point.price
            else:
                break
        return candidate

    def _derived(self, *, ts_ms: int) -> dict[str, Any]:
        current_price = self.price_points[-1].price
        price_1m_ago = self._price_at_or_before(ts_ms - 60_000)
        price_5m_ago = self._price_at_or_before(ts_ms - 300_000)
        ret_1m_pct = ((current_price - price_1m_ago) / price_1m_ago * 100.0) if price_1m_ago not in (None, 0) else None
        ret_5m_pct = ((current_price - price_5m_ago) / price_5m_ago * 100.0) if price_5m_ago not in (None, 0) else None

        points_5m = [p for p in self.price_points if p.ts_ms >= ts_ms - 300_000]
        vol_5m = sum(p.buy_volume_usd + p.sell_volume_usd for p in points_5m)
        buy_vol_5m = sum(p.buy_volume_usd for p in points_5m)
        sell_vol_5m = sum(p.sell_volume_usd for p in points_5m)
        trades_5m = sum(p.num_buys + p.num_sells for p in points_5m)
        volume_weights = [max(p.buy_volume_usd + p.sell_volume_usd, 0.0) for p in points_5m]
        total_weight = sum(volume_weights)
        if total_weight > 0:
            vwap_5m = sum(p.price * w for p, w in zip(points_5m, volume_weights)) / total_weight
        elif points_5m:
            vwap_5m = sum(p.price for p in points_5m) / len(points_5m)
        else:
            vwap_5m = current_price
        rolling_5m_high_close = max((p.price for p in points_5m), default=current_price)

        current_minute_id = ts_ms // 60_000
        current_bucket = self.minute_buckets.get(current_minute_id)
        previous_completed = [bucket for mid, bucket in self.minute_buckets.items() if mid < current_minute_id]
        previous_completed = previous_completed[-5:]
        prev_volumes = [bucket.total_volume_usd for bucket in previous_completed]
        current_1m_volume_usd = current_bucket.total_volume_usd if current_bucket else None
        current_1m_buy_to_sell_volume = None
        candle_1m_open = None
        candle_1m_high = None
        if current_bucket is not None:
            current_1m_buy_to_sell_volume = current_bucket.buy_volume_usd / max(current_bucket.sell_volume_usd, 1.0)
            candle_1m_open = current_bucket.open_price
            candle_1m_high = current_bucket.high_price

        return {
            "current_price": current_price,
            "current_close": current_price,
            "ret_1m_pct": ret_1m_pct,
            "ret_5m_pct": ret_5m_pct,
            "volume_usd_effective": vol_5m,
            "buy_volume_usd_5m": buy_vol_5m,
            "sell_volume_usd_5m": sell_vol_5m,
            "trades_5m": trades_5m,
            "vwap_5m": vwap_5m,
            "rolling_5m_high_close": rolling_5m_high_close,
            "current_1m_volume_usd": current_1m_volume_usd,
            "previous_5_completed_1m_volume_usd": prev_volumes,
            "current_1m_buy_to_sell_volume": current_1m_buy_to_sell_volume,
            "candle_1m_open": candle_1m_open,
            "candle_1m_high": candle_1m_high,
        }


GMGN_KEY_ALIASES: dict[str, list[str]] = {
    "lp_locked_or_burned_pct": ["lp_locked_or_burned_pct", "burnt_pct", "burnt", "burnPoolPct", "burnedLpPct"],
    "insider_cluster_supply_pct": ["insider_cluster_supply_pct", "insider_pct", "insiderSupplyPct"],
    "same_funder_cluster_top20_pct": ["same_funder_cluster_top20_pct", "sameFunderTop20Pct"],
    "same_slot_cluster_ratio_first_150_trades_pct": [
        "same_slot_cluster_ratio_first_150_trades_pct",
        "bundle_ratio_first_150_trades",
        "first150BundlePct",
    ],
    "smart_buys_observed_window": ["smart_buys_observed_window", "smartBuysObserved", "smartBuysWindow"],
    "smart_quality_avg": ["smart_quality_avg", "smartQualityAvg"],
    "smart_cluster_concentration": ["smart_cluster_concentration", "smartClusterConcentration"],
    "copy_trade_score": ["copy_trade_score", "copyTradeScore"],
    "young_wallet_buy_volume_share_5m_pct": ["young_wallet_buy_volume_share_5m_pct", "youngWalletBuyVolumeShare5mPct"],
    "buy_size_cv_5m": ["buy_size_cv_5m", "buySizeCv5m"],
    "smart_wallets_same_funder_cluster_count": [
        "smart_wallets_same_funder_cluster_count",
        "smartWalletsSameFunderClusterCount",
    ],
    "related_wallet_count_2m": ["related_wallet_count_2m", "relatedWalletCount2m"],
    "related_wallet_combined_sell_usd_2m": [
        "related_wallet_combined_sell_usd_2m",
        "relatedWalletCombinedSellUsd2m",
    ],
    "buy_usd_volume_during_candidate_window": [
        "buy_usd_volume_during_candidate_window",
        "buyUsdVolumeDuringCandidateWindow",
    ],
}


class DexJupGmgnMvpProvider:
    def __init__(
        self,
        *,
        interval_sec: float = 15.0,
        timeout_sec: float = 10.0,
        chain_id: str = "solana",
        discovery_sources: list[str] | None = None,
        watchlist_tokens: list[str] | None = None,
        max_tokens: int = 30,
        min_liq_usd: float = 10000.0,
        min_fdv_usd: float = 0.0,
        jupiter_api_base: str = "https://api.jup.ag",
        jupiter_api_key: str | None = None,
        dexscreener_api_base: str = "https://api.dexscreener.com",
        buy_probe_usd: float = 5000.0,
        sell_probe_usd: float = 5000.0,
        sell_guard_probe_usd: float = 1000.0,
        gmgn_enrichment_url: str | None = None,
        gmgn_headers: dict[str, str] | None = None,
        request_concurrency: int = 8,
    ) -> None:
        self.interval_sec = interval_sec
        self.timeout_sec = timeout_sec
        self.chain_id = chain_id
        self.discovery_sources = discovery_sources or [
            "watchlist",
            "jupiter_recent",
            "jupiter_toporganicscore_5m",
            "dex_boosts_top",
        ]
        self.watchlist_tokens = watchlist_tokens or []
        self.max_tokens = max_tokens
        self.min_liq_usd = min_liq_usd
        self.min_fdv_usd = min_fdv_usd
        self.jupiter_api_base = jupiter_api_base.rstrip("/")
        self.jupiter_api_key = jupiter_api_key
        self.dexscreener_api_base = dexscreener_api_base.rstrip("/")
        self.buy_probe_usd = buy_probe_usd
        self.sell_probe_usd = sell_probe_usd
        self.sell_guard_probe_usd = sell_guard_probe_usd
        self.gmgn_enrichment_url = gmgn_enrichment_url
        self.gmgn_headers = gmgn_headers or {}
        self.request_concurrency = max(request_concurrency, 1)
        self._runtime_by_token: dict[str, TokenRuntime] = {}
        self._sem = asyncio.Semaphore(self.request_concurrency)

    async def __aiter__(self):
        async with aiohttp.ClientSession() as session:
            while True:
                started = time.time()
                snapshots = await self.poll_once(session)
                for snapshot in snapshots:
                    yield snapshot
                elapsed = time.time() - started
                await asyncio.sleep(max(self.interval_sec - elapsed, 0.0))

    async def poll_once(self, session: aiohttp.ClientSession) -> list[Snapshot]:
        return await self._poll_once(session)

    async def _poll_once(self, session: aiohttp.ClientSession) -> list[Snapshot]:
        discovered = await self._discover_tokens(session)
        if not discovered:
            return []

        jup_infos = await self._fetch_jupiter_tokens(session, discovered + [SOL_MINT])
        sol_info = jup_infos.get(SOL_MINT)
        sol_price = _safe_float(sol_info.get("usdPrice") if sol_info else None) or 0.0
        gmgn_enrichment = await self._fetch_gmgn_enrichment(session, discovered)

        tasks = [
            asyncio.create_task(
                self._build_snapshot(
                    session=session,
                    mint=mint,
                    jup_info=jup_infos.get(mint),
                    sol_price=sol_price,
                    gmgn_metrics=gmgn_enrichment.get(mint, {}),
                )
            )
            for mint in discovered
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        snapshots: list[Snapshot] = []
        for result in results:
            if isinstance(result, Snapshot):
                snapshots.append(result)
        return snapshots

    async def _discover_tokens(self, session: aiohttp.ClientSession) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        def push(token: str | None) -> None:
            token = (token or "").strip()
            if not token or token in seen or token == SOL_MINT:
                return
            seen.add(token)
            ordered.append(token)

        for token in self.watchlist_tokens:
            push(token)

        for source in self.discovery_sources:
            try:
                if source == "watchlist":
                    continue
                if source == "jupiter_recent":
                    for token in await self._discover_from_jupiter_recent(session):
                        push(token)
                elif source.startswith("jupiter_"):
                    payload = source.removeprefix("jupiter_")
                    if "_" in payload:
                        category, interval = payload.rsplit("_", 1)
                        for token in await self._discover_from_jupiter_category(session, category, interval):
                            push(token)
                elif source == "dex_boosts_top":
                    for token in await self._discover_from_dex_token_list(session, "/token-boosts/top/v1"):
                        push(token)
                elif source == "dex_boosts_latest":
                    for token in await self._discover_from_dex_token_list(session, "/token-boosts/latest/v1"):
                        push(token)
                elif source == "dex_profiles_latest":
                    for token in await self._discover_from_dex_token_list(session, "/token-profiles/latest/v1"):
                        push(token)
            except Exception:
                continue
            if len(ordered) >= self.max_tokens:
                break
        return ordered[: self.max_tokens]

    async def _discover_from_jupiter_recent(self, session: aiohttp.ClientSession) -> list[str]:
        data = await self._json_get(
            session,
            f"{self.jupiter_api_base}/tokens/v2/recent",
            params={"limit": str(self.max_tokens)},
            headers=self._jup_headers(),
        )
        return [str(item.get("id")) for item in data if isinstance(item, dict)]

    async def _discover_from_jupiter_category(self, session: aiohttp.ClientSession, category: str, interval: str) -> list[str]:
        data = await self._json_get(
            session,
            f"{self.jupiter_api_base}/tokens/v2/{category}/{interval}",
            params={"limit": str(self.max_tokens)},
            headers=self._jup_headers(),
        )
        return [str(item.get("id")) for item in data if isinstance(item, dict)]

    async def _discover_from_dex_token_list(self, session: aiohttp.ClientSession, path: str) -> list[str]:
        data = await self._json_get(session, f"{self.dexscreener_api_base}{path}")
        if isinstance(data, dict):
            items = data.get("items") if isinstance(data.get("items"), list) else [data]
        elif isinstance(data, list):
            items = data
        else:
            items = []
        out: list[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("chainId") == self.chain_id:
                token = item.get("tokenAddress") or item.get("address")
                if token:
                    out.append(str(token))
        return out

    async def _fetch_jupiter_tokens(self, session: aiohttp.ClientSession, mints: list[str]) -> dict[str, dict[str, Any]]:
        unique = []
        seen = set()
        for mint in mints:
            mint = mint.strip()
            if mint and mint not in seen:
                seen.add(mint)
                unique.append(mint)
        if not unique:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for idx in range(0, len(unique), 100):
            chunk = unique[idx : idx + 100]
            data = await self._json_get(
                session,
                f"{self.jupiter_api_base}/tokens/v2/search",
                params={"query": ",".join(chunk)},
                headers=self._jup_headers(),
            )
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("id"):
                        result[str(item["id"])] = item
        return result

    async def _fetch_gmgn_enrichment(self, session: aiohttp.ClientSession, mints: list[str]) -> dict[str, dict[str, Any]]:
        if not self.gmgn_enrichment_url:
            return {}
        if "{token}" in self.gmgn_enrichment_url:
            tasks = [
                asyncio.create_task(
                    self._json_get(
                        session,
                        self.gmgn_enrichment_url.format(token=mint),
                        headers=self.gmgn_headers,
                        swallow_errors=True,
                    )
                )
                for mint in mints
            ]
            raw_results = await asyncio.gather(*tasks)
            merged: dict[str, dict[str, Any]] = {}
            for mint, raw in zip(mints, raw_results):
                if raw is not None:
                    merged[mint] = self._normalize_gmgn_metrics(raw)
            return merged
        raw = await self._json_get(
            session,
            self.gmgn_enrichment_url,
            params={"tokens": ",".join(mints)},
            headers=self.gmgn_headers,
            swallow_errors=True,
        )
        if raw is None:
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        if isinstance(raw, dict) and isinstance(raw.get("tokens"), dict):
            for mint, payload in raw["tokens"].items():
                if isinstance(payload, dict):
                    normalized[str(mint)] = self._normalize_gmgn_metrics(payload)
        elif isinstance(raw, dict) and isinstance(raw.get("snapshots"), list):
            for item in raw["snapshots"]:
                if isinstance(item, dict) and item.get("token"):
                    normalized[str(item["token"])] = self._normalize_gmgn_metrics(item.get("metrics", item))
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("token"):
                    normalized[str(item["token"])] = self._normalize_gmgn_metrics(item)
        return normalized

    def _normalize_gmgn_metrics(self, payload: dict[str, Any]) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        for canonical, aliases in GMGN_KEY_ALIASES.items():
            for key in aliases:
                if key in payload:
                    metrics[canonical] = payload[key]
                    break
        return metrics

    async def _build_snapshot(
        self,
        *,
        session: aiohttp.ClientSession,
        mint: str,
        jup_info: dict[str, Any] | None,
        sol_price: float,
        gmgn_metrics: dict[str, Any],
    ) -> Snapshot | None:
        if not jup_info:
            return None
        pairs = await self._fetch_dex_pairs(session, mint)
        pair = self._select_primary_pair(pairs)
        if not pair:
            return None

        liq_usd = _safe_float(pair.get("liquidity", {}).get("usd")) or _safe_float(jup_info.get("liquidity")) or 0.0
        fdv_usd = _safe_float(pair.get("fdv")) or _safe_float(jup_info.get("fdv")) or 0.0
        if liq_usd < self.min_liq_usd or fdv_usd < self.min_fdv_usd:
            return None

        decimals = int(jup_info.get("decimals") or 0)
        current_price = _safe_float(pair.get("priceUsd")) or _safe_float(jup_info.get("usdPrice"))
        if current_price in (None, 0):
            return None

        quotes = await self._fetch_quote_metrics(
            session=session,
            mint=mint,
            decimals=decimals,
            token_price_usd=current_price,
            sol_price_usd=sol_price,
        )

        ts_ms = int(time.time() * 1000)
        jup_stats_5m = self._extract_stats(jup_info, "stats5m")
        runtime = self._runtime_by_token.setdefault(mint, TokenRuntime(interval_sec=self.interval_sec))
        runtime_metrics = runtime.ingest(
            ts_ms=ts_ms,
            price=current_price,
            rolling_buy_volume_usd=_safe_float(jup_stats_5m.get("buyVolume")) or 0.0,
            rolling_sell_volume_usd=_safe_float(jup_stats_5m.get("sellVolume")) or 0.0,
            rolling_num_buys=_safe_float(jup_stats_5m.get("numBuys")) or 0.0,
            rolling_num_sells=_safe_float(jup_stats_5m.get("numSells")) or 0.0,
        )

        first_pool_created_at = self._parse_iso_ms(
            (jup_info.get("firstPool") or {}).get("createdAt")
        ) or int(pair.get("pairCreatedAt") or 0)
        token_age_minutes = ((ts_ms - first_pool_created_at) / 60_000) if first_pool_created_at else None

        audit = jup_info.get("audit") or {}
        num_buys = _safe_float(jup_stats_5m.get("numBuys")) or 0.0
        num_sells = _safe_float(jup_stats_5m.get("numSells")) or 0.0
        num_organic_buyers = _safe_float(jup_stats_5m.get("numOrganicBuyers"))
        num_net_buyers = _safe_float(jup_stats_5m.get("numNetBuyers"))
        num_traders = _safe_float(jup_stats_5m.get("numTraders"))
        buy_volume = _safe_float(jup_stats_5m.get("buyVolume"))
        sell_volume = _safe_float(jup_stats_5m.get("sellVolume"))
        buy_organic_volume = _safe_float(jup_stats_5m.get("buyOrganicVolume"))

        unique_buyers_proxy = max(v for v in [num_organic_buyers, num_net_buyers, 0.0] if v is not None)
        if unique_buyers_proxy <= 0 and num_traders is not None:
            unique_buyers_proxy = max(num_traders * 0.5, 0.0)

        buyers_to_sellers_proxy = (num_buys / max(num_sells, 1.0)) if num_buys > 0 else None
        median_buy_usd_proxy = None
        if buy_organic_volume is not None and num_organic_buyers not in (None, 0):
            median_buy_usd_proxy = buy_organic_volume / max(num_organic_buyers, 1.0)
        elif buy_volume is not None and num_buys > 0:
            median_buy_usd_proxy = buy_volume / max(num_buys, 1.0)

        smart_quality_proxy = (_safe_float(jup_info.get("organicScore")) or 0.0) / 100.0
        smart_buys_proxy = (num_organic_buyers or 0.0) / 12.0
        smart_cluster_proxy = None
        if num_organic_buyers is not None and num_buys > 0:
            smart_cluster_proxy = _clamp(1.0 - (num_organic_buyers / max(num_buys, 1.0)), 0.0, 1.0)
        copy_trade_proxy = None
        if buy_organic_volume is not None and buy_volume not in (None, 0):
            copy_trade_proxy = _clamp(1.0 - (buy_organic_volume / max(buy_volume, 1.0)), 0.0, 1.0)

        metrics: dict[str, Any] = {
            "mint_authority_revoked": bool(audit.get("mintAuthorityDisabled")) if audit else None,
            "freeze_authority_revoked": bool(audit.get("freezeAuthorityDisabled")) if audit else None,
            "creator_token_share_pct": _safe_float(audit.get("devBalancePercentage")),
            "top10_ex_lp_pct": _safe_float(audit.get("topHoldersPercentage")),
            "liq_usd": liq_usd,
            "fdv_usd": fdv_usd,
            "token_age_minutes": token_age_minutes,
            "current_price": current_price,
            "ret_5m_pct": _safe_float(pair.get("priceChange", {}).get("m5")) or runtime_metrics.get("ret_5m_pct"),
            "volume_equiv_15m_usd": ((buy_volume or 0.0) + (sell_volume or 0.0)) * 3.0 if buy_volume is not None and sell_volume is not None else None,
            "trades_per_min_5m": (num_buys + num_sells) / 5.0 if (num_buys + num_sells) > 0 else None,
            "unique_buyers_5m": unique_buyers_proxy if unique_buyers_proxy > 0 else None,
            "buyers_to_sellers_5m": buyers_to_sellers_proxy,
            "buy_to_sell_volume_5m": ((buy_volume or 0.0) / max((sell_volume or 0.0), 1.0)) if buy_volume is not None else None,
            "median_buy_usd": median_buy_usd_proxy,
            "smart_buys_observed_window": smart_buys_proxy,
            "smart_quality_avg": smart_quality_proxy,
            "smart_cluster_concentration": smart_cluster_proxy,
            "copy_trade_score": copy_trade_proxy,
            "sell_success_rate_sim_pct": 100.0 if quotes.get("sell_guard_success") else 0.0,
            "effective_sell_slippage_1000_pct": quotes.get("effective_sell_slippage_1000_pct"),
            "price_impact_buy_5000_pct": quotes.get("price_impact_buy_5000_pct"),
            "price_impact_sell_5000_pct": quotes.get("price_impact_sell_5000_pct"),
            "depth_2pct_usd": quotes.get("depth_2pct_usd"),
        }
        metrics.update(runtime_metrics)
        if runtime_metrics.get("ret_5m_pct") is not None and metrics.get("ret_5m_pct") is None:
            metrics["ret_5m_pct"] = runtime_metrics["ret_5m_pct"]

        for key, value in gmgn_metrics.items():
            if value is not None:
                metrics[key] = value

        slot = int(quotes.get("context_slot") or 0)
        pair_address = str(pair.get("pairAddress") or pair.get("pair") or mint)
        symbol = str((pair.get("baseToken") or {}).get("symbol") or jup_info.get("symbol") or "?")

        return Snapshot(
            ts_ms=ts_ms,
            slot=slot,
            token=mint,
            symbol=symbol,
            pool=pair_address,
            data_age_sec=0.0,
            source="dexscreener+jupiter+gmgn-mvp",
            metrics=metrics,
        )

    async def _fetch_dex_pairs(self, session: aiohttp.ClientSession, mint: str) -> list[dict[str, Any]]:
        data = await self._json_get(
            session,
            f"{self.dexscreener_api_base}/token-pairs/v1/{self.chain_id}/{mint}",
            swallow_errors=True,
        )
        return data if isinstance(data, list) else []

    def _select_primary_pair(self, pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not pairs:
            return None
        ranked = sorted(
            pairs,
            key=lambda item: (
                _safe_float((item.get("liquidity") or {}).get("usd")) or 0.0,
                _safe_float(item.get("fdv")) or 0.0,
            ),
            reverse=True,
        )
        best = ranked[0]
        best_liq = _safe_float((best.get("liquidity") or {}).get("usd")) or 0.0
        for candidate in ranked[1:3]:
            cand_liq = _safe_float((candidate.get("liquidity") or {}).get("usd")) or 0.0
            if best_liq > 0 and cand_liq >= best_liq * 0.85 and self._pair_has_preferred_quote(candidate):
                return candidate
        return best

    def _pair_has_preferred_quote(self, pair: dict[str, Any]) -> bool:
        quote = pair.get("quoteToken") or {}
        quote_address = str(quote.get("address") or "")
        quote_symbol = str(quote.get("symbol") or "")
        return quote_address in PREFERRED_QUOTES or quote_symbol in PREFERRED_QUOTE_SYMBOLS

    async def _fetch_quote_metrics(
        self,
        *,
        session: aiohttp.ClientSession,
        mint: str,
        decimals: int,
        token_price_usd: float,
        sol_price_usd: float,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "context_slot": 0,
            "sell_guard_success": False,
        }
        if sol_price_usd <= 0 or token_price_usd <= 0 or decimals < 0:
            return result

        buy_amount_lamports = int(self.buy_probe_usd / sol_price_usd * (10**9))
        sell_amount_raw = int(self.sell_probe_usd / token_price_usd * (10**decimals))
        sell_guard_amount_raw = int(self.sell_guard_probe_usd / token_price_usd * (10**decimals))

        buy_quote_task = asyncio.create_task(self._quote_exact_in(session, SOL_MINT, mint, buy_amount_lamports))
        sell_quote_task = asyncio.create_task(self._quote_exact_in(session, mint, SOL_MINT, sell_amount_raw))
        sell_guard_task = asyncio.create_task(self._quote_exact_in(session, mint, SOL_MINT, sell_guard_amount_raw))
        buy_quote, sell_quote, sell_guard_quote = await asyncio.gather(buy_quote_task, sell_quote_task, sell_guard_task)

        if buy_quote:
            result["price_impact_buy_5000_pct"] = _safe_float(buy_quote.get("priceImpactPct"))
            result["context_slot"] = max(result["context_slot"], int(buy_quote.get("contextSlot") or 0))
        if sell_quote:
            result["price_impact_sell_5000_pct"] = _safe_float(sell_quote.get("priceImpactPct"))
            result["context_slot"] = max(result["context_slot"], int(sell_quote.get("contextSlot") or 0))
        if sell_guard_quote:
            result["effective_sell_slippage_1000_pct"] = _safe_float(sell_guard_quote.get("priceImpactPct"))
            result["sell_guard_success"] = True
            result["context_slot"] = max(result["context_slot"], int(sell_guard_quote.get("contextSlot") or 0))

        buy_impact = _safe_float(result.get("price_impact_buy_5000_pct"))
        sell_impact = _safe_float(result.get("price_impact_sell_5000_pct"))
        impact_ref = max(v for v in [buy_impact, sell_impact, 0.0] if v is not None)
        if impact_ref and impact_ref > 0:
            result["depth_2pct_usd"] = _clamp(self.buy_probe_usd * 2.0 / impact_ref, 0.0, 100000.0)
        elif buy_quote or sell_quote:
            result["depth_2pct_usd"] = 100000.0
        return result

    async def _quote_exact_in(
        self,
        session: aiohttp.ClientSession,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
    ) -> dict[str, Any] | None:
        if amount_raw <= 0:
            return None
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": "50",
            "restrictIntermediateTokens": "true",
        }
        return await self._json_get(
            session,
            f"{self.jupiter_api_base}/swap/v1/quote",
            params=params,
            headers=self._jup_headers(),
            swallow_errors=True,
        )

    async def _json_get(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        swallow_errors: bool = False,
    ) -> Any:
        try:
            async with self._sem:
                async with session.get(url, params=params, headers=headers, timeout=self.timeout_sec) as resp:
                    if resp.status >= 300:
                        body = await resp.text()
                        if swallow_errors:
                            return None
                        raise RuntimeError(f"GET {url} failed with {resp.status}: {body}")
                    return await resp.json(content_type=None)
        except Exception:
            if swallow_errors:
                return None
            raise

    def _jup_headers(self) -> dict[str, str] | None:
        if not self.jupiter_api_key:
            return None
        return {"x-api-key": self.jupiter_api_key}

    @staticmethod
    def _extract_stats(payload: dict[str, Any], key: str) -> dict[str, Any]:
        value = payload.get(key)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _parse_iso_ms(value: Any) -> int | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            from datetime import datetime

            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except ValueError:
            return None
