"""
Offline tests for the Derive collateral read + equity sync.

Regression cover for the 2026-07-29 incident: an EMPTY subaccount reported a
fictional "Available: $5,242" pre-flight and every order then died on
`Derive RPC 11000: Insufficient funds`. Two defects combined:

  1. get_subaccount_equity() preferred `subaccount_value` (includes position
     mark-to-market, not spendable) and could not distinguish "empty" (0.0)
     from "read failed".
  2. sync_equity() refused to sync a live 0.0, so a stale persisted equity
     survived an empty account and drove sizing.

No network / Derive client required. Run with:
    python -m pytest tests/test_collateral_read.py
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.exchange import DeriveExchange  # noqa: E402


class _State:
    def __init__(self, collaterals_value=None, subaccount_value=None,
                 collaterals=None):
        if collaterals_value is not None:
            self.collaterals_value = collaterals_value
        if subaccount_value is not None:
            self.subaccount_value = subaccount_value
        self.collaterals = collaterals or []
        self.subaccount_id = 67902
        self.margin_type = "PM2"


class _Sub:
    def __init__(self, state, raises=False):
        self.state = state
        self._raises = raises

    def refresh(self):
        if self._raises:
            raise RuntimeError("rpc down")
        return None  # refresh() mutates; returns nothing useful


class _Client:
    def __init__(self, sub):
        self.active_subaccount = sub


def _exchange(state, raises=False):
    ex = DeriveExchange()
    ex._client = _Client(_Sub(state, raises=raises))
    return ex


def _read(ex):
    return asyncio.run(ex.get_subaccount_collateral())


def test_prefers_collaterals_value_over_subaccount_value():
    """subaccount_value includes position MTM and is NOT spendable."""
    ex = _exchange(_State(collaterals_value=560.0, subaccount_value=6553.26))
    assert _read(ex) == 560.0


def test_empty_subaccount_returns_zero_not_none():
    """A genuine 0 must be reported as 0.0 so risk gates can block."""
    ex = _exchange(_State(collaterals_value=0, subaccount_value=0))
    assert _read(ex) == 0.0


def test_read_failure_returns_none():
    """None means 'unknown' — callers must fail CLOSED, not treat as zero."""
    ex = _exchange(_State(collaterals_value=560.0), raises=True)
    assert _read(ex) is None


def test_missing_state_returns_none():
    ex = _exchange(None)
    assert _read(ex) is None


def test_missing_both_fields_returns_none():
    ex = _exchange(_State())
    assert _read(ex) is None


def test_falls_back_to_subaccount_value_when_collateral_absent():
    ex = _exchange(_State(subaccount_value=1234.5))
    assert _read(ex) == 1234.5


def test_equity_wrapper_maps_none_to_zero():
    ex = _exchange(_State(collaterals_value=560.0), raises=True)
    assert asyncio.run(ex.get_subaccount_equity()) == 0.0


def test_sync_equity_accepts_zero_and_rejects_negative():
    """A live 0 MUST overwrite stale equity; negatives are impossible."""
    from core.portfolio import Portfolio
    p = Portfolio.__new__(Portfolio)      # bypass __init__ (touches disk)
    p._equity = 6553.26
    p._save_equity = lambda: None
    p.sync_equity(0.0)
    assert p._equity == 0.0
    p._equity = 500.0
    p.sync_equity(-1.0)
    assert p._equity == 500.0             # rejected, unchanged


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
