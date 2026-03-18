from __future__ import annotations

import html
from typing import Any

import aiohttp

from ..models import AlertEvent


def _fmt_usd(value: Any) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "n/a"
    abs_num = abs(num)
    if abs_num >= 1_000_000_000:
        return f"${num/1_000_000_000:.2f}B"
    if abs_num >= 1_000_000:
        return f"${num/1_000_000:.2f}M"
    if abs_num >= 1_000:
        return f"${num/1_000:.1f}K"
    if abs_num >= 1:
        return f"${num:,.0f}"
    if abs_num >= 0.01:
        return f"${num:,.4f}"
    return f"${num:.8f}"


def _fmt_num(value: Any, digits: int = 2, suffix: str = "") -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{num:.{digits}f}{suffix}"


def _confidence(score: float) -> str:
    if score >= 0.75:
        return "HIGH"
    if score >= 0.65:
        return "MEDIUM"
    return "LOW"


def _status(metrics: dict[str, Any]) -> str:
    ret_5m = _to_float(metrics.get("ret_5m_pct"))
    ret_1m = _to_float(metrics.get("ret_1m_pct"))
    if ret_5m is not None and ret_5m >= 20:
        return "Late Pump"
    if ret_1m is not None and ret_1m >= 5:
        return "Momentum Spike"
    return "Early Runner"


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        parse_mode: str = "HTML",
        disable_notification: bool = True,
        topic_id: int | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.parse_mode = parse_mode
        self.disable_notification = disable_notification
        self.topic_id = topic_id
        self.api_base = f"https://api.telegram.org/bot{bot_token}"

    async def send(self, event: AlertEvent) -> None:
        message = self._format_message(event)
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": self.parse_mode,
            "disable_notification": self.disable_notification,
            "disable_web_page_preview": True,
        }
        if self.topic_id is not None:
            payload["message_thread_id"] = self.topic_id
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.api_base}/sendMessage", json=payload, timeout=15) as resp:
                body = await resp.text()
                if resp.status >= 300:
                    raise RuntimeError(f"Telegram error {resp.status}: {body}")

    def _format_message(self, event: AlertEvent) -> str:
        if event.event == "reject":
            return self._format_reject(event)
        return self._format_runner(event)

    def _format_runner(self, event: AlertEvent) -> str:
        m = event.metrics
        header = "🚀 <b>RUNNER ALERT</b>" if event.event == "runner_alert" else "🟡 <b>RUNNER CANDIDATE</b>"
        smart_quality = _to_float(m.get("smart_quality_avg")) or 0.0
        smart_buys = _to_float(m.get("smart_buys_observed_window")) or 0.0
        smart_money = "✅" if smart_quality >= 0.25 or smart_buys >= 1 else "❌"
        lines = [
            header,
            "",
            f"🪙 <b>{html.escape(event.symbol)}</b>",
            f"💰 Price: {_fmt_usd(m.get('current_price'))}",
            f"💧 Liquidity: {_fmt_usd(m.get('liq_usd'))}",
            f"🏦 FDV: {_fmt_usd(m.get('fdv_usd'))}",
            "",
            f"📈 Momentum: {_fmt_num(m.get('ret_5m_pct'), 1, '%')} (5m)",
            f"⚡ Trades: {_fmt_num(m.get('trades_per_min_5m'), 1, '/min')}",
            f"🔥 Volume: {_fmt_usd(m.get('volume_equiv_15m_usd'))} (15m)",
            "",
            f"🧠 Smart Money: {smart_money}",
            f"📊 Impact (5k): {_fmt_num(m.get('price_impact_buy_5000_pct'), 2, '%')}",
            "",
            f"🎯 Score: <b>{event.score_final:.2f}</b>",
            f"🟢 Confidence: <b>{_confidence(event.score_final)}</b>",
            f"🔎 Status: <b>{html.escape(_status(m))}</b>",
        ]
        if event.penalties:
            penalty_text = ", ".join(f"{html.escape(str(p['code']))} ({float(p['delta']):+.2f})" for p in event.penalties)
            lines.extend(["", f"⚠️ Penalties: {penalty_text}"])
        if event.reason_codes:
            reason_text = ", ".join(html.escape(code) for code in event.reason_codes)
            lines.extend(["", f"📝 Reasons: {reason_text}"])
        return "\n".join(lines)

    def _format_reject(self, event: AlertEvent) -> str:
        lines = [
            "⛔ <b>REJECT</b>",
            f"🪙 <b>{html.escape(event.symbol)}</b>",
            f"🎯 Score: <b>{event.score_final:.2f}</b>",
        ]
        if event.reason_codes:
            lines.append("📝 Reasons: " + ", ".join(html.escape(code) for code in event.reason_codes))
        if event.failed_veto_rules:
            lines.append("🧱 Failed veto: " + ", ".join(html.escape(code) for code in event.failed_veto_rules))
        return "\n".join(lines)
