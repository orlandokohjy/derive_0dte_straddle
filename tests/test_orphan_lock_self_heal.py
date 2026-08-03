"""
Offline tests for the self-healing orphan entry-lock.

Regression cover for the 2026-08-03 incident: the Derive algo went quiet with

    SKIPPED
    Entry locked: Pre-entry exchange not flat: 1 open position(s) —
    possible orphan, refusing to stack

and could NOT be recovered by a restart, because the startup reconcile re-locks
for as long as the residual exists. A leg with no bid therefore bricked the
stack indefinitely. The OKX stacks had already solved this with a risk-aware
lock (only POSITION/orphan locks auto-release, and only on a clean flat read);
Derive never got the port.

No network / Derive client required. Run with:
    python -m pytest tests/test_orphan_lock_self_heal.py
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402


class _Exchange:
    """Stands in for DeriveExchange.list_open_positions."""

    def __init__(self, positions, raises=False):
        self._positions = positions
        self._raises = raises
        self.calls = 0

    async def list_open_positions(self):
        self.calls += 1
        if self._raises:
            raise RuntimeError("network down")
        return self._positions


class _Portfolio:
    def __init__(self, has_open=False):
        self.has_open = has_open


class _Lockable:
    """Minimal host for the two lock methods lifted off Algo.

    Binding the real functions keeps the test honest — it exercises main.py's
    code, not a paraphrase of it — while avoiding Algo.__init__, which would
    construct a live DeriveExchange.
    """

    def __init__(self, exchange, portfolio):
        self.exchange = exchange
        self.portfolio = portfolio
        self._entry_locked = False
        self._lock_reason = ""
        self._lock_clearable_when_flat = False

    from main import Algo as _Algo  # noqa: E402
    _set_entry_lock = _Algo._set_entry_lock
    _maybe_release_orphan_lock = _Algo._maybe_release_orphan_lock


def _host(positions=None, has_open=False, raises=False):
    return _Lockable(_Exchange(positions or [], raises=raises),
                     _Portfolio(has_open=has_open))


def _run(coro):
    return asyncio.run(coro)


def test_orphan_lock_releases_once_flat():
    """The exact 2026-08-03 lock, then the leg settles → trading resumes."""
    h = _host(positions=[])
    h._set_entry_lock(
        "Pre-entry exchange not flat: 1 open position(s) — possible orphan, "
        "refusing to stack",
        clearable_when_flat=True,
    )
    assert h._entry_locked
    assert _run(h._maybe_release_orphan_lock()) is True
    assert h._entry_locked is False
    assert h._lock_reason == ""
    assert h._lock_clearable_when_flat is False


def test_orphan_lock_stays_while_position_lives():
    h = _host(positions=[{"instrument_name": "BTC-20260803-64000-C",
                          "amount": 0.1}])
    h._set_entry_lock("Pre-entry exchange not flat: 1 open position(s)",
                      clearable_when_flat=True)
    assert _run(h._maybe_release_orphan_lock()) is False
    assert h._entry_locked is True


def test_kill_switch_lock_never_self_heals():
    """Config/self-test/API/stale-state/breaker locks need a human, even flat."""
    for reason in (
        "OPTION_CHASE_DEADLINE_MIN=25.0 does not fit shortest session window",
        "Chase self-test failed: sim_price=$5,000.00 vs mark=$200.00",
        "Could not fetch positions from Derive",
        "Algo state has open straddle but exchange shows flat — stale "
        "positions.json",
        "3 consecutive session failures — restart algo to reset",
    ):
        h = _host(positions=[])
        h._set_entry_lock(reason)                 # clearable defaults to False
        assert _run(h._maybe_release_orphan_lock()) is False, reason
        assert h._entry_locked is True, reason


def test_no_release_when_exchange_flat_but_local_straddle_open():
    """Exchange flat + local open is the STALE-STATE case, not a healed orphan.

    Releasing here would let the next entry stack on a straddle we would then
    "close" with fabricated exit prices — the phantom close the post-close
    orphan path exists to prevent.
    """
    h = _host(positions=[], has_open=True)
    h._set_entry_lock("Post-close exchange NOT flat after re-flatten budget",
                      clearable_when_flat=True)
    assert _run(h._maybe_release_orphan_lock()) is False
    assert h._entry_locked is True


def test_fetch_failure_fails_closed():
    """An unreadable exchange must never be mistaken for a flat one."""
    h = _host(raises=True)
    h._set_entry_lock("Pre-entry exchange not flat: 1 open position(s)",
                      clearable_when_flat=True)
    assert _run(h._maybe_release_orphan_lock()) is False
    assert h._entry_locked is True


def test_flag_off_restores_manual_behaviour():
    h = _host(positions=[])
    h._set_entry_lock("Pre-entry exchange not flat: 1 open position(s)",
                      clearable_when_flat=True)
    original = config.SELF_HEAL_LOCK_ON_FLAT
    config.SELF_HEAL_LOCK_ON_FLAT = False
    try:
        assert _run(h._maybe_release_orphan_lock()) is False
        assert h._entry_locked is True
    finally:
        config.SELF_HEAL_LOCK_ON_FLAT = original


def test_later_kill_switch_cannot_inherit_clearable_flag():
    """Stacked locks: a clearable orphan lock followed by a breaker trip must
    leave the algo NON-clearable, or the kill-switch self-heals by accident."""
    h = _host(positions=[])
    h._set_entry_lock("Pre-entry exchange not flat", clearable_when_flat=True)
    h._set_entry_lock("3 consecutive session failures — restart algo to reset")
    assert h._lock_clearable_when_flat is False
    assert _run(h._maybe_release_orphan_lock()) is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("all orphan-lock self-heal tests passed")
