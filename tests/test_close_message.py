"""OKX-parity SESSION CLOSE message for Derive."""
from __future__ import annotations

from types import SimpleNamespace

from core.notifier import _format_close_message


def _straddle(**overrides):
    base = dict(
        id="D0-abc123",
        strike=64000.0,
        put_strike=63500.0,
        qty_per_leg=0.1,
        num_straddles=1,
        entry_call_price=277.0,
        entry_put_price=162.0,
        exit_call_price=200.0,
        exit_put_price=100.0,
        call_leg=SimpleNamespace(instrument="BTC-20260805-64000-C"),
        put_leg=SimpleNamespace(instrument="BTC-20260805-63500-P"),
        pnl=-13.90,
        fees=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_rich_close_shows_both_strikes_and_leg_pnl():
    body = _format_close_message(
        pnl=-13.90,
        straddle=_straddle(),
        equity_before=864.17,
        equity_after=850.27,
    )
    assert "SESSION CLOSE" in body
    assert "ID: D0-abc123" in body
    assert "C $64,000" in body and "P $63,500" in body
    assert "OTM strangle" in body
    assert "Qty: 0.1000 BTC/leg" in body
    assert "BTC-20260805-64000-C" in body
    assert "Entry: $277.00 ($27.70)" in body
    assert "Exit:  $200.00 ($20.00)" in body
    assert "Leg P&amp;L:" in body
    assert "Gross P&amp;L:" in body
    assert "Net P&amp;L:" in body
    assert "Equity: $864.17 → $850.27" in body


def test_legacy_fallback_without_straddle():
    body = _format_close_message(pnl=12.34)
    assert body == "<b>SESSION CLOSE</b>\nNet P&amp;L: +$12.34\n"


def test_same_strike_shows_single_strike_line():
    body = _format_close_message(
        pnl=1.0,
        straddle=_straddle(put_strike=64000.0),
    )
    assert "Strike: $64,000" in body
    assert "OTM strangle" not in body
