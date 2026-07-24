"""
Atomic straddle construction and teardown via RFQ.

One straddle = 1 ATM call + 1 put (nearest strike to spot) per QTY_PER_LEG BTC.

Entry: RFQ (buy call + buy put) → atomic fill
Exit:  RFQ (sell call + sell put) → atomic fill
Fallback: individual leg chasing if RFQ fails.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Optional

import structlog

import config
from core.exchange import DeriveExchange
from core.portfolio import Portfolio, Straddle, StraddleLeg
from data.market_data import MarketData
from strategy.option_selector import StraddlePair
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)


@dataclass
class UnwindResult:
    """Outcome of a straddle unwind attempt.

    Deliberately does NOT record the close in the portfolio — the caller
    (main._on_close) reconciles against the exchange and only finalises the
    close (or locks on an orphan) once flatness is confirmed. This is the
    reconcile-then-lock fix for the phantom-close bug where a failed maker
    sell used to fall back to the ENTRY price and mark the straddle closed
    while the exchange still held the legs.
    """
    exit_call_price: float
    exit_put_price: float
    call_sold: bool
    put_sold: bool
    atomic: bool  # RFQ atomic sell succeeded (both legs)

    @property
    def both_sold(self) -> bool:
        return self.atomic or (self.call_sold and self.put_sold)


def _spread_pct(bid: float, ask: float) -> float:
    if bid <= 0 or ask <= 0:
        return 1.0
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid if mid > 0 else 1.0


async def build_straddle(
    exchange: DeriveExchange,
    market: MarketData,
    portfolio: Portfolio,
    pair: StraddlePair,
    num_straddles: int,
) -> Optional[Straddle]:
    """
    Execute the atomic entry for N identical straddle units.

    Primary: RFQ for both legs atomically.
    Fallback: individual leg chasing if RFQ produces no quotes.
    """
    from core import notifier

    straddle_id = f"D0-{uuid.uuid4().hex[:8]}"
    total_qty = config.QTY_PER_LEG * num_straddles

    log.info("building_straddle", id=straddle_id, strike=pair.strike,
             call=pair.call.symbol, put=pair.put.symbol, num=num_straddles,
             method="rfq")

    # ── Pre-entry spread gate ──
    call_spread = _spread_pct(pair.call.bid, pair.call.ask)
    put_spread = _spread_pct(pair.put.bid, pair.put.ask)
    if (call_spread > config.OPTION_MAX_ENTRY_SPREAD_PCT
            or put_spread > config.OPTION_MAX_ENTRY_SPREAD_PCT):
        msg = (
            f"Entry spread too wide — call={call_spread:.1%}, "
            f"put={put_spread:.1%}, "
            f"limit={config.OPTION_MAX_ENTRY_SPREAD_PCT:.0%}"
        )
        log.warning("spread_gate_skip", id=straddle_id, msg=msg)
        await notifier.send(
            f"<b>ENTRY SKIPPED — wide spread</b> [{straddle_id}]\n"
            f"Strike: ${pair.strike:,.0f}\n"
            f"  Call:  bid=${pair.call.bid:,.2f}  ask=${pair.call.ask:,.2f}  "
            f"spread={call_spread:.1%}\n"
            f"  Put:   bid=${pair.put.bid:,.2f}  ask=${pair.put.ask:,.2f}  "
            f"spread={put_spread:.1%}\n"
            f"Cap: {config.OPTION_MAX_ENTRY_SPREAD_PCT:.0%}\n"
        )
        return None

    # ── Primary: RFQ atomic entry ──
    rfq_result = await exchange.send_rfq(
        pair.call.symbol, pair.put.symbol, total_qty)

    if rfq_result is not None:
        call_fill = rfq_result["call_price"]
        put_fill = rfq_result["put_price"]
        rfq_id = rfq_result["rfq_id"]

        log.info("rfq_straddle_filled", id=straddle_id, rfq_id=rfq_id,
                 call_price=call_fill, put_price=put_fill)

        call_leg = StraddleLeg(
            instrument=pair.call.symbol, side="Buy",
            qty=total_qty, entry_price=call_fill,
            order_id=rfq_id, avg_fill_price=call_fill,
        )
        put_leg = StraddleLeg(
            instrument=pair.put.symbol, side="Buy",
            qty=total_qty, entry_price=put_fill,
            order_id=rfq_id, avg_fill_price=put_fill,
        )
    else:
        # ── Fallback: individual leg chasing ──
        log.warning("rfq_failed_fallback_to_chase", id=straddle_id)

        call_result = await exchange.chase_buy(
            pair.call.symbol, total_qty, pair.call.bid)
        if call_result is None or call_result.get("order_status") == "partial":
            log.error("call_buy_failed_or_partial", id=straddle_id,
                      symbol=pair.call.symbol, partial=bool(call_result))
            # A partial fill leaves a live long leg — flatten it so we don't
            # leave an orphan behind a failed entry.
            await _emergency_flatten(exchange, pair.call.symbol)
            return None

        call_fill = float(call_result.get("average_price", pair.call.bid))
        log.info("call_filled", id=straddle_id, price=call_fill)

        call_leg = StraddleLeg(
            instrument=pair.call.symbol, side="Buy",
            qty=total_qty, entry_price=call_fill,
            order_id=call_result.get("order_id", ""),
            avg_fill_price=call_fill,
        )

        put_result = await exchange.chase_buy(
            pair.put.symbol, total_qty, pair.put.bid)
        if put_result is None or put_result.get("order_status") == "partial":
            log.error("put_buy_failed_or_partial", id=straddle_id,
                      symbol=pair.put.symbol, partial=bool(put_result))
            # Roll back the filled call AND any partial put leg.
            await _emergency_flatten(exchange, pair.call.symbol)
            await _emergency_flatten(exchange, pair.put.symbol)
            return None

        put_fill = float(put_result.get("average_price", pair.put.bid))
        log.info("put_filled", id=straddle_id, price=put_fill)

        put_leg = StraddleLeg(
            instrument=pair.put.symbol, side="Buy",
            qty=total_qty, entry_price=put_fill,
            order_id=put_result.get("order_id", ""),
            avg_fill_price=put_fill,
        )

    # ── Register ──
    straddle_cost = config.QTY_PER_LEG * (call_fill + put_fill)

    straddle = Straddle(
        id=straddle_id,
        call_leg=call_leg,
        put_leg=put_leg,
        strike=pair.strike,
        qty_per_leg=config.QTY_PER_LEG,
        entry_time=now_utc().isoformat(),
        entry_call_price=call_fill,
        entry_put_price=put_fill,
        straddle_cost=straddle_cost,
        num_straddles=num_straddles,
    )

    portfolio.set_straddle(straddle)
    log.info("straddle_built", id=straddle_id, num=num_straddles,
             cost=f"${straddle_cost * num_straddles:,.2f}",
             call_premium=call_fill, put_premium=put_fill, strike=pair.strike)
    return straddle


async def unwind_straddle(
    exchange: DeriveExchange,
    market: MarketData,
    portfolio: Portfolio,
    reason: str = "hard_close",
) -> Optional[UnwindResult]:
    """
    Attempt to close the open straddle. Returns an ``UnwindResult`` (or
    ``None`` if nothing is open).

    Primary: RFQ sell both legs atomically.
    Fallback: individual leg chasing.

    IMPORTANT: this function no longer records the close in the portfolio.
    It reports exactly which legs actually SOLD so the caller can reconcile
    against exchange truth and only finalise the close once flatness is
    confirmed. On a failed sell the exit price falls back to the last mark /
    entry price purely for reporting; ``call_sold`` / ``put_sold`` tell the
    caller whether that price is real.
    """
    straddle = portfolio.open_straddle
    if straddle is None:
        return None

    log.info("unwinding", id=straddle.id, reason=reason, method="rfq")

    # ── Primary: RFQ atomic exit ──
    rfq_result = await exchange.send_rfq_sell(
        straddle.call_leg.instrument,
        straddle.put_leg.instrument,
        straddle.call_leg.qty,
    )

    if rfq_result is not None:
        exit_call_price = rfq_result["call_price"]
        exit_put_price = rfq_result["put_price"]
        log.info("rfq_unwind_filled", id=straddle.id,
                 call_exit=exit_call_price, put_exit=exit_put_price)
        return UnwindResult(
            exit_call_price=exit_call_price, exit_put_price=exit_put_price,
            call_sold=True, put_sold=True, atomic=True,
        )

    # ── Fallback: individual leg chasing ──
    log.warning("rfq_sell_failed_fallback_to_chase", id=straddle.id)

    call_sold = False
    exit_call_price = straddle.entry_call_price
    _, call_ask = await market.get_option_bid_ask(straddle.call_leg.instrument)
    if call_ask > 0:
        result = await exchange.chase_sell(
            straddle.call_leg.instrument, straddle.call_leg.qty, call_ask)
        if result and result.get("order_status") != "partial":
            exit_call_price = float(result.get("average_price", call_ask))
            call_sold = True
            log.info("call_sold", price=exit_call_price)
        else:
            log.warning("call_sell_failed",
                        instrument=straddle.call_leg.instrument,
                        partial=bool(result))

    put_sold = False
    exit_put_price = straddle.entry_put_price
    _, put_ask = await market.get_option_bid_ask(straddle.put_leg.instrument)
    if put_ask > 0:
        result = await exchange.chase_sell(
            straddle.put_leg.instrument, straddle.put_leg.qty, put_ask)
        if result and result.get("order_status") != "partial":
            exit_put_price = float(result.get("average_price", put_ask))
            put_sold = True
            log.info("put_sold", price=exit_put_price)
        else:
            log.warning("put_sell_failed",
                        instrument=straddle.put_leg.instrument,
                        partial=bool(result))

    return UnwindResult(
        exit_call_price=exit_call_price, exit_put_price=exit_put_price,
        call_sold=call_sold, put_sold=put_sold, atomic=False,
    )


async def _emergency_flatten(exchange: DeriveExchange, instrument: str) -> None:
    """Position-aware rollback of a leg left live by a failed/partial build.

    Reads the ACTUAL exchange position for ``instrument`` and chases it flat
    (sells a long, buys back a short), rather than trusting an assumed qty —
    this cannot oversell into a short. No-op if already flat. In DRY_RUN
    ``list_open_positions`` is empty so this is a safe no-op.
    """
    from core import notifier
    try:
        positions = await exchange.list_open_positions()
        target = next(
            (p for p in positions if p["instrument_name"] == instrument), None,
        )
        amt = float(target.get("amount", 0.0)) if target else 0.0
        if abs(amt) < 1e-9:
            log.info("emergency_flatten_already_flat", instrument=instrument)
            return
        ticker = await exchange.get_ticker(instrument)
        if amt > 0:
            initial = ticker.bid if ticker.bid > 0 else ticker.ask
            result = await exchange.chase_sell(instrument, abs(amt), initial)
        else:
            initial = ticker.ask if ticker.ask > 0 else ticker.bid
            result = await exchange.chase_buy(instrument, abs(amt), initial)
        if result and result.get("order_status") != "partial":
            log.info("emergency_flatten_done", instrument=instrument, amt=amt)
        else:
            log.error("emergency_flatten_incomplete",
                      instrument=instrument, amt=amt, partial=bool(result))
            await notifier.send(
                f"<b>⚠️ MANUAL ACTION REQUIRED</b>\n"
                f"Emergency flatten of <code>{instrument}</code> did not "
                f"complete (residual {amt:+.4f}). Check & flatten manually."
            )
    except Exception:
        log.error("emergency_flatten_failed", instrument=instrument,
                  exc_info=True)
