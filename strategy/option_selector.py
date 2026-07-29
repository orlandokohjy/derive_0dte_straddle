"""
Select the LONG OTM STRANGLE (call + put at different strikes).

Strategy (parity with the OKX `okx_strangle_btc` stack): buy the next OTM
call (nearest listed strike strictly ABOVE spot with a tradable ask) and the
next OTM put (nearest listed strike strictly BELOW spot with a tradable ask).
Net debit, long volatility, no short legs.

History: the original selector rounded DOWN to an ITM call (long-delta bias);
2026-07-25 it moved to true-ATM (same strike both legs); 2026-07-29 it became
this OTM strangle to match the OKX strangle stacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

from data.option_chain import OptionChain, OptionInfo

log = structlog.get_logger(__name__)


@dataclass
class StraddlePair:
    call: OptionInfo
    put: OptionInfo
    # CALL strike, kept as ``strike`` for compatibility with legacy
    # single-strike call sites. The authoritative per-leg strikes are
    # ``call.strike`` and ``put.strike`` (they DIFFER on a strangle).
    strike: float


def _spread_pct(bid: float, ask: float, mark: float = 0.0) -> float:
    """Bid-ask spread as % of mid (or vs mark if bid is missing on a thin
    book)."""
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2
        return (ask - bid) / mid * 100 if mid > 0 else 999
    if ask > 0 and mark > 0:
        return (ask - mark) / mark * 100
    return 999


def select_straddle_pair(chain: OptionChain, spot: float) -> Optional[StraddlePair]:
    """
    Find the LONG OTM strangle: next OTM call + next OTM put.

    - Call leg: nearest listed strike strictly ABOVE spot with a tradable ask
      (we're buying, so ask > 0 is the liquidity check; bid may be 0 on a thin
      book). This is the first OTM call.
    - Put leg:  nearest listed strike strictly BELOW spot with a tradable ask.
      This is the first OTM put.

    The two legs have DIFFERENT strikes (they straddle spot). Returns None if
    either side has no tradable OTM strike.
    """
    calls_by_strike: dict[float, OptionInfo] = {}
    for c in chain.calls:
        if c.ask > 0 and c.strike not in calls_by_strike:
            calls_by_strike[c.strike] = c
    puts_by_strike: dict[float, OptionInfo] = {}
    for p in chain.puts:
        if p.ask > 0 and p.strike not in puts_by_strike:
            puts_by_strike[p.strike] = p

    calls_above = sorted(s for s in calls_by_strike if s > spot)
    puts_below = sorted((s for s in puts_by_strike if s < spot), reverse=True)
    if not calls_above or not puts_below:
        log.warning("no_otm_strangle", spot=spot,
                    calls_above=calls_above[:5],
                    puts_below=puts_below[:5])
        return None

    best_call = calls_by_strike[calls_above[0]]   # nearest strike above spot
    matching_put = puts_by_strike[puts_below[0]]  # nearest strike below spot

    spread_call = _spread_pct(best_call.bid, best_call.ask, best_call.mark)
    spread_put = _spread_pct(matching_put.bid, matching_put.ask,
                             matching_put.mark)

    log.info("otm_strangle_selected",
             call_strike=best_call.strike,
             put_strike=matching_put.strike,
             call_bid=best_call.bid, call_ask=best_call.ask,
             call_mark=best_call.mark, call_spread=f"{spread_call:.1f}%",
             put_bid=matching_put.bid, put_ask=matching_put.ask,
             put_mark=matching_put.mark, put_spread=f"{spread_put:.1f}%",
             spot=spot)

    return StraddlePair(
        call=best_call,
        put=matching_put,
        strike=best_call.strike,
    )
