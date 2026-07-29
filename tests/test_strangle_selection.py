"""
Offline unit tests for the Derive long OTM STRANGLE selection.

No network / Derive client required. Run with:
    python -m pytest tests/test_strangle_selection.py
or directly:
    python tests/test_strangle_selection.py

Mirrors the OKX `okx_strangle_btc` selector semantics: buy the nearest
tradable (ask>0) strike strictly ABOVE spot for the call, and the nearest
tradable strike strictly BELOW spot for the put. The two strikes DIFFER.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from data.option_chain import OptionChain, OptionInfo  # noqa: E402
from strategy.option_selector import select_straddle_pair  # noqa: E402


def _chain(strikes, *, ask=10.0, bid=9.0, mark=9.5,
           call_ask_overrides=None, put_ask_overrides=None):
    """Build an OptionChain with a call+put at each strike."""
    call_ask_overrides = call_ask_overrides or {}
    put_ask_overrides = put_ask_overrides or {}
    ch = OptionChain(None)  # exchange unused for selection
    for s in strikes:
        ch.calls.append(OptionInfo(
            symbol=f"BTC-C-{s}", strike=s, option_type="C",
            bid=bid, ask=call_ask_overrides.get(s, ask), mark=mark,
        ))
        ch.puts.append(OptionInfo(
            symbol=f"BTC-P-{s}", strike=s, option_type="P",
            bid=bid, ask=put_ask_overrides.get(s, ask), mark=mark,
        ))
    return ch


def test_picks_next_otm_each_side():
    # spot 65200 → call = 65500 (first above), put = 65000 (first below).
    ch = _chain([64000, 64500, 65000, 65500, 66000])
    pair = select_straddle_pair(ch, 65200)
    assert pair is not None
    assert pair.call.strike == 65500
    assert pair.put.strike == 65000


def test_strikes_differ_and_straddle_spot():
    ch = _chain([64500, 65000, 65500, 66000])
    pair = select_straddle_pair(ch, 65300)
    assert pair is not None
    assert pair.put.strike < 65300 < pair.call.strike
    assert pair.call.strike != pair.put.strike


def test_pair_strike_carries_call_strike():
    """``pair.strike`` is the legacy single-strike field = CALL strike."""
    ch = _chain([64500, 65000, 65500])
    pair = select_straddle_pair(ch, 65100)
    assert pair is not None
    assert pair.strike == pair.call.strike == 65500
    assert pair.put.strike == 65000


def test_spot_exactly_on_a_strike_is_not_selected():
    """Strictly OTM: a strike equal to spot is skipped on BOTH legs."""
    ch = _chain([64500, 65000, 65500])
    pair = select_straddle_pair(ch, 65000)
    assert pair is not None
    assert pair.call.strike == 65500   # 65000 not > spot
    assert pair.put.strike == 64500    # 65000 not < spot


def test_skips_strike_without_tradable_ask():
    # First OTM call 65500 has no ask → step out to 66000. Put unaffected.
    ch = _chain([65000, 65500, 66000], call_ask_overrides={65500: 0.0})
    pair = select_straddle_pair(ch, 65200)
    assert pair is not None
    assert pair.call.strike == 66000
    assert pair.put.strike == 65000


def test_skips_put_strike_without_tradable_ask():
    ch = _chain([64500, 65000, 65500], put_ask_overrides={65000: 0.0})
    pair = select_straddle_pair(ch, 65200)
    assert pair is not None
    assert pair.put.strike == 64500


def test_returns_none_when_no_otm_call():
    # Every listed strike is BELOW spot → no OTM call available.
    ch = _chain([64000, 64500, 65000])
    assert select_straddle_pair(ch, 66000) is None


def test_returns_none_when_no_otm_put():
    ch = _chain([65000, 65500, 66000])
    assert select_straddle_pair(ch, 64000) is None


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
