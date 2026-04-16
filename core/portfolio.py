"""
Equity tracking, position state, and trade logging.

Compound sizing: equity grows/shrinks with each trade's realised P&L.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass
from typing import Optional

import structlog

import config
from utils.time_utils import now_utc

log = structlog.get_logger(__name__)

TRADE_LOG_FIELDS = [
    "date", "entry_time", "exit_time", "exit_reason",
    "num_straddles", "strike",
    "call_premium_entry", "call_premium_exit",
    "put_premium_entry", "put_premium_exit",
    "total_capital_used", "straddle_cost", "capital_before",
    "call_pnl", "put_pnl", "gross_pnl", "fees", "net_pnl",
    "capital_after",
]


@dataclass
class StraddleLeg:
    instrument: str
    side: str
    qty: float
    entry_price: float
    order_id: str = ""
    avg_fill_price: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Straddle:
    id: str
    call_leg: StraddleLeg
    put_leg: StraddleLeg
    strike: float
    qty_per_leg: float
    entry_time: str
    entry_call_price: float
    entry_put_price: float
    straddle_cost: float
    num_straddles: int

    status: str = "open"
    exit_time: Optional[str] = None
    exit_call_price: Optional[float] = None
    exit_put_price: Optional[float] = None
    pnl: Optional[float] = None

    def call_pnl(self, call_now: float) -> float:
        return self.qty_per_leg * (call_now - self.entry_call_price) * self.num_straddles

    def put_pnl(self, put_now: float) -> float:
        return self.qty_per_leg * (put_now - self.entry_put_price) * self.num_straddles

    def combined_pnl(self, call_now: float, put_now: float) -> float:
        return self.call_pnl(call_now) + self.put_pnl(put_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "call_leg": self.call_leg.to_dict(),
            "put_leg": self.put_leg.to_dict(),
            "strike": self.strike,
            "qty_per_leg": self.qty_per_leg,
            "entry_time": self.entry_time,
            "entry_call_price": self.entry_call_price,
            "entry_put_price": self.entry_put_price,
            "straddle_cost": self.straddle_cost,
            "num_straddles": self.num_straddles,
            "status": self.status,
            "exit_time": self.exit_time,
            "exit_call_price": self.exit_call_price,
            "exit_put_price": self.exit_put_price,
            "pnl": self.pnl,
        }


class Portfolio:
    """Tracks equity and the current open straddle (at most one per day)."""

    def __init__(self) -> None:
        self._equity: float = config.INITIAL_CAPITAL_USD
        self._straddle: Optional[Straddle] = None
        self._daily_pnl: float = 0.0
        self._load_equity()

    @property
    def equity(self) -> float:
        return self._equity

    def sync_equity(self, live_equity: float) -> None:
        """Sync internal equity with the live Derive subaccount balance."""
        if live_equity <= 0:
            log.warning("sync_equity_skipped", live_equity=live_equity)
            return
        old = self._equity
        self._equity = live_equity
        self._save_equity()
        log.info("equity_synced", old=f"${old:,.2f}", live=f"${live_equity:,.2f}",
                 delta=f"${live_equity - old:,.2f}")

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def has_open(self) -> bool:
        return self._straddle is not None and self._straddle.status == "open"

    @property
    def open_straddle(self) -> Optional[Straddle]:
        return self._straddle if self.has_open else None

    def set_straddle(self, s: Straddle) -> None:
        self._straddle = s
        self._save_positions()

    def close_straddle(
        self, exit_call_price: float, exit_put_price: float, exit_reason: str,
    ) -> float:
        s = self._straddle
        if s is None or s.status != "open":
            return 0.0

        pnl = s.combined_pnl(exit_call_price, exit_put_price)
        s.status = "closed"
        s.exit_time = now_utc().isoformat()
        s.exit_call_price = exit_call_price
        s.exit_put_price = exit_put_price
        s.pnl = pnl

        self._equity += pnl
        self._daily_pnl += pnl
        self._save_equity()
        self._save_positions()
        self._log_trade(s, exit_reason)

        log.info("straddle_closed", pnl=f"${pnl:,.2f}", equity=f"${self._equity:,.2f}")
        return pnl

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self._straddle = None
        self._save_positions()

    # ──────────────── Persistence ─────────────────────────────────

    def _save_equity(self) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(config.EQUITY_FILE, "w") as f:
            json.dump({"equity": self._equity}, f)

    def _load_equity(self) -> None:
        if os.path.exists(config.EQUITY_FILE):
            try:
                with open(config.EQUITY_FILE) as f:
                    self._equity = json.load(f).get("equity", config.INITIAL_CAPITAL_USD)
                log.info("equity_loaded", equity=self._equity)
            except Exception:
                log.warning("equity_load_failed", exc_info=True)

    def _save_positions(self) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        data = self._straddle.to_dict() if self._straddle else None
        with open(config.POSITIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _log_trade(self, s: Straddle, exit_reason: str) -> None:
        os.makedirs(config.STATE_DIR, exist_ok=True)
        total_capital_used = s.straddle_cost * s.num_straddles

        row = {
            "date": s.entry_time[:10],
            "entry_time": s.entry_time,
            "exit_time": s.exit_time,
            "exit_reason": exit_reason,
            "num_straddles": s.num_straddles,
            "strike": s.strike,
            "call_premium_entry": s.entry_call_price,
            "call_premium_exit": s.exit_call_price,
            "put_premium_entry": s.entry_put_price,
            "put_premium_exit": s.exit_put_price,
            "total_capital_used": round(total_capital_used, 2),
            "straddle_cost": s.straddle_cost,
            "capital_before": self._equity - (s.pnl or 0),
            "call_pnl": s.call_pnl(s.exit_call_price or s.entry_call_price),
            "put_pnl": s.put_pnl(s.exit_put_price or s.entry_put_price),
            "gross_pnl": s.pnl,
            "fees": 0.0,
            "net_pnl": s.pnl,
            "capital_after": self._equity,
        }

        needs_header = not os.path.exists(config.TRADE_LOG_FILE)
        with open(config.TRADE_LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)
