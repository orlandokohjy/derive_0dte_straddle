"""Risk checks before entry."""
from __future__ import annotations

from dataclasses import dataclass

import structlog

import config
from core.portfolio import Portfolio

log = structlog.get_logger(__name__)


@dataclass
class RiskCheck:
    allowed: bool
    reason: str = ""


class RiskManager:
    def __init__(self, portfolio: Portfolio) -> None:
        self._portfolio = portfolio

    def check_api_health(self, error_count: int) -> RiskCheck:
        if error_count >= config.CIRCUIT_BREAKER_API_ERRORS:
            return RiskCheck(False, f"API error count {error_count} >= {config.CIRCUIT_BREAKER_API_ERRORS}")
        return RiskCheck(True)

    def check_daily_loss(self) -> RiskCheck:
        if config.MAX_DAILY_LOSS_PCT is None:
            return RiskCheck(True)
        pnl_pct = self._portfolio.daily_pnl / self._portfolio.equity if self._portfolio.equity > 0 else 0
        if pnl_pct < -config.MAX_DAILY_LOSS_PCT:
            return RiskCheck(False, f"Daily loss {pnl_pct:.1%} exceeds limit")
        return RiskCheck(True)

    def check_entry(self, num_straddles: int, straddle_cost: float) -> RiskCheck:
        if num_straddles <= 0:
            return RiskCheck(False, "Zero straddles")
        return RiskCheck(True)
