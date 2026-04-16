"""Exit manager — triggers straddle unwind at session close."""
from __future__ import annotations

import structlog

from core.exchange import DeriveExchange
from core import notifier
from core.portfolio import Portfolio
from data.market_data import MarketData
from strategy.straddle_builder import unwind_straddle

log = structlog.get_logger(__name__)


class ExitManager:
    def __init__(self, exchange: DeriveExchange, market: MarketData,
                 portfolio: Portfolio) -> None:
        self._exchange = exchange
        self._market = market
        self._portfolio = portfolio

    async def hard_close(self) -> float:
        if not self._portfolio.has_open:
            log.info("nothing_to_close")
            return 0.0

        pnl = await unwind_straddle(
            self._exchange, self._market, self._portfolio, reason="session_close",
        )
        await notifier.notify_close(pnl, "session_close")
        return pnl
