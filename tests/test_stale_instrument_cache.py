"""Stale derive-client instrument cache must refresh, not burn the chase.

After the 08:00 UTC 0DTE roll, tickers already see today's strikes
(BTC-YYYYMMDD-…) while the client's InstrumentType.option cache still holds
yesterday's. Orders then die with:

    Instrument 'BTC-…' not found in InstrumentType.option instrument cache.
    … Call fetch_instruments() or fetch_all_instruments() to refresh …

Without a refresh, the chase retries the same miss 6× and reports REJECTED.
"""
from __future__ import annotations

import asyncio

import config
from core.exchange import (
    DeriveExchange,
    TickerSnapshot,
    _is_stale_instrument_cache,
)


def test_detector_matches_the_live_error():
    err = (
        "Instrument 'BTC-20260806-64500-C' not found in InstrumentType.option "
        "instrument cache. Either the name is incorrect, or the local cache "
        "is stale. Call fetch_instruments() or fetch_all_instruments() to "
        "refresh the cache."
    )
    assert _is_stale_instrument_cache(err) is True
    assert _is_stale_instrument_cache("Derive RPC 11000: Insufficient funds") is False
    assert _is_stale_instrument_cache("post_only would cross") is False


def test_chase_buy_refreshes_cache_once_then_fills(monkeypatch):
    """First place → stale-cache; after refresh → fill. Must NOT abort."""
    ex = DeriveExchange()
    calls = {"place": 0, "refresh": 0}

    async def _fake_ticker(instrument):
        return TickerSnapshot(bid=200.0, ask=210.0, mark=205.0, index=64000.0)

    async def _fake_place(instrument, direction, qty, price, post_only=True):
        calls["place"] += 1
        if calls["refresh"] == 0:
            return {
                "rejected_stale_cache": True,
                "error": (
                    "Instrument 'BTC-20260806-64500-C' not found in "
                    "InstrumentType.option instrument cache. … stale. "
                    "Call fetch_instruments()"
                ),
            }
        return {"order_id": "ok1", "order_status": "filled",
                "average_price": "205"}

    async def _fake_refresh():
        calls["refresh"] += 1
        return 120

    monkeypatch.setattr(ex, "get_ticker", _fake_ticker)
    monkeypatch.setattr(ex, "_place_limit_order", _fake_place)
    monkeypatch.setattr(ex, "refresh_option_instruments", _fake_refresh)
    monkeypatch.setattr(config, "OPTION_CHASE_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(config, "DRY_RUN", False)

    result = asyncio.run(ex.chase_buy("BTC-20260806-64500-C", 0.1, 200.0))

    assert result is not None
    assert result.get("order_status") == "filled"
    assert calls["refresh"] == 1, "must refresh exactly once"
    assert calls["place"] >= 2, "must retry after the refresh"
    assert not result.get("rejected_by_exchange"), (
        "a one-shot stale-cache miss must not surface as a hard reject"
    )


def test_chase_sell_also_refreshes(monkeypatch):
    ex = DeriveExchange()
    calls = {"place": 0, "refresh": 0}

    async def _fake_ticker(instrument):
        return TickerSnapshot(bid=200.0, ask=210.0, mark=205.0, index=64000.0)

    async def _fake_place(instrument, direction, qty, price, post_only=True):
        calls["place"] += 1
        if calls["refresh"] == 0:
            return {"rejected_stale_cache": True,
                    "error": "local cache is stale"}
        return {"order_id": "ok1", "order_status": "filled",
                "average_price": "205"}

    async def _fake_refresh():
        calls["refresh"] += 1
        return 120

    monkeypatch.setattr(ex, "get_ticker", _fake_ticker)
    monkeypatch.setattr(ex, "_place_limit_order", _fake_place)
    monkeypatch.setattr(ex, "refresh_option_instruments", _fake_refresh)
    monkeypatch.setattr(config, "OPTION_CHASE_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(config, "DRY_RUN", False)

    result = asyncio.run(ex.chase_sell("BTC-20260806-64500-C", 0.1, 210.0))
    assert result.get("order_status") == "filled"
    assert calls["refresh"] == 1
