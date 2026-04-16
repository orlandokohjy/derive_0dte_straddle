"""
Atomic straddle construction and teardown.

One straddle = 1 ITM call + 1 put (same strike) per QTY_PER_LEG BTC.

Entry: call first (GTC limit at bid — maker) → put (GTC limit at bid — maker)
Exit:  call first (GTC limit at ask — maker) → put (GTC limit at ask — maker)
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

    1. Buy call: QTY_PER_LEG × num_straddles BTC
    2. Buy put:  QTY_PER_LEG × num_straddles BTC
    3. Register straddle in portfolio
    """
    straddle_id = f"D0-{uuid.uuid4().hex[:8]}"
    total_qty = config.QTY_PER_LEG * num_straddles

    log.info("building_straddle", id=straddle_id, strike=pair.strike,
             call=pair.call.symbol, put=pair.put.symbol, num=num_straddles)

    # ── Step 1: Buy call (GTC limit at bid — maker) ──
    call_result = await exchange.chase_buy(pair.call.symbol, total_qty, pair.call.bid)
    if call_result is None:
        log.error("call_buy_failed", id=straddle_id, symbol=pair.call.symbol)
        return None

    call_fill = float(call_result.get("average_price", pair.call.bid))
    call_order_id = call_result.get("order_id", "")
    log.info("call_filled", id=straddle_id, price=call_fill, order_id=call_order_id)

    call_leg = StraddleLeg(
        instrument=pair.call.symbol, side="Buy",
        qty=total_qty, entry_price=call_fill,
        order_id=call_order_id, avg_fill_price=call_fill,
    )

    # ── Step 2: Buy put (GTC limit at bid — maker) ──
    put_result = await exchange.chase_buy(pair.put.symbol, total_qty, pair.put.bid)
    if put_result is None:
        log.error("put_buy_failed", id=straddle_id, symbol=pair.put.symbol)
        await _emergency_sell(exchange, pair.call.symbol, total_qty, call_fill)
        return None

    put_fill = float(put_result.get("average_price", pair.put.bid))
    put_order_id = put_result.get("order_id", "")
    log.info("put_filled", id=straddle_id, price=put_fill, order_id=put_order_id)

    put_leg = StraddleLeg(
        instrument=pair.put.symbol, side="Buy",
        qty=total_qty, entry_price=put_fill,
        order_id=put_order_id, avg_fill_price=put_fill,
    )

    # ── Step 3: Register ──
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
    Close the open straddle: sell call first, then sell put.
    Returns the P&L.
    """
    straddle = portfolio.open_straddle
    if straddle is None:
        return 0.0

    log.info("unwinding", id=straddle.id, reason=reason)

    # ── Sell call (GTC limit at ask — maker) ──
    exit_call_price = straddle.entry_call_price
    _, call_ask = await market.get_option_bid_ask(straddle.call_leg.instrument)
    if call_ask > 0:
        result = await exchange.chase_sell(
            straddle.call_leg.instrument, straddle.call_leg.qty, call_ask,
        )
        if result:
            exit_call_price = float(result.get("average_price", call_ask))
            log.info("call_sold", price=exit_call_price)
        else:
            log.warning("call_sell_failed", instrument=straddle.call_leg.instrument)
    else:
        log.warning("call_no_ask", instrument=straddle.call_leg.instrument)

    # ── Sell put (GTC limit at ask — maker) ──
    exit_put_price = straddle.entry_put_price
    _, put_ask = await market.get_option_bid_ask(straddle.put_leg.instrument)
    if put_ask > 0:
        result = await exchange.chase_sell(
            straddle.put_leg.instrument, straddle.put_leg.qty, put_ask,
        )
        if result:
            exit_put_price = float(result.get("average_price", put_ask))
            log.info("put_sold", price=exit_put_price)
        else:
            log.warning("put_sell_failed", instrument=straddle.put_leg.instrument)
    else:
        log.warning("put_no_ask", instrument=straddle.put_leg.instrument)

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
