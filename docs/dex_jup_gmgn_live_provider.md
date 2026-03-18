# DexScreener + Jupiter + GMGN live MVP provider

Этот provider предназначен для live-MVP запуска без отдельного Solana индексера.

## Что он делает

1. **Discovery**
   - Jupiter Tokens V2: `recent`, `toporganicscore`, `toptrending`, `toptraded`
   - DexScreener: `token-boosts/top`, `token-boosts/latest`, `token-profiles/latest`
   - ручной `watchlist_tokens`

2. **Pool selection**
   - тянет `token-pairs/v1/{chainId}/{tokenAddress}` из DexScreener
   - выбирает primary pool по максимальной ликвидности
   - если ликвидность близка, предпочитает quote в `SOL/USDC/USDT`

3. **Token safety / flow / metadata**
   - Jupiter Tokens V2 `search`
   - использует `audit.mintAuthorityDisabled`, `audit.freezeAuthorityDisabled`, `audit.topHoldersPercentage`, `audit.devBalancePercentage`
   - использует `stats5m` для `buyVolume`, `sellVolume`, `numBuys`, `numSells`, `numOrganicBuyers`, `numNetBuyers`

4. **Price impact / sellability**
   - Jupiter `swap/v1/quote`
   - считает:
     - `price_impact_buy_5000_pct`
     - `price_impact_sell_5000_pct`
     - `effective_sell_slippage_1000_pct`
     - `sell_success_rate_sim_pct` (MVP proxy: есть quote => 100, нет => 0)
     - `depth_2pct_usd` (MVP proxy)

5. **Optional GMGN enrichment**
   - provider **не использует скрытые GMGN endpoint'ы**
   - вместо этого может принять ваш внешний enrichment endpoint (`gmgn_enrichment_url`)
   - если у вас есть whitelisted crawler / scraper / internal bridge, provider замапит его поля в snapshot schema

## Важное ограничение

Без внешнего GMGN enrichment feed публичный стек **не закрывает полностью** следующие strict-метрики:

- `lp_locked_or_burned_pct`
- `insider_cluster_supply_pct`
- `same_funder_cluster_top20_pct`
- `same_slot_cluster_ratio_first_150_trades_pct`
- `related_wallet_*`

Поэтому в проект добавлен отдельный ruleset:

- `rules/sol_meme_runner_live_mvp_dex_jup_gmgn.yaml`

Он сохраняет core-логику раннера, но ослабляет hard-veto, которые требуют частного cluster/enrichment слоя.

## Proxy-метрики в live MVP

Когда GMGN enrichment отсутствует, provider заполняет несколько полей прокси-значениями:

- `unique_buyers_5m` ← max(`numOrganicBuyers`, `numNetBuyers`)
- `buyers_to_sellers_5m` ← `numBuys / numSells`
- `median_buy_usd` ← `buyOrganicVolume / numOrganicBuyers` или `buyVolume / numBuys`
- `smart_quality_avg` ← `organicScore / 100`
- `smart_buys_observed_window` ← `numOrganicBuyers / 12`
- `smart_cluster_concentration` ← `1 - numOrganicBuyers / numBuys`
- `copy_trade_score` ← `1 - buyOrganicVolume / buyVolume`

Это **MVP-эвристики**, а не строгие on-chain cluster metrics.

## GMGN enrichment contract

Поддерживаются 2 формата:

### 1. Batch GET

`GET <gmgn_enrichment_url>?tokens=mint1,mint2,...`

Ответ:

```json
{
  "tokens": {
    "mint1": {
      "burnt_pct": 99,
      "insiderSupplyPct": 12.5,
      "sameFunderTop20Pct": 15.2,
      "first150BundlePct": 8.3,
      "smartBuysObserved": 5,
      "smartQualityAvg": 0.81,
      "smartClusterConcentration": 0.18,
      "copyTradeScore": 0.12
    }
  }
}
```

### 2. Per-token GET

Если `gmgn_enrichment_url` содержит `{token}`:

```text
https://your-service.local/gmgn/{token}
```

Тогда provider вызовет endpoint отдельно для каждого mint.

## Запуск

```bash
python -m sol_runner_bot.app --config config/example.dex_jup_gmgn.yml
```

Для strict режима используйте оригинальный ruleset **только если** ваш `gmgn_enrichment_url` реально заполняет недостающие cluster/security метрики.
