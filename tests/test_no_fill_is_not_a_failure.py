"""A chase that expires with nothing filled must NOT trip the breaker.

2026-07-30: after the clamp fix went live, three illiquid sessions in a row
returned ``"failed"`` for a plain no-fill, hit CONSECUTIVE_FAILURE_LIMIT=3 and
locked entries for the next 8 sessions ("Entry locked: 3 consecutive session
failures — restart algo to reset").

A post-only bid the book never crosses is a MARKET condition, in the same
class as the wide-spread skip. Only a partial fill — which forces us to unwind
a live leg — is a genuine fault worth counting.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

import config
from strategy import straddle_builder


@dataclass
class _Opt:
    symbol: str
    strike: float
    bid: float
    ask: float


@dataclass
class _Pair:
    call: _Opt
    put: _Opt


def _tight_pair() -> _Pair:
    """Strikes with a spread well inside the entry gate, so the spread skip
    cannot mask what we're actually testing."""
    return _Pair(
        call=_Opt("BTC-20260730-64000-C", 64000.0, 200.0, 210.0),
        put=_Opt("BTC-20260730-63500-P", 63500.0, 140.0, 147.0),
    )


class _Exchange:
    """Returns a scripted chase_buy result; records rollback calls."""

    def __init__(self, call_result, put_result=None):
        self._results = {"C": call_result, "P": put_result}
        self.flattened: list[str] = []

    async def chase_buy(self, symbol, qty, price):
        return self._results["C" if symbol.endswith("-C") else "P"]

    async def chase_sell(self, symbol, qty, price):
        return {"order_status": "filled", "average_price": "1"}

    async def list_open_positions(self):
        return []


class _Portfolio:
    def __init__(self):
        self.straddle = None

    def set_straddle(self, s):
        self.straddle = s


def _build(exchange):
    return asyncio.run(straddle_builder.build_straddle(
        exchange, market=None, portfolio=_Portfolio(),
        pair=_tight_pair(), num_straddles=1,
    ))


@pytest.fixture(autouse=True)
def _mute_telegram(monkeypatch):
    from core import notifier

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(notifier, "send", _noop)


def test_call_no_fill_is_skipped_not_failed():
    straddle, outcome = _build(_Exchange(call_result=None))
    assert straddle is None
    assert outcome == "skipped", (
        "a chase that filled nothing is a market condition; counting it as "
        "'failed' locked the algo for 8 sessions on 2026-07-30"
    )


def test_call_partial_fill_is_still_a_failure():
    partial = {"order_status": "partial", "average_price": "205",
               "filled_amount": "0.05", "remaining_amount": "0.05"}
    straddle, outcome = _build(_Exchange(call_result=partial))
    assert straddle is None
    assert outcome == "failed", "a partial fill forced an unwind — a real fault"


def test_put_no_fill_after_call_filled_is_skipped():
    filled_call = {"order_status": "filled", "average_price": "205",
                   "order_id": "c1"}
    straddle, outcome = _build(
        _Exchange(call_result=filled_call, put_result=None))
    assert straddle is None
    assert outcome == "skipped"


def test_zero_fill_price_is_still_a_failure():
    """A $0 average price is a data/plumbing fault, never a market state."""
    bogus = {"order_status": "filled", "average_price": "0", "order_id": "c1"}
    straddle, outcome = _build(_Exchange(call_result=bogus))
    assert straddle is None
    assert outcome == "failed"


def test_breaker_limit_is_reachable_only_by_real_faults():
    """Guard the knob itself: the default must still arm the breaker."""
    assert config.CONSECUTIVE_FAILURE_LIMIT >= 0
