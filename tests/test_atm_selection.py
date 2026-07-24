"""
Offline unit tests for the Derive ATM (nearest-strike) straddle selection.

No network / Derive client required. Run with:
    python -m pytest tests/test_atm_selection.py
or directly:
    python tests/test_atm_selection.py

Mirrors the OKX ATM-wings selector semantics: pick the listed strike
closest to spot with a tradable (ask>0) call AND put; ties break to the
lower strike; the chosen strike may sit ABOVE spot.
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


def test_rounds_to_nearest_below_midpoint():
    # spot 65200, strikes every 500 → nearest is 65000 (200 away vs 300).
    ch = _chain([64000, 64500, 65000, 65500, 66000])
    pair = select_straddle_pair(ch, 65200)
    assert pair is not None
    assert pair.strike == 65000
    assert pair.call.strike == pair.put.strike == 65000


def test_rounds_to_nearest_above_midpoint():
    # spot 65300 → nearest is 65500 (200 away vs 300). Strike ABOVE spot.
    ch = _chain([64500, 65000, 65500, 66000])
    pair = select_straddle_pair(ch, 65300)
    assert pair is not None
    assert pair.strike == 65500


def test_picks_closest_even_when_above_spot():
    # spot 65490, 500-multiples → 65500 (10 away) beats 65000 (490 away).
    ch = _chain([64500, 65000, 65500, 66000])
    pair = select_straddle_pair(ch, 65490)
    assert pair is not None
    assert pair.strike == 65500


def test_tie_breaks_to_lower_strike():
    # spot exactly at midpoint 65250 → equidistant 65000/65500 → lower wins.
    ch = _chain([65000, 65500])
    pair = select_straddle_pair(ch, 65250)
    assert pair is not None
    assert pair.strike == 65000


def test_call_and_put_share_strike():
    ch = _chain([64000, 65000, 66000])
    pair = select_straddle_pair(ch, 64900)
    assert pair is not None
    assert pair.call.strike == pair.put.strike == pair.strike


def test_skips_strike_without_tradable_ask():
    # Nearest strike 65000 has NO call ask → fall back to next-closest 65500.
    ch = _chain([65000, 65500, 66000],
                call_ask_overrides={65000: 0.0})
    pair = select_straddle_pair(ch, 65100)
    assert pair is not None
    assert pair.strike == 65500


def test_returns_none_when_no_common_tradable_strike():
    # Calls tradable only at 65000; puts tradable only at 66000 → no overlap.
    ch = _chain([65000, 66000],
                call_ask_overrides={66000: 0.0},
                put_ask_overrides={65000: 0.0})
    assert select_straddle_pair(ch, 65500) is None


if __name__ == "__main__":
    test_rounds_to_nearest_below_midpoint()
    test_rounds_to_nearest_above_midpoint()
    test_picks_closest_even_when_above_spot()
    test_tie_breaks_to_lower_strike()
    test_call_and_put_share_strike()
    test_skips_strike_without_tradable_ask()
    test_returns_none_when_no_common_tradable_strike()
    print("All Derive ATM-selection tests passed.")
