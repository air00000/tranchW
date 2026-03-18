# Normalized snapshot contract

Бот ожидает поток `Snapshot`-объектов в таком формате:

```json
{
  "ts_ms": 1760000000000,
  "slot": 1000,
  "token": "So1Runner11111111111111111111111111111111111",
  "symbol": "RUN1",
  "pool": "Pool11111111111111111111111111111111111111",
  "data_age_sec": 1,
  "source": "helius+jupiter+internal-analytics",
  "metrics": {
    "mint_authority_revoked": true,
    "freeze_authority_revoked": true,
    "lp_locked_or_burned_pct": 99,
    "creator_token_share_pct": 0.8,
    "top10_ex_lp_pct": 14,
    "insider_cluster_supply_pct": 10,
    "same_funder_cluster_top20_pct": 17,
    "same_slot_cluster_ratio_first_150_trades_pct": 6,
    "sell_success_rate_sim_pct": 99,
    "effective_sell_slippage_1000_pct": 5,
    "liq_usd": 145000,
    "fdv_usd": 820000,

    "token_age_minutes": 12,
    "ret_1m_pct": 11,
    "ret_5m_pct": 35,
    "pullback_from_high_5m_pct": 2,
    "must_be_above_vwap_5m": true,
    "volume_equiv_15m_usd": 330000,
    "trades_per_min_5m": 42,
    "unique_buyers_5m": 115,
    "buyers_to_sellers_5m": 2.9,
    "buy_to_sell_volume_5m": 3.4,
    "median_buy_usd": 120,
    "smart_buys_observed_window": 8,
    "smart_quality_avg": 0.84,
    "smart_cluster_concentration": 0.16,
    "copy_trade_score": 0.11,
    "blowoff_1m_pct": 16,

    "depth_2pct_usd": 45000,
    "price_impact_buy_5000_pct": 1.8,
    "price_impact_sell_5000_pct": 2.2,

    "young_wallet_buy_volume_share_5m_pct": 38,
    "buy_size_cv_5m": 0.31,
    "smart_wallets_same_funder_cluster_count": 1,
    "related_wallet_count_2m": 0,
    "related_wallet_combined_sell_usd_2m": 0,
    "buy_usd_volume_during_candidate_window": 42000,

    "current_1m_close": 1.12,
    "current_1m_volume_usd": 7200,
    "previous_5_completed_1m_volume_usd_avg": 5200,
    "current_1m_buy_to_sell_volume": 1.3
  }
}
```

## Поля, которые бот умеет вывести сам

Если upstream не прислал метрику, бот попробует посчитать её сам при наличии базовых полей:

- `liq_to_fdv` из `liq_usd / fdv_usd`
- `volume_equiv_15m_usd` из `volume_usd_effective` и `token_age_minutes`
- `buyers_to_sellers_5m` из `unique_buyers_5m / unique_sellers_5m`
- `buy_to_sell_volume_5m` из `buy_volume_usd_5m / sell_volume_usd_5m`
- `must_be_above_vwap_5m` из `current_price > vwap_5m`
- `pullback_from_high_5m_pct` из `rolling_5m_high_close` и `current_close`
- `blowoff_1m_pct` из `candle_1m_high` и `candle_1m_open`
- `trades_per_min_5m` из `trades_5m / 5`
- `previous_5_completed_1m_volume_usd_avg` из массива `previous_5_completed_1m_volume_usd`

## Что должен считать upstream-аналитик

Следующие метрики бот специально не пытается реконструировать из голого потока свопов:

- `smart_buys_observed_window`, `smart_quality_avg`, `smart_cluster_concentration`
- `copy_trade_score`
- `insider_cluster_supply_pct`, `same_funder_cluster_top20_pct`
- `same_slot_cluster_ratio_first_150_trades_pct`
- `young_wallet_buy_volume_share_5m_pct`
- `related_wallet_count_2m`, `related_wallet_combined_sell_usd_2m`
- `lp_locked_or_burned_pct`

Эти поля логично поставлять отдельным enrichment-сервисом, который агрегирует Solana RPC/Helius, хранилище держателей и свой graph/funder analysis.
