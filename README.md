# Solana Runner Alert Bot v1.1

Готовый бот по алертам раннеров на Solana.

В версии `v1.1` добавлен **live-MVP provider** под стек:

- DexScreener
- Jupiter Tokens V2 + Quote
- optional GMGN enrichment

## Что есть в проекте

- загрузка ruleset из JSON/YAML
- exact rules-engine
- hard-veto и candidate prefilters
- score model 0..1
- anti-sybil / anti-cabal penalties
- state machine `NEW -> WATCH_CANDIDATE -> ALERTED -> COOLDOWN -> REARMED`
- SQLite persistence
- Telegram и webhook алерты
- file replay режим
- HTTP polling режим
- новый live provider `dex_jup_gmgn_mvp`
- live-MVP ruleset `sol_meme_runner_live_mvp_dex_jup_gmgn`

## Быстрый старт

### 1. Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

### 2. Replay демо

```bash
python -m sol_runner_bot.app --config config/example.replay.yml
```

### 3. Live MVP: DexScreener + Jupiter + GMGN enrichment

```bash
cp .env.example .env
export $(grep -v '^#' .env | xargs)
python -m sol_runner_bot.app --config config/example.dex_jup_gmgn.yml
```

## Важная разница между strict и live-MVP

В проекте теперь **два режима правил**:

### Strict

- `rules/sol_meme_runner_ruleset_v1_1.yaml`
- требует полный enrichment слой
- подходит, если у вас уже есть GMGN/cluster analytics / holder analytics / sell simulation слой

### Live MVP

- `rules/sol_meme_runner_live_mvp_dex_jup_gmgn.yaml`
- рассчитан на публичный стек DexScreener + Jupiter
- умеет использовать внешний GMGN enrichment feed, если он есть
- часть smart / flow метрик заполняется Jupiter-based proxy значениями

## Новый provider: `dex_jup_gmgn_mvp`

Что он делает:

- discovery токенов через Jupiter `recent`, `toporganicscore`, `toptrending`, `toptraded`
- дополнительный discovery через DexScreener boosts/profiles
- выбирает primary pool через DexScreener `token-pairs`
- тянет token audit / flow / holder metrics через Jupiter Tokens V2
- считает buy/sell impact через Jupiter Quote
- может мерджить GMGN enrichment из вашего HTTP endpoint
- поддерживает rolling 1m/5m derived metrics для continuation-trigger

Подробности: `docs/dex_jup_gmgn_live_provider.md`

## Конфиг live-MVP

Смотри:

- `config/example.dex_jup_gmgn.yml`

Обязательное для live запуска:

- `JUPITER_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Опционально:

- `GMGN_ENRICHMENT_URL`
- `GMGN_ENRICHMENT_TOKEN`

## Что provider заполняет хорошо

Надежно / напрямую:

- `mint_authority_revoked`
- `freeze_authority_revoked`
- `creator_token_share_pct`
- `top10_ex_lp_pct`
- `liq_usd`
- `fdv_usd`
- `token_age_minutes`
- `buy_to_sell_volume_5m`
- `price_impact_buy_5000_pct`
- `price_impact_sell_5000_pct`
- `effective_sell_slippage_1000_pct`

Через live-MVP proxies:

- `unique_buyers_5m`
- `buyers_to_sellers_5m`
- `median_buy_usd`
- `smart_buys_observed_window`
- `smart_quality_avg`
- `smart_cluster_concentration`
- `copy_trade_score`
- `depth_2pct_usd`
- `sell_success_rate_sim_pct`

Только через внешний GMGN enrichment / private analytics:

- `lp_locked_or_burned_pct`
- `insider_cluster_supply_pct`
- `same_funder_cluster_top20_pct`
- `same_slot_cluster_ratio_first_150_trades_pct`
- `related_wallet_count_2m`
- `related_wallet_combined_sell_usd_2m`

## Тесты

```bash
python -m unittest discover -s tests -v
```

Покрыто:

- original replay flow: candidate -> alert -> rearm -> alert
- hard-veto reject
- live-MVP runtime derivation
- GMGN alias normalization


## HTTP aggregator mode

Теперь проект умеет работать в **двухпроцессном live-режиме**:

1. `sol_runner_bot.aggregator` собирает live snapshots из DexScreener + Jupiter + optional GMGN enrichment и отдает их по `GET /snapshots`.
2. основной бот читает эти снапшоты через `provider.kind=http_metrics`.

### Запуск агрегатора

```bash
python -m sol_runner_bot.aggregator --config config/example.aggregator.dex_jup_gmgn.yml
```

### Запуск бота поверх агрегатора

```bash
python -m sol_runner_bot.app --config config/example.bot_http_live_mvp.yml
```

### Endpoint'ы агрегатора

- `GET /health` — liveness / counters
- `GET /ready` — readiness по свежести последнего poll
- `GET /snapshots` — список нормализованных Snapshot
- `GET /snapshots/{token}` — последний snapshot по mint

Если в `auth.api_key` задан ключ, `/snapshots` требует header `X-API-Key`.

Подробности: `docs/aggregator_http_service.md`
