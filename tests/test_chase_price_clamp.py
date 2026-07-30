"""Regression tests for the chase price clamp.

2026-07-29: the 23:30 UTC session logged 116 consecutive
``chase_buy_cap_reached`` warnings and placed ZERO orders, then expired
silently. Cause: the clamp rounded the buy price UP onto the $5 tick grid
*after* capping it at ``mark * 1.15``, so the price it produced was above the
cap it had just applied (mark=218 -> cap=250.7 -> price=255), and the guard
below rejected it every time. ``chase_sell`` had the mirror bug (rounded down
below ``mark / 1.15``), which could deadlock an EXIT.

These tests assert the clamp is self-consistent: a clamped buy is never above
its cap and a clamped sell is never below its floor, for every mark on a wide
grid — including the marks that expose binary float error.
"""
from __future__ import annotations

import config
from core.exchange import _EPS, _round_price


def _clamped_buy_price(mark: float, ask: float) -> float:
    """Mirror of the clamp in ``chase_buy`` (candidate pinned at the cap)."""
    tick = config.OPTION_TICK_SIZE
    slip_cap = mark * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
    hard_cap = min(slip_cap, ask - tick)
    price = _round_price(hard_cap, "down")
    return tick if price < tick else price


def _clamped_sell_price(mark: float, bid: float) -> float:
    """Mirror of the clamp in ``chase_sell`` (candidate pinned at the floor)."""
    tick = config.OPTION_TICK_SIZE
    slip_floor = mark / config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
    hard_floor = max(slip_floor, bid + tick)
    price = _round_price(hard_floor, "up")
    return tick if price < tick else price


def test_buy_clamp_never_exceeds_slip_cap():
    """A capped buy must be placeable, i.e. at or below its own cap."""
    for mark in range(50, 1001):
        m = float(mark)
        slip_cap = m * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
        # Ask far above the cap: this is the case that pinned the real chase.
        price = _clamped_buy_price(m, ask=m * 2.0)
        assert price <= slip_cap + _EPS, (
            f"mark={m} cap={slip_cap} produced un-placeable buy {price}"
        )


def test_sell_clamp_never_undercuts_slip_floor():
    for mark in range(50, 1001):
        m = float(mark)
        slip_floor = m / config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
        price = _clamped_sell_price(m, bid=m * 0.5)
        assert price >= slip_floor - _EPS, (
            f"mark={m} floor={slip_floor} produced un-placeable sell {price}"
        )


def test_the_exact_deadlocked_quote_from_the_log():
    """mark=218, ask~256 — the quote that produced 116 dead attempts."""
    price = _clamped_buy_price(218.0, ask=256.0)
    slip_cap = 218.0 * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR  # 250.7
    assert price <= slip_cap + _EPS
    assert price == 250.0, f"expected a $250 bid under the $250.7 cap, got {price}"


def test_float_error_mark_does_not_block():
    """mark=200 -> 200*1.15 == 229.999...97; a bare `>` blocked price 230.0."""
    mark = 200.0
    slip_cap = mark * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
    assert slip_cap < 230.0, "precondition: the cap is just under the tick"
    price = _clamped_buy_price(mark, ask=mark * 2.0)
    assert price <= slip_cap + _EPS
    assert not price > slip_cap + _EPS
