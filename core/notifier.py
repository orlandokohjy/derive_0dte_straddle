"""Telegram notification helper.

Two channels:
  - Ops chat (TELEGRAM_CHAT_ID): startup, pre-flight, entry, close, errors
  - Report chat (TELEGRAM_REPORT_CHAT_ID): slim daily report only
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

import config

log = structlog.get_logger(__name__)


async def _send_to(bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token or not chat_id:
        log.debug("telegram_disabled", chat_id=chat_id, msg=text[:80])
        return
    try:
        import aiohttp
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
    except Exception:
        log.warning("telegram_send_failed", chat_id=chat_id, exc_info=True)


async def send(text: str) -> None:
    """Send to the ops/testing chat (personal bot)."""
    await _send_to(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, text)


async def send_report(text: str) -> None:
    """Send to the report group chat."""
    bot = config.TELEGRAM_REPORT_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN
    chat = config.TELEGRAM_REPORT_CHAT_ID or config.TELEGRAM_CHAT_ID
    await _send_to(bot, chat, text)


async def notify_entry(
    num_straddles: int, equity: float, straddle_cost: float,
    strike: float, call_fill: float, put_fill: float,
    call_cost_total: float, put_cost_total: float,
    put_strike: float = 0.0,
) -> None:
    """``strike`` is the OTM CALL strike; ``put_strike`` the OTM put strike.
    On a strangle they differ and both are shown; a legacy same-strike
    straddle (put_strike 0 or equal) shows a single line."""
    if put_strike > 0 and abs(put_strike - strike) > 1e-9:
        strike_lines = (
            f"Call strike (OTM): ${strike:,.0f}\n"
            f"Put strike (OTM):  ${put_strike:,.0f}\n"
        )
    else:
        strike_lines = f"Strike: ${strike:,.0f}\n"
    await send(
        f"<b>SESSION ENTRY</b>\n"
        f"Straddles: {num_straddles}\n"
        f"Equity: ${equity:,.2f}\n"
        f"\n<b>Fills</b>\n"
        f"{strike_lines}"
        f"Call premium: ${call_fill:,.2f}\n"
        f"Put premium: ${put_fill:,.2f}\n"
        f"\n<b>Capital used</b>\n"
        f"Call cost: ${call_cost_total:,.2f}\n"
        f"Put cost: ${put_cost_total:,.2f}\n"
        f"Total: ${call_cost_total + put_cost_total:,.2f}\n"
    )


def _fmt_signed_usd(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:,.2f}"


def _format_close_message(
    pnl: float,
    session_label: str = "",
    straddle: object | None = None,
    equity_before: float | None = None,
    equity_after: float | None = None,
) -> str:
    """OKX-parity SESSION CLOSE body.

    Derive premiums are already USD-per-BTC-of-notional (same shape as OKX
    UM), so leg costs are ``premium × qty × num`` with no spot multiplier.
    Falls back to the legacy two-line format when no straddle is supplied.
    """
    header = "<b>SESSION CLOSE</b>"
    if session_label:
        header = f"<b>SESSION CLOSE [{session_label}]</b>"

    if straddle is None:
        return f"{header}\nNet P&amp;L: {_fmt_signed_usd(pnl)}\n"

    s = straddle
    qty = float(getattr(s, "qty_per_leg", 0.0))
    num = int(getattr(s, "num_straddles", 1))
    call_strike = float(getattr(s, "strike", 0.0))
    put_strike = float(getattr(s, "put_strike", 0.0) or call_strike)

    call_leg = getattr(s, "call_leg", None)
    put_leg = getattr(s, "put_leg", None)
    call_inst = getattr(call_leg, "instrument", "?") if call_leg else "?"
    put_inst = getattr(put_leg, "instrument", "?") if put_leg else "?"

    entry_call = float(getattr(s, "entry_call_price", 0.0) or 0.0)
    entry_put = float(getattr(s, "entry_put_price", 0.0) or 0.0)
    exit_call = float(getattr(s, "exit_call_price", 0.0) or 0.0)
    exit_put = float(getattr(s, "exit_put_price", 0.0) or 0.0)

    call_entry_usd = entry_call * qty * num
    call_exit_usd = exit_call * qty * num
    put_entry_usd = entry_put * qty * num
    put_exit_usd = exit_put * qty * num
    call_pnl = call_exit_usd - call_entry_usd
    put_pnl = put_exit_usd - put_entry_usd
    gross = float(getattr(s, "gross_pnl", None) or (call_pnl + put_pnl))
    fees = float(getattr(s, "fees", None) or 0.0)
    net = float(getattr(s, "pnl", pnl) or pnl)

    lines: list[str] = [header]
    lines.append(f"ID: {getattr(s, 'id', '?')}")

    if call_strike > 0 and put_strike > 0 and abs(put_strike - call_strike) > 1e-9:
        lines.append(
            f"Strikes: C ${call_strike:,.0f} / P ${put_strike:,.0f}  "
            f"<i>(OTM strangle)</i>"
        )
    elif call_strike > 0:
        lines.append(f"Strike: ${call_strike:,.0f}")

    qty_line = f"Qty: {qty:.4f} BTC/leg"
    if num != 1:
        qty_line += f" × {num} straddles"
    lines.append(qty_line)
    lines.append("")

    lines.append(f"<b>Call</b> {call_inst}")
    lines.append(f"  Entry: ${entry_call:,.2f} (${call_entry_usd:,.2f})")
    lines.append(f"  Exit:  ${exit_call:,.2f} (${call_exit_usd:,.2f})")
    lines.append(f"  Leg P&amp;L: {_fmt_signed_usd(call_pnl)}")

    lines.append(f"<b>Put</b> {put_inst}")
    lines.append(f"  Entry: ${entry_put:,.2f} (${put_entry_usd:,.2f})")
    lines.append(f"  Exit:  ${exit_put:,.2f} (${put_exit_usd:,.2f})")
    lines.append(f"  Leg P&amp;L: {_fmt_signed_usd(put_pnl)}")
    lines.append("")

    lines.append(
        f"<b>Gross P&amp;L:</b> {_fmt_signed_usd(gross)}  "
        f"<i>(call + put)</i>"
    )
    if fees >= 0:
        lines.append(f"<b>Rebate:</b>    +${fees:,.2f}")
    else:
        lines.append(f"<b>Fees:</b>      -${abs(fees):,.2f}")
    lines.append(f"<b>Net P&amp;L:</b>   {_fmt_signed_usd(net)}")

    if equity_before is not None and equity_after is not None:
        lines.append("")
        lines.append(
            f"Equity: ${equity_before:,.2f} → ${equity_after:,.2f}"
        )

    return "\n".join(lines)


async def notify_close(
    pnl: float,
    exit_reason: str,
    session_label: str = "",
    straddle: object | None = None,
    equity_before: float | None = None,
    equity_after: float | None = None,
) -> None:
    """SESSION CLOSE — OKX-parity rich body when ``straddle`` is supplied."""
    body = _format_close_message(
        pnl,
        session_label=session_label,
        straddle=straddle,
        equity_before=equity_before,
        equity_after=equity_after,
    )
    await send(body)


async def notify_skip(reason: str) -> None:
    await send(f"<b>SKIPPED</b>\n{reason}")


async def notify_error(context: str, message: str) -> None:
    await send(f"<b>ERROR</b> [{context}]\n{message}")


async def notify_daily_summary(equity: float, daily_pnl: float, cum_return: float) -> None:
    await send(
        f"<b>DAILY SUMMARY</b>\n"
        f"Equity: ${equity:,.2f}\n"
        f"Today P&L: ${daily_pnl:,.2f}\n"
        f"Cumulative return: {cum_return:.1%}\n"
    )


async def send_daily_report(equity: float) -> None:
    """Generate and send the slim Trade Summary to the report group chat."""
    from reporting.daily_report import compute_report, format_telegram_summary
    try:
        metrics = compute_report(equity)
        if metrics is None:
            log.info("daily_report_skipped", reason="no trades today")
            return
        await send_report(format_telegram_summary(metrics))
        log.info("daily_report_sent")
    except Exception:
        log.warning("daily_report_failed", exc_info=True)


async def send_weekly_report(equity: float) -> None:
    """Generate and send the weekly report to the report group chat."""
    from reporting.daily_report import compute_weekly_report, format_weekly_report
    try:
        metrics = compute_weekly_report(equity)
        if metrics is None:
            log.info("weekly_report_skipped", reason="no trades this week")
            return
        await send_report(format_weekly_report(metrics))
        log.info("weekly_report_sent")
    except Exception:
        log.warning("weekly_report_failed", exc_info=True)
