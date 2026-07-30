"""Loosening the spread cap must not admit legs with no bid.

`OPTION_MAX_ENTRY_SPREAD_PCT` was raised to 3.0 (300 %) for Derive so genuinely
wide 0DTE books can still be traded. `_spread_pct` scores a MISSING quote as a
flat 1.0 (100 %), which a 0.30 cap rejected only by accident — any cap above
1.0 waves it through. Buying a leg with no bid means nothing to sell into on
the way out, so it is refused independently of the percentage cap.
"""
from __future__ import annotations

import asyncio

import pytest

import config
from strategy import straddle_builder


class _Opt:
    def __init__(self, symbol, strike, bid, ask):
        self.symbol, self.strike = symbol, strike
        self.bid, self.ask = bid, ask


class _Pair:
    def __init__(self, call, put):
        self.call, self.put = call, put


def _pair(call_bid=200.0, call_ask=210.0, put_bid=140.0, put_ask=147.0):
    return _Pair(
        _Opt("BTC-20260731-64500-C", 64500.0, call_bid, call_ask),
        _Opt("BTC-20260731-64000-P", 64000.0, put_bid, put_ask),
    )


class _Ex:
    """Fills anything asked of it, so a skip can only come from a gate."""

    def __init__(self):
        self.buys = []

    async def chase_buy(self, symbol, qty, price):
        self.buys.append(symbol)
        return {"order_id": "x", "order_status": "filled",
                "average_price": "205"}

    async def chase_sell(self, symbol, qty, price):
        return {"order_status": "filled", "average_price": "1"}

    async def list_open_positions(self):
        return []


class _Pf:
    def __init__(self):
        self.straddle = None

    def set_straddle(self, s):
        self.straddle = s


@pytest.fixture(autouse=True)
def _mute(monkeypatch):
    from core import notifier
    sent: list[str] = []

    async def _capture(msg):
        sent.append(msg)

    monkeypatch.setattr(notifier, "send", _capture)
    return sent


def _build(pair, ex=None):
    ex = ex or _Ex()
    straddle, outcome = asyncio.run(straddle_builder.build_straddle(
        ex, market=None, portfolio=_Pf(), pair=pair, num_straddles=1,
    ))
    return straddle, outcome, ex


def test_no_bid_is_refused_even_at_a_300pct_cap(monkeypatch, _mute):
    monkeypatch.setattr(config, "OPTION_MAX_ENTRY_SPREAD_PCT", 3.0)
    straddle, outcome, ex = _build(_pair(call_bid=0.0))

    assert straddle is None
    assert outcome == "skipped"
    assert ex.buys == [], "bought a leg that had no bid to exit into"
    assert "no two-sided market" in _mute[0].lower()


def test_no_ask_is_refused_even_at_a_300pct_cap(monkeypatch, _mute):
    monkeypatch.setattr(config, "OPTION_MAX_ENTRY_SPREAD_PCT", 3.0)
    straddle, outcome, ex = _build(_pair(put_ask=0.0))

    assert straddle is None
    assert outcome == "skipped"
    assert ex.buys == []


def test_a_genuinely_wide_but_two_sided_book_now_trades(monkeypatch, _mute):
    """The point of the 300 % cap: a 150 %-spread book must go through."""
    monkeypatch.setattr(config, "OPTION_MAX_ENTRY_SPREAD_PCT", 3.0)
    # bid 40 / ask 260 -> spread = 220/150 = ~147 %
    straddle, outcome, ex = _build(
        _pair(call_bid=40.0, call_ask=260.0, put_bid=40.0, put_ask=260.0))

    assert outcome == "ok", f"wide-but-quoted book was rejected ({outcome})"
    assert straddle is not None
    assert len(ex.buys) == 2


def test_the_old_tight_cap_still_rejects_a_wide_book(monkeypatch, _mute):
    monkeypatch.setattr(config, "OPTION_MAX_ENTRY_SPREAD_PCT", 0.30)
    straddle, outcome, ex = _build(
        _pair(call_bid=40.0, call_ask=260.0, put_bid=40.0, put_ask=260.0))

    assert outcome == "skipped"
    assert ex.buys == []
    assert "wide spread" in _mute[0].lower()
