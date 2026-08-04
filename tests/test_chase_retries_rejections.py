"""An exchange rejection must not abandon the whole chase.

2026-07-30: three sessions logged one `order_failed` each and the chase quit
after `attempts: 1`, ~2 s into a 10-minute budget, then reported
`chase_buy_deadline_no_fill` — which reads as "the book never crossed us" and
sent us hunting for a liquidity problem that did not exist. The culprit was a
bare `if not order_id: break` on the first rejection.

Rejections must be retried (they can be transient) and a persistent one must
surface as `rejected_by_exchange` carrying the error string, never as a
deadline no-fill and never as a $0 fill.
"""
from __future__ import annotations

import asyncio

import pytest

import config
from core.exchange import _PLACE_ERROR_ABORT_ATTEMPTS, DeriveExchange, TickerSnapshot


def _exchange(monkeypatch, place_results):
    """A DeriveExchange whose order placement replays `place_results`."""
    ex = DeriveExchange()
    calls = {"n": 0, "prices": []}

    async def _fake_ticker(instrument):
        return TickerSnapshot(bid=200.0, ask=210.0, mark=205.0, index=64000.0)

    async def _fake_place(instrument, direction, qty, price, post_only=True):
        i = calls["n"]
        calls["n"] += 1
        calls["prices"].append(price)
        return place_results[min(i, len(place_results) - 1)]

    async def _fake_wait_fill(order_id, timeout):
        return {"order_status": "filled", "average_price": "205"}

    monkeypatch.setattr(ex, "get_ticker", _fake_ticker)
    monkeypatch.setattr(ex, "_place_limit_order", _fake_place)
    monkeypatch.setattr(ex, "_wait_fill", _fake_wait_fill)
    monkeypatch.setattr(config, "OPTION_CHASE_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(config, "DRY_RUN", False)
    return ex, calls


def test_one_rejection_does_not_end_the_chase(monkeypatch):
    """Reject once, then accept: the chase must go on to fill."""
    ex, calls = _exchange(monkeypatch, [
        {},  # rejected (no order_id) — used to `break` here
        {"order_id": "ok1", "order_status": "filled", "average_price": "205"},
    ])
    result = asyncio.run(ex.chase_buy("BTC-20260731-64500-C", 0.1, 200.0))

    assert result is not None, "a single rejection killed the chase"
    assert result.get("order_status") == "filled"
    assert calls["n"] >= 2, "chase never retried after the rejection"


def test_persistent_rejection_surfaces_the_error(monkeypatch):
    ex, calls = _exchange(monkeypatch, [
        {"error": "INVALID_SIGNATURE_EXPIRY"},
    ])
    result = asyncio.run(ex.chase_buy("BTC-20260731-64500-C", 0.1, 200.0))

    assert result is not None
    assert result.get("rejected_by_exchange") is True
    assert "INVALID_SIGNATURE_EXPIRY" in result.get("error", "")
    assert result.get("average_price") == "0"
    assert calls["n"] == _PLACE_ERROR_ABORT_ATTEMPTS, (
        f"expected {_PLACE_ERROR_ABORT_ATTEMPTS} attempts before giving up, "
        f"got {calls['n']}"
    )


def test_sell_side_also_retries(monkeypatch):
    """The exit path matters more: bailing leaves a live position."""
    ex, calls = _exchange(monkeypatch, [
        {},
        {"order_id": "ok1", "order_status": "filled", "average_price": "205"},
    ])
    result = asyncio.run(ex.chase_sell("BTC-20260731-64500-C", 0.1, 210.0))

    assert result is not None, "a single rejection abandoned an EXIT chase"
    assert result.get("order_status") == "filled"
    assert calls["n"] >= 2


def test_persistent_sell_rejection_surfaces_the_error(monkeypatch):
    ex, _ = _exchange(monkeypatch, [{"error": "SELF_TRADING_DISALLOWED"}])
    result = asyncio.run(ex.chase_sell("BTC-20260731-64500-C", 0.1, 210.0))

    assert result.get("rejected_by_exchange") is True
    assert "SELF_TRADING_DISALLOWED" in result.get("error", "")


def test_empty_place_result_falls_back_to_rejected_label(monkeypatch):
    """Legacy _place_limit_order returned {} with no error key — chase must
    still abort cleanly (as rejected_by_exchange) rather than hang or report
    a no-fill. The production path now also returns {"error": ...}; this
    covers the empty-dict case so a regression can't silently reintroduce
    the old break-on-first-reject behaviour.
    """
    ex, calls = _exchange(monkeypatch, [{}])
    result = asyncio.run(ex.chase_buy("BTC-20260805-64500-C", 0.1, 200.0))

    assert result.get("rejected_by_exchange") is True
    assert result.get("error") == "rejected"
    assert calls["n"] == _PLACE_ERROR_ABORT_ATTEMPTS


def test_rejection_is_reported_as_failed_not_a_bad_fill(monkeypatch):
    """build_straddle must name the rejection, not blame a $0 fill price."""
    from strategy import straddle_builder
    from core import notifier

    sent: list[str] = []

    async def _capture(msg):
        sent.append(msg)

    monkeypatch.setattr(notifier, "send", _capture)

    class _Opt:
        def __init__(self, symbol, strike, bid, ask):
            self.symbol, self.strike = symbol, strike
            self.bid, self.ask = bid, ask

    class _Pair:
        call = _Opt("BTC-20260731-64500-C", 64500.0, 200.0, 210.0)
        put = _Opt("BTC-20260731-64000-P", 64000.0, 140.0, 147.0)

    class _Ex:
        async def chase_buy(self, symbol, qty, price):
            return {"rejected_by_exchange": True,
                    "error": "INVALID_SIGNATURE_EXPIRY", "average_price": "0"}

        async def chase_sell(self, symbol, qty, price):
            return {"order_status": "filled", "average_price": "1"}

        async def list_open_positions(self):
            return []

    class _Pf:
        def set_straddle(self, s):
            pass

    straddle, outcome = asyncio.run(straddle_builder.build_straddle(
        _Ex(), market=None, portfolio=_Pf(), pair=_Pair(), num_straddles=1,
    ))

    assert straddle is None
    assert outcome == "failed"
    assert sent, "a rejected entry sent no Telegram alert"
    assert "REJECTED" in sent[0]
    assert "INVALID_SIGNATURE_EXPIRY" in sent[0]
    assert "$0 fill" not in sent[0], "must not blame a bad fill price"
