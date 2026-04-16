"""
Compound position sizing for a pure option straddle.

straddle_cost = call_premium + put_premium  (per QTY_PER_LEG BTC)
num_straddles = floor(ALLOC_PCT × equity / buffered_straddle_cost)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import structlog

import config

log = structlog.get_logger(__name__)

SLIPPAGE_BUFFER: float = 0.05


@dataclass
class SizingResult:
    num_straddles: int
    call_cost_per: float
    put_cost_per: float
    straddle_cost: float
    total_call_cost: float
    total_put_cost: float
    total_capital_required: float
    equity: float
    available_capital: float


def size_position(equity: float, call_premium: float, put_premium: float) -> SizingResult:
    """
    Compute sizing based on available equity and option premiums.

    Both legs are pure option buys — no spot margin needed.
    """
    call_cost_per = config.QTY_PER_LEG * call_premium
    put_cost_per = config.QTY_PER_LEG * put_premium
    straddle_cost = call_cost_per + put_cost_per

    if straddle_cost <= 0:
        return SizingResult(
            num_straddles=0, call_cost_per=0, put_cost_per=0,
            straddle_cost=0, total_call_cost=0, total_put_cost=0,
            total_capital_required=0, equity=equity,
            available_capital=config.ALLOC_PCT * equity,
        )

    available = config.ALLOC_PCT * equity
    buffered_cost = straddle_cost * (1 + SLIPPAGE_BUFFER)
    n = math.floor(available / buffered_cost)
    n = max(0, n)

    total_call = call_cost_per * n
    total_put = put_cost_per * n
    total_required = (total_call + total_put) * (1 + SLIPPAGE_BUFFER)

    result = SizingResult(
        num_straddles=n,
        call_cost_per=call_cost_per,
        put_cost_per=put_cost_per,
        straddle_cost=straddle_cost,
        total_call_cost=total_call,
        total_put_cost=total_put,
        total_capital_required=total_required,
        equity=equity,
        available_capital=available,
    )

    log.info(
        "position_sized",
        equity=f"${equity:,.0f}",
        available=f"${available:,.0f}",
        num_straddles=n,
        call_cost_per=f"${call_cost_per:,.2f}",
        put_cost_per=f"${put_cost_per:,.2f}",
        straddle_cost=f"${straddle_cost:,.2f}",
        total_required=f"${total_required:,.2f}",
        buffer=f"{SLIPPAGE_BUFFER:.0%}",
    )
    return result
