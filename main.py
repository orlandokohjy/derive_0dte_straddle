"""
Derive 0DTE BTC Pure Straddle Algo.

Single daily session: 14:00–18:00 UTC, Mon–Fri.
Position: 1 ITM call + 1 put (same strike) per QTY_PER_LEG BTC.
Compound sizing: 80% of current equity, no cap on straddles.
Maker-only orders with escalating chase on Derive (formerly Lyra).
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys

import structlog

import config
from core import notifier
from core.exchange import DeriveExchange
from core.portfolio import Portfolio
from core.scheduler import Scheduler
from data.market_data import MarketData
from data.option_chain import OptionChain
from risk.risk_manager import RiskManager
from strategy.exit_manager import ExitManager
from strategy.option_selector import select_straddle_pair
from strategy.position_sizer import size_position
from strategy.straddle_builder import build_straddle, unwind_straddle
from utils.logging_config import setup_logging
from utils.time_utils import format_utc_sgt, now_utc
from utils import volume_tracker

log = structlog.get_logger(__name__)


class Algo:
    def __init__(self) -> None:
        self.exchange = DeriveExchange()
        self.chain = OptionChain(self.exchange)
        self.market = MarketData(self.exchange, self.chain)
        self.portfolio = Portfolio()
        self.risk = RiskManager(self.portfolio)
        self.exit_mgr = ExitManager(self.exchange, self.market, self.portfolio)
        self.scheduler = Scheduler()
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        setup_logging()
        log.info("algo_starting", env=config.DERIVE_ENV, dry_run=config.DRY_RUN)

        if not config.DERIVE_WALLET or not config.DERIVE_SESSION_KEY:
            log.error("missing_derive_credentials",
                      hint="Set DERIVE_WALLET and DERIVE_SESSION_KEY in .env")
            sys.exit(1)

        self.exchange.connect()

        spot = await self.exchange.get_spot_price()

        if not config.DRY_RUN:
            live_equity = await self.exchange.get_subaccount_equity()
            if live_equity > 0:
                self.portfolio.sync_equity(live_equity)

        log.info("algo_initialized",
                 spot=f"${spot:,.2f}",
                 equity=f"${self.portfolio.equity:,.2f}")

        await notifier.send(
            f"<b>DERIVE STRADDLE ALGO STARTED</b>\n"
            f"Env: {config.DERIVE_ENV}"
            f"{' (DRY RUN)' if config.DRY_RUN else ''}\n"
            f"Spot: ${spot:,.2f}\n"
            f"Equity: ${self.portfolio.equity:,.2f}\n"
            f"Time: {format_utc_sgt(now_utc())}\n"
        )

        self.scheduler.register_session(
            on_entry=self._on_entry,
            on_close=self._on_close,
            on_report=self._on_report,
            on_weekly_report=self._on_weekly_report,
        )
        self.scheduler.start()

        fire_times = self.scheduler.get_next_fire_times()
        for job_id, ft in fire_times.items():
            if ft:
                log.info("next_fire", job=job_id, time=format_utc_sgt(ft))

        if os.getenv("ENTRY_NOW", "").lower() == "true":
            log.info("immediate_entry_triggered")
            await self._on_entry()

        log.info("algo_running")
        await self._shutdown.wait()

    # ──────────────────── Entry ───────────────────────────────────

    async def _on_entry(self) -> None:
        try:
            await self._run_entry()
        except Exception:
            log.error("entry_error", exc_info=True)
            await notifier.notify_error("Entry", "Unhandled exception — check logs")

    async def _run_entry(self) -> None:
        log.info("session_entry_start")

        api_check = self.risk.check_api_health(self.exchange.error_count)
        if not api_check.allowed:
            log.warning("entry_blocked_api", reason=api_check.reason)
            await notifier.notify_skip(api_check.reason)
            return

        loss_check = self.risk.check_daily_loss()
        if not loss_check.allowed:
            log.warning("entry_blocked_loss", reason=loss_check.reason)
            await notifier.notify_skip(loss_check.reason)
            return

        if self.portfolio.has_open:
            log.warning("already_has_open_straddle")
            return

        total_options = await self.chain.refresh()
        if total_options == 0:
            log.error("no_0dte_options")
            await notifier.notify_skip("No 0DTE options found on Derive")
            return

        spot = await self.exchange.get_spot_price()
        pair = select_straddle_pair(self.chain, spot)
        if pair is None:
            await notifier.notify_skip(f"No valid ITM call + put pair near spot ${spot:,.0f}")
            return

        if not config.DRY_RUN:
            live_equity = await self.exchange.get_subaccount_equity()
            if live_equity > 0:
                self.portfolio.sync_equity(live_equity)

        equity = self.portfolio.equity
        sizing = size_position(equity, pair.call.ask, pair.put.ask)

        if config.NUM_STRADDLES_OVERRIDE > 0:
            sizing.num_straddles = config.NUM_STRADDLES_OVERRIDE
            sizing.total_call_cost = sizing.call_cost_per * sizing.num_straddles
            sizing.total_put_cost = sizing.put_cost_per * sizing.num_straddles
            sizing.total_capital_required = (sizing.total_call_cost + sizing.total_put_cost) * 1.05
            log.info("straddles_override", forced=config.NUM_STRADDLES_OVERRIDE)

        if sizing.num_straddles == 0:
            msg = (
                f"Insufficient capital for even 1 straddle.\n"
                f"Equity: ${equity:,.2f}\n"
                f"Available (80%): ${sizing.available_capital:,.2f}\n"
                f"Straddle cost: ${sizing.straddle_cost:,.2f}"
            )
            log.warning("zero_straddles", msg=msg)
            await notifier.notify_skip(msg)
            return

        entry_check = self.risk.check_entry(sizing.num_straddles, sizing.straddle_cost)
        if not entry_check.allowed:
            log.warning("entry_blocked", reason=entry_check.reason)
            await notifier.notify_skip(entry_check.reason)
            return

        log.info(
            "preflight_check_passed",
            num_straddles=sizing.num_straddles,
            call_cost_per=f"${sizing.call_cost_per:,.2f}",
            put_cost_per=f"${sizing.put_cost_per:,.2f}",
            total_call_cost=f"${sizing.total_call_cost:,.2f}",
            total_put_cost=f"${sizing.total_put_cost:,.2f}",
            total_required=f"${sizing.total_capital_required:,.2f}",
            available=f"${sizing.available_capital:,.2f}",
            headroom=f"${sizing.available_capital - sizing.total_capital_required:,.2f}",
        )

        await notifier.send(
            f"<b>PRE-FLIGHT CHECK</b>\n"
            f"Straddles: {sizing.num_straddles}\n"
            f"Spot: ${spot:,.0f} | Strike: ${pair.strike:,.0f}\n"
            f"\n<b>Per straddle:</b>\n"
            f"  Call cost ({config.QTY_PER_LEG} BTC): ${sizing.call_cost_per:,.2f}\n"
            f"  Put cost ({config.QTY_PER_LEG} BTC): ${sizing.put_cost_per:,.2f}\n"
            f"  Total: ${sizing.straddle_cost:,.2f}\n"
            f"\n<b>All {sizing.num_straddles} straddles:</b>\n"
            f"  Call cost: ${sizing.total_call_cost:,.2f}\n"
            f"  Put cost: ${sizing.total_put_cost:,.2f}\n"
            f"  Total (w/ 5% buffer): ${sizing.total_capital_required:,.2f}\n"
            f"  Available: ${sizing.available_capital:,.2f}\n"
            f"  Headroom: ${sizing.available_capital - sizing.total_capital_required:,.2f}\n"
        )

        straddle = await build_straddle(
            self.exchange, self.market, self.portfolio, pair, sizing.num_straddles,
        )
        if straddle:
            volume_tracker.record_trade(sizing.num_straddles)
            await notifier.notify_entry(
                num_straddles=sizing.num_straddles,
                equity=equity,
                straddle_cost=sizing.straddle_cost,
                strike=pair.strike,
                call_fill=straddle.entry_call_price,
                put_fill=straddle.entry_put_price,
                call_cost_total=straddle.entry_call_price * config.QTY_PER_LEG * sizing.num_straddles,
                put_cost_total=straddle.entry_put_price * config.QTY_PER_LEG * sizing.num_straddles,
            )
            log.info("session_entry_done", num_straddles=sizing.num_straddles)
        else:
            log.error("straddle_build_failed")

    # ──────────────────── Close ───────────────────────────────────

    async def _on_close(self) -> None:
        try:
            equity_before = self.portfolio.equity
            pnl = await self.exit_mgr.hard_close()

            if not config.DRY_RUN:
                live_equity = await self.exchange.get_subaccount_equity()
                if live_equity > 0:
                    self.portfolio.sync_equity(live_equity)

            actual_pnl = self.portfolio.equity - equity_before
            if actual_pnl != 0.0:
                cum_return = (self.portfolio.equity - config.INITIAL_CAPITAL_USD) / config.INITIAL_CAPITAL_USD
                await notifier.notify_daily_summary(
                    self.portfolio.equity, actual_pnl, cum_return,
                )
            self.portfolio.reset_daily()
            log.info("session_close_done", pnl=f"${pnl:,.2f}",
                     actual_pnl=f"${actual_pnl:,.2f}",
                     equity=f"${self.portfolio.equity:,.2f}")
        except Exception:
            log.error("close_error", exc_info=True)
            await notifier.notify_error("Close", "Unhandled exception — check logs")

    # ──────────────────── Daily Report (19:00 UTC) ────────────────

    async def _on_report(self) -> None:
        try:
            await notifier.send_daily_report(self.portfolio.equity)
        except Exception:
            log.error("report_error", exc_info=True)
            await notifier.notify_error("Report", "Daily report failed — check logs")

    # ──────────────────── Weekly Report (Fri 20:00 UTC) ──────────

    async def _on_weekly_report(self) -> None:
        try:
            await notifier.send_weekly_report(self.portfolio.equity)
        except Exception:
            log.error("weekly_report_error", exc_info=True)
            await notifier.notify_error("Weekly Report", "Weekly report failed — check logs")

    # ──────────────────── Shutdown ────────────────────────────────

    async def shutdown(self) -> None:
        log.info("shutdown_initiated")
        await notifier.send("<b>DERIVE STRADDLE ALGO SHUTTING DOWN</b>")

        self.scheduler.stop()

        if self.portfolio.has_open:
            log.warning("closing_remaining_position")
            await unwind_straddle(
                self.exchange, self.market, self.portfolio, reason="shutdown",
            )

        log.info("algo_stopped")
        self._shutdown.set()


async def main() -> None:
    algo = Algo()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(algo.shutdown()))

    try:
        await algo.start()
    except KeyboardInterrupt:
        await algo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
