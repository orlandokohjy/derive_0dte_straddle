"""Exit manager — triggers straddle unwind at session close."""
from __future__ import annotations

import structlog

from typing import Optional

from core.exchange import DeriveExchange
from core.portfolio import Portfolio
from data.market_data import MarketData
from strategy.straddle_builder import UnwindResult, unwind_straddle

log = structlog.get_logger(__name__)


class ExitManager:
    def __init__(self, exchange: DeriveExchange, market: MarketData,
                 portfolio: Portfolio) -> None:
        self._exchange = exchange
        self._market = market
        self._portfolio = portfolio

    async def hard_close(self, reason: str = "session_close") -> Optional[UnwindResult]:
        """Attempt to unwind the open straddle. Returns the UnwindResult (or
        None if nothing is open). Does NOT record the close or notify — the
        caller reconciles against exchange truth and finalises."""
        if not self._portfolio.has_open:
            log.info("nothing_to_close")
            return None

        return await unwind_straddle(
            self._exchange, self._market, self._portfolio, reason=reason,
        )
