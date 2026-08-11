"""Offline tests for persistent post-close re-flatten (no orphan lock)."""
from __future__ import annotations

import asyncio
import types

import config
from main import Algo


class _Leg:
    def __init__(self, instrument: str, entry: float = 100.0):
        self.instrument = instrument
        self.entry_price = entry


class _Straddle:
    def __init__(self):
        self.id = "D0-test"
        self.call_leg = _Leg("BTC-20260811-65000-C", 120.0)
        self.put_leg = _Leg("BTC-20260811-64000-P", 110.0)
        self.entry_call_price = 120.0
        self.entry_put_price = 110.0


def _run(coro):
    return asyncio.run(coro)


def test_persist_keeps_going_until_flat(monkeypatch):
    """With PERSIST=true the loop must not stop at the soft budget."""
    original = config.CLOSE_FLATTEN_PERSIST
    budget = config.CLOSE_FLATTEN_BUDGET_MIN
    round_min = config.CLOSE_FLATTEN_ROUND_MIN
    taker_after = config.CLOSE_FLATTEN_TAKER_AFTER_ROUNDS
    config.CLOSE_FLATTEN_PERSIST = True
    config.CLOSE_FLATTEN_BUDGET_MIN = 0.0  # soft budget already elapsed
    config.CLOSE_FLATTEN_ROUND_MIN = 0.0
    config.CLOSE_FLATTEN_TAKER_AFTER_ROUNDS = 0

    calls = {"n": 0}
    sent: list[str] = []

    class _Ex:
        async def list_open_positions(self):
            calls["n"] += 1
            if calls["n"] < 3:
                return [{"instrument_name": "BTC-20260811-65000-C",
                         "amount": 0.1}]
            return []

        async def taker_flatten(self, inst, amt):
            return {"average_price": 50.0, "order_id": "x"}

        async def get_ticker(self, inst):
            raise AssertionError("maker path should not run past soft budget")

        async def chase_sell(self, *a, **k):
            raise AssertionError("maker chase should not run")

        async def chase_buy(self, *a, **k):
            raise AssertionError("maker chase should not run")

    async def _send(msg):
        sent.append(msg)

    algo = Algo.__new__(Algo)
    algo.exchange = _Ex()
    monkeypatch.setattr("main.notifier.send", _send)

    # Avoid real sleeps in the loop.
    async def _sleep(_):
        return None

    monkeypatch.setattr("main.asyncio.sleep", _sleep)

    flat, overrides = _run(
        algo._flatten_residual_until_flat(_Straddle())
    )

    config.CLOSE_FLATTEN_PERSIST = original
    config.CLOSE_FLATTEN_BUDGET_MIN = budget
    config.CLOSE_FLATTEN_ROUND_MIN = round_min
    config.CLOSE_FLATTEN_TAKER_AFTER_ROUNDS = taker_after

    assert flat is True
    assert overrides.get("BTC-20260811-65000-C") == 50.0
    assert calls["n"] >= 3
    assert any("CONTINUING" in m or "RE-FLATTEN" in m for m in sent)


def test_non_persist_stops_after_budget(monkeypatch):
    original = config.CLOSE_FLATTEN_PERSIST
    budget = config.CLOSE_FLATTEN_BUDGET_MIN
    round_min = config.CLOSE_FLATTEN_ROUND_MIN
    config.CLOSE_FLATTEN_PERSIST = False
    config.CLOSE_FLATTEN_BUDGET_MIN = 0.0
    config.CLOSE_FLATTEN_ROUND_MIN = 0.0

    class _Ex:
        async def list_open_positions(self):
            return [{"instrument_name": "BTC-20260811-65000-C", "amount": 0.1}]

        async def taker_flatten(self, inst, amt):
            return None

        async def get_ticker(self, inst):
            return types.SimpleNamespace(bid=1.0, ask=2.0, mark=1.5)

        async def chase_sell(self, *a, **k):
            return None

        async def chase_buy(self, *a, **k):
            return None

    async def _send(msg):
        return None

    async def _sleep(_):
        return None

    algo = Algo.__new__(Algo)
    algo.exchange = _Ex()
    monkeypatch.setattr("main.notifier.send", _send)
    monkeypatch.setattr("main.asyncio.sleep", _sleep)

    flat, _ = _run(algo._flatten_residual_until_flat(_Straddle()))

    config.CLOSE_FLATTEN_PERSIST = original
    config.CLOSE_FLATTEN_BUDGET_MIN = budget
    config.CLOSE_FLATTEN_ROUND_MIN = round_min

    assert flat is False


def test_taker_flatten_mark_fallback_no_bid(monkeypatch):
    from core.exchange import DeriveExchange

    placed: list[dict] = []

    class _Ticker:
        bid = 0.0
        ask = 10.0
        mark = 8.0

    ex = DeriveExchange.__new__(DeriveExchange)

    async def _ticker(_inst):
        return _Ticker()

    async def _place(inst, direction, qty, price, post_only=False):
        placed.append({
            "inst": inst, "direction": direction, "qty": qty,
            "price": price, "post_only": post_only,
        })
        return {"order_id": "oid", "average_price": price}

    monkeypatch.setattr(ex, "get_ticker", _ticker)
    monkeypatch.setattr(ex, "_place_limit_order", _place)

    result = _run(ex.taker_flatten("BTC-20260811-65000-C", 0.1))
    assert result is not None
    assert placed[0]["direction"] == "sell"
    assert placed[0]["post_only"] is False
    # 50% of mark, floored at OPTION_TICK_SIZE
    assert placed[0]["price"] == max(4.0, config.OPTION_TICK_SIZE)
