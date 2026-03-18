# HTTP aggregator for DexScreener + Jupiter + optional GMGN enrichment

Этот сервис поднимает локальный HTTP endpoint со снапшотами:

- `GET /health`
- `GET /ready`
- `GET /snapshots`
- `GET /snapshots/{token}`

Он использует уже реализованный provider `dex_jup_gmgn_mvp`, но вместо прямой отправки алертов кеширует последние снапшоты и отдает их по HTTP. Это удобно, если сам бот должен работать через `provider.kind=http_metrics`.

## Что внутри

- discovery через Jupiter Tokens V2 и DexScreener
- primary pair selection через DexScreener `token-pairs`
- impact/depth proxies через Jupiter `swap/v1/quote`
- optional GMGN enrichment через ваш собственный HTTP bridge
- `X-API-Key` защита для `/snapshots`
- readiness check по свежести последнего успешного poll

## Конфиг

Смотри `config/example.aggregator.dex_jup_gmgn.yml`.

Ключевые поля:

- `listen.host`, `listen.port` — адрес сервиса
- `auth.api_key` — если задан, `/snapshots` требует header `X-API-Key`
- `runtime.readiness_max_staleness_sec` — когда `/ready` начинает отдавать `503`
- `provider.*` — те же live-параметры, что и у `dex_jup_gmgn_mvp`

## Запуск агрегатора

```bash
python -m sol_runner_bot.aggregator --config config/example.aggregator.dex_jup_gmgn.yml
```

## Подключение бота

Используй `config/example.bot_http_live_mvp.yml`:

```bash
python -m sol_runner_bot.app --config config/example.bot_http_live_mvp.yml
```

## Примеры запросов

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/ready
curl -H "X-API-Key: $METRICS_API_KEY" http://127.0.0.1:8787/snapshots
curl -H "X-API-Key: $METRICS_API_KEY" "http://127.0.0.1:8787/snapshots?limit=10"
curl -H "X-API-Key: $METRICS_API_KEY" http://127.0.0.1:8787/snapshots/So11111111111111111111111111111111111111112
```

## GMGN

Официальный GMGN trading API требует аппрув и `x-route-key`, а data-доступ у них не является таким же стабильным публичным API, как у DexScreener/Jupiter. Поэтому в этом сервисе GMGN подключается только как **ваш собственный enrichment URL**. Если `gmgn_enrichment_url` пустой, агрегатор все равно работает в live-MVP режиме.
