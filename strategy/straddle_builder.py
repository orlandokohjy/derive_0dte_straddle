"""
Atomic straddle construction and teardown via RFQ.

One straddle = 1 ITM call + 1 put (same strike) per QTY_PER_LEG BTC.

Entry: RFQ (buy call + buy put) → atomic fill
Exit:  RFQ (sell call + sell put) → atomic fill
Fallback: individual leg chasing if RFQ fails.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

import structlog

import config
from core.exchange import DeriveExchange
from core.portfolio import Portfolio, Straddle, StraddleLeg
from data.market_data import MarketData
from strategy.option_selector import StraddlePair
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)


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
    straddle_id = f"D0-{uuid.uuid4().hex[:8]}"
    total_qty = config.QTY_PER_LEG * num_straddles

    log.info("building_straddle", id=straddle_id, strike=pair.strike,
             call=pair.call.symbol, put=pair.put.symbol, num=num_straddles,
             method="rfq")

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
        if call_result is None:
            log.error("call_buy_failed", id=straddle_id, symbol=pair.call.symbol)
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
        if put_result is None:
            log.error("put_buy_failed", id=straddle_id, symbol=pair.put.symbol)
            await _emergency_sell(exchange, pair.call.symbol, total_qty, call_fill)
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
) -> float:
    """
    Close the open straddle.
    Primary: RFQ sell both legs atomically.
    Fallback: individual leg chasing.
    """
    straddle = portfolio.open_straddle
    if straddle is None:
        return 0.0

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
    else:
        # ── Fallback: individual leg chasing ──
        log.warning("rfq_sell_failed_fallback_to_chase", id=straddle.id)

        exit_call_price = straddle.entry_call_price
        _, call_ask = await market.get_option_bid_ask(straddle.call_leg.instrument)
        if call_ask > 0:
            result = await exchange.chase_sell(
                straddle.call_leg.instrument, straddle.call_leg.qty, call_ask)
            if result:
                exit_call_price = float(result.get("average_price", call_ask))
                log.info("call_sold", price=exit_call_price)
            else:
                log.warning("call_sell_failed",
                            instrument=straddle.call_leg.instrument)

        exit_put_price = straddle.entry_put_price
        _, put_ask = await market.get_option_bid_ask(straddle.put_leg.instrument)
        if put_ask > 0:
            result = await exchange.chase_sell(
                straddle.put_leg.instrument, straddle.put_leg.qty, put_ask)
            if result:
                exit_put_price = float(result.get("average_price", put_ask))
                log.info("put_sold", price=exit_put_price)
            else:
                log.warning("put_sell_failed",
                            instrument=straddle.put_leg.instrument)

    pnl = portfolio.close_straddle(exit_call_price, exit_put_price, reason)
    log.info("straddle_unwound", id=straddle.id, reason=reason,
             pnl=f"${pnl:,.2f}", exit_call=exit_call_price, exit_put=exit_put_price)
    return pnl


async def _emergency_sell(exchange: DeriveExchange, instrument: str,
                          qty: float, entry_price: float) -> None:
    """Attempt to sell a leg that was already filled during a failed build."""
    try:
        ticker = await exchange.get_ticker(instrument)
        ask = ticker.ask if ticker.ask > 0 else entry_price
        result = await exchange.chase_sell(instrument, qty, ask)
        if result:
            log.info("emergency_sell_done", instrument=instrument)
        else:
            log.error("emergency_sell_chase_exhausted", instrument=instrument)
    except Exception:
        log.error("emergency_sell_failed", instrument=instrument, exc_info=True)
