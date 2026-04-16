"""
Select the ITM call and its matching put for the pure straddle.

Strategy: pick the nearest strike where the CALL is ITM (strike < spot),
then use the same strike for the put (which will be OTM). This creates
a standard straddle with a slight long-delta bias.
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
    strike: float


def select_straddle_pair(chain: OptionChain, spot: float) -> Optional[StraddlePair]:
    """
    Find the nearest ITM call strike and its matching put.

    ITM call = strike < spot. We pick the closest strike below spot
    that has both a call and a put with valid bid/ask.
    """
    itm_calls = [c for c in chain.calls if c.strike < spot and c.bid > 0]
    if not itm_calls:
        log.warning("no_itm_calls", spot=spot)
        return None

    best_call = max(itm_calls, key=lambda c: c.strike)

    matching_put = None
    for p in chain.puts:
        if p.strike == best_call.strike and p.bid > 0:
            matching_put = p
            break

    if matching_put is None:
        log.warning("no_matching_put", strike=best_call.strike, spot=spot)
        return None

    spread_call = (best_call.ask - best_call.bid) / best_call.bid * 100 if best_call.bid > 0 else 999
    spread_put = (matching_put.ask - matching_put.bid) / matching_put.bid * 100 if matching_put.bid > 0 else 999

    log.info("straddle_pair_selected",
             strike=best_call.strike,
             call_bid=best_call.bid, call_ask=best_call.ask,
             call_spread=f"{spread_call:.1f}%",
             put_bid=matching_put.bid, put_ask=matching_put.ask,
             put_spread=f"{spread_put:.1f}%",
             spot=spot)

    return StraddlePair(
        call=best_call,
        put=matching_put,
        strike=best_call.strike,
    )
