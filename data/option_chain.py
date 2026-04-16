"""
Option chain management for Derive.

Fetches BTC options, filters to 0DTE, and provides strike/instrument lookup.
Derive instrument naming: BTC-DDMMYYYY-STRIKE-C or BTC-DDMMYYYY-STRIKE-P

Uses bulk get_tickers for efficiency instead of per-instrument polling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import structlog

import config
from core.exchange import DeriveExchange
from utils.time_utils import today_expiry_api_str, today_expiry_date_str

log = structlog.get_logger(__name__)


@dataclass
class OptionInfo:
    symbol: str
    strike: float
    option_type: str   # "C" or "P"
    bid: float = 0.0
    ask: float = 0.0
    mark: float = 0.0


class OptionChain:
    """Maintains the 0DTE option chain for BTC."""

    def __init__(self, exchange: DeriveExchange) -> None:
        self._exchange = exchange
        self.calls: list[OptionInfo] = []
        self.puts: list[OptionInfo] = []

    async def refresh(self) -> int:
        """
        Fetch all BTC 0DTE option tickers in a single bulk call.

        Returns the total number of 0DTE instruments found.
        """
        expiry_api = today_expiry_api_str()
        expiry_name = today_expiry_date_str()
        self.calls.clear()
        self.puts.clear()

        tickers = await self._exchange.get_tickers_for_expiry(
            currency=config.BASE_COIN, expiry_date=expiry_api,
        )

        if not tickers:
            log.warning("no_tickers_for_expiry", expiry=expiry_api)
            return 0

        for name, ticker in tickers.items():
            if not name.startswith(f"{config.BASE_COIN}-"):
                continue

            parts = name.split("-")
            if len(parts) != 4:
                continue

            _, date_str, strike_str, opt_type = parts
            if date_str != expiry_name:
                continue

            try:
                strike = float(strike_str)
            except ValueError:
                continue

            info = OptionInfo(
                symbol=name,
                strike=strike,
                option_type=opt_type,
                bid=ticker.bid,
                ask=ticker.ask,
                mark=ticker.mark,
            )

            if opt_type == "C":
                self.calls.append(info)
            elif opt_type == "P":
                self.puts.append(info)

        self.calls.sort(key=lambda x: x.strike)
        self.puts.sort(key=lambda x: x.strike)

        total = len(self.calls) + len(self.puts)
        log.info("chain_refreshed", expiry=expiry_name,
                 calls=len(self.calls), puts=len(self.puts))
        return total
