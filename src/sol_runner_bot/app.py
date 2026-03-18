from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .bot import SolRunnerAlertBot
from .config import load_config
from .dispatcher import EventDispatcher
from .models import RuntimeOptions
from .notifiers import ConsoleNotifier, TelegramNotifier, WebhookNotifier
from .providers import DexJupGmgnMvpProvider, FileReplayProvider, HttpMetricsPollProvider
from .rules_loader import RulesetLoader
from .state_store import SqliteStateStore


def build_provider(config):
    if config.kind == "file_replay":
        if not config.file_path:
            raise ValueError("provider.file_path is required for file_replay")
        return FileReplayProvider(file_path=config.file_path)
    if config.kind == "http_metrics":
        if not config.poll_url:
            raise ValueError("provider.poll_url is required for http_metrics")
        return HttpMetricsPollProvider(
            url=config.poll_url,
            interval_sec=config.poll_interval_sec,
            headers=config.headers,
            timeout_sec=config.timeout_sec,
        )
    if config.kind == "dex_jup_gmgn_mvp":
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
    raise ValueError(f"Unsupported provider.kind: {config.kind}")


def build_dispatcher(config) -> EventDispatcher:
    sinks = [ConsoleNotifier()]
    if config.telegram.enabled:
        if not config.telegram.bot_token or not config.telegram.chat_id:
            raise ValueError("telegram.bot_token and telegram.chat_id are required when telegram.enabled=true")
        sinks.append(
            TelegramNotifier(
                bot_token=config.telegram.bot_token,
                chat_id=config.telegram.chat_id,
                parse_mode=config.telegram.parse_mode,
                disable_notification=config.telegram.disable_notification,
                topic_id=config.telegram.topic_id,
            )
        )
    if config.webhook.enabled:
        if not config.webhook.url:
            raise ValueError("webhook.url is required when webhook.enabled=true")
        sinks.append(
            WebhookNotifier(
                url=config.webhook.url,
                headers=config.webhook.headers,
                timeout_sec=config.webhook.timeout_sec,
            )
        )
    return EventDispatcher(sinks)


async def run(config_path: str) -> None:
    config = load_config(config_path)
    ruleset = RulesetLoader.load(config.ruleset_path)
    store = SqliteStateStore(config.storage.sqlite_path)
    runtime = RuntimeOptions(
        dispatch_rejects=config.runtime.dispatch_rejects,
        dispatch_candidates=config.runtime.dispatch_candidates,
        write_all_events_jsonl=config.runtime.event_log_jsonl,
    )
    bot = SolRunnerAlertBot(ruleset=ruleset, store=store, runtime=runtime)
    dispatcher = build_dispatcher(config)
    provider = build_provider(config.provider)

    try:
        async for snapshot in provider:
            events = bot.process_snapshot(snapshot)
            if events:
                await dispatcher.dispatch(events)
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Solana runner alert bot")
    parser.add_argument("--config", required=True, help="Path to YAML config")
    args = parser.parse_args()
    if not Path(args.config).exists():
        print(f"Config not found: {args.config}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run(args.config))


if __name__ == "__main__":
    main()
