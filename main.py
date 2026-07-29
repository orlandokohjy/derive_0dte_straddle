"""
Derive 0DTE BTC Long OTM Strangle Algo.

Multi-session schedule mirroring the OKX ATM-wings timing (no wings).
Position: next OTM call + next OTM put (strikes straddling spot, so they
DIFFER) per QTY_PER_LEG BTC — a net-debit long-volatility strangle.
Compound sizing: 80% of current equity, no cap on straddles.
Maker-only orders with escalating chase on Derive (formerly Lyra); the
post-close reconcile taker-escalates after CLOSE_FLATTEN_TAKER_AFTER_ROUNDS.
"""
from __future__ import annotations

import asyncio
import atexit
import os
import re
import signal
import sys
import time as _time
from typing import Optional

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

_LOCK_PATH = f"{config.STATE_DIR}/algo.pid"


def _process_alive(pid: int) -> bool:
    """True if a process with ``pid`` is currently alive on this host."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except Exception:
        return False
    return True


def _acquire_singleton_lock() -> bool:
    """Refuse to run a second algo instance on the same session key /
    subaccount. Writes our PID to ``state/algo.pid``; if the file already
    holds a LIVE pid, returns False. Stale (dead) pids are overwritten.

    This is the Derive analogue of the OKX singleton lock built after the
    2026-05-07 incident where two processes raced on the same orders and
    left an orphan. Especially important under Docker ``restart: always``.
    """
    if not config.SINGLETON_LOCK_ENABLED:
        return True
    os.makedirs(config.STATE_DIR, exist_ok=True)
    if os.path.exists(_LOCK_PATH):
        try:
            with open(_LOCK_PATH) as f:
                existing = int((f.read() or "0").strip())
        except Exception:
            existing = 0
        if existing and existing != os.getpid() and _process_alive(existing):
            log.error("singleton_lock_held", pid=existing, path=_LOCK_PATH)
            return False
        log.warning("singleton_lock_stale_overwrite", stale_pid=existing)
    with open(_LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_singleton_lock)
    log.info("singleton_lock_acquired", pid=os.getpid(), path=_LOCK_PATH)
    return True


def _release_singleton_lock() -> None:
    """Release the singleton lock iff we still own it."""
    try:
        if not os.path.exists(_LOCK_PATH):
            return
        with open(_LOCK_PATH) as f:
            owner = int((f.read() or "0").strip())
        if owner == os.getpid():
            os.remove(_LOCK_PATH)
            log.info("singleton_lock_released", pid=os.getpid())
    except Exception:
        log.warning("singleton_lock_release_failed", exc_info=True)


def _disable_entry_now_in_env_file(env_path: str = ".env") -> None:
    """Rewrite ENTRY_NOW=true to ENTRY_NOW=false in the local .env file.

    Called immediately after consuming an immediate-entry trigger so that the
    next container restart does NOT fire entry again. Silent no-op if the
    file is missing, read-only, or doesn't contain ENTRY_NOW.
    """
    try:
        if not os.path.exists(env_path):
            log.debug("entry_now_disable_skipped", reason="no_env_file")
            return
        with open(env_path, "r") as f:
            content = f.read()
        new_content = re.sub(
            r"^(\s*ENTRY_NOW\s*=\s*)(true|TRUE|True|1)\b.*$",
            r"\1false",
            content,
            flags=re.MULTILINE,
        )
        if new_content != content:
            with open(env_path, "w") as f:
                f.write(new_content)
            log.info("entry_now_auto_disabled", env_path=env_path)
        else:
            log.debug("entry_now_disable_noop", reason="no_match")
    except Exception:
        log.warning("entry_now_disable_failed", exc_info=True)


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
        # Set by startup reconciliation when exchange state disagrees with
        # local state, or by the consecutive-failure circuit breaker.
        self._entry_locked: bool = False
        self._lock_reason: str = ""
        self._consecutive_failures: int = 0

    async def start(self) -> None:
        setup_logging()
        log.info("algo_starting", env=config.DERIVE_ENV, dry_run=config.DRY_RUN)

        if not config.DERIVE_WALLET or not config.DERIVE_SESSION_KEY:
            log.error("missing_derive_credentials",
                      hint="Set DERIVE_WALLET and DERIVE_SESSION_KEY in .env")
            sys.exit(1)

        if not _acquire_singleton_lock():
            log.error("refusing_second_instance", path=_LOCK_PATH)
            await notifier.send(
                "<b>⚠️ DERIVE ALGO REFUSED TO START</b>\n"
                "Another instance holds the singleton lock "
                f"(<code>{_LOCK_PATH}</code>). Not starting a second copy."
            )
            sys.exit(2)

        if config.RESET_STATE_ON_BOOT:
            self.portfolio.reset_state_on_boot()

        self._validate_chase_deadline_fits_session()

        self.exchange.connect()

        if not config.DRY_RUN:
            await self._startup_cancel_stale_orders()
            await self._startup_reconcile_positions()
            await self._chase_pricing_selftest()

        spot = await self.exchange.get_spot_price()

        if not config.DRY_RUN:
            # None = read failed (keep last known equity); 0.0 = genuinely
            # empty and MUST be synced, or stale equity fakes the pre-flight.
            live_equity = await self.exchange.get_subaccount_collateral()
            if live_equity is None:
                log.warning("startup_equity_read_failed_keeping_persisted",
                            persisted=self.portfolio.equity)
            else:
                self.portfolio.sync_equity(live_equity)

        log.info("algo_initialized",
                 spot=f"${spot:,.2f}",
                 equity=f"${self.portfolio.equity:,.2f}",
                 entry_locked=self._entry_locked)

        lock_line = (f"\n<b>⚠️ ENTRY LOCKED</b>: {self._lock_reason}"
                     if self._entry_locked else "")
        wd_sessions = [s for s in config.SESSION_SCHEDULE
                       if "mon" in s.entry_days]
        we_sessions = [s for s in config.SESSION_SCHEDULE
                       if "sat" in s.entry_days]
        sched_line = (
            f"\nSessions ({len(config.SESSION_SCHEDULE)}):\n"
            f"  • Mon-Fri: "
            f"{', '.join(s.entry_utc.strftime('%H:%M') for s in wd_sessions)}\n"
            f"  • Sat-Sun: "
            f"{', '.join(s.entry_utc.strftime('%H:%M') for s in we_sessions)}\n"
            f"  (30-min holds, no wings)"
        )
        await notifier.send(
            f"<b>DERIVE STRADDLE ALGO STARTED</b>\n"
            f"Env: {config.DERIVE_ENV}"
            f"{' (DRY RUN)' if config.DRY_RUN else ''}\n"
            f"Spot: ${spot:,.2f}\n"
            f"Equity: ${self.portfolio.equity:,.2f}\n"
            f"Time: {format_utc_sgt(now_utc())}"
            f"{sched_line}"
            f"{lock_line}\n"
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
            _disable_entry_now_in_env_file()
            await self._on_entry()

        log.info("algo_running")
        await self._shutdown.wait()

    # ──────────────────── Startup Safeguards ──────────────────────

    def _validate_chase_deadline_fits_session(self) -> None:
        """Lock entries at boot if the entry chase deadline could run past the
        session close. window = close − entry; the chase must finish with a
        safety buffer before close so the exit isn't racing an unfilled entry.
        """
        # Validate against the SHORTEST session window in the multi-session
        # schedule — the chase must finish with a safety buffer before the
        # tightest close, or the exit races an unfilled entry.
        buffer_min = 5
        shortest = min(config.SESSION_SCHEDULE, key=lambda s: s.window_min)
        window_min = shortest.window_min
        if config.OPTION_CHASE_DEADLINE_MIN > max(0, window_min - buffer_min):
            self._entry_locked = True
            self._lock_reason = (
                f"OPTION_CHASE_DEADLINE_MIN={config.OPTION_CHASE_DEADLINE_MIN} "
                f"does not fit shortest session window "
                f"({shortest.name} {window_min}min) − {buffer_min}min "
                f"buffer — lower OPTION_CHASE_DEADLINE_MIN before trading"
            )
            log.error("chase_deadline_exceeds_window",
                      deadline=config.OPTION_CHASE_DEADLINE_MIN,
                      shortest_session=shortest.name,
                      window_min=window_min)
        else:
            log.info("chase_deadline_fits_all_sessions",
                     deadline=config.OPTION_CHASE_DEADLINE_MIN,
                     shortest_session=shortest.name,
                     shortest_window_min=window_min)

    async def _startup_cancel_stale_orders(self) -> None:
        """Cancel any resting orders from a previous run before the scheduler
        starts. Stale orders eat margin and can cause 'Insufficient funds'
        errors on new RFQs/entries (seen 2026-04-20).
        """
        try:
            cancelled = await self.exchange.cancel_all_open_orders()
            if cancelled > 0:
                await notifier.send(
                    f"<b>STARTUP CLEANUP</b>\n"
                    f"Cancelled {cancelled} stale open order(s) from previous run."
                )
        except Exception:
            log.error("startup_cancel_failed", exc_info=True)
            await notifier.notify_error(
                "Startup cleanup",
                "Failed to cancel stale orders — check logs manually")

    async def _startup_reconcile_positions(self) -> None:
        """Compare exchange positions against local positions.json.

        If they disagree, set entry lock to prevent blind re-entry on top
        of a mis-tracked position (which would compound errors).
        """
        try:
            exchange_positions = await self.exchange.list_open_positions()
        except Exception:
            log.error("reconcile_fetch_failed", exc_info=True)
            self._entry_locked = True
            self._lock_reason = "Could not fetch positions from Derive"
            await notifier.notify_error(
                "Startup reconciliation",
                "Failed to fetch exchange positions — entries blocked")
            return

        exchange_has_positions = len(exchange_positions) > 0
        local_has_straddle = self.portfolio.has_open

        log.info("startup_reconcile",
                 exchange_positions=len(exchange_positions),
                 exchange_detail=[f"{p['instrument_name']} {p['amount']:+.4f}"
                                  for p in exchange_positions],
                 local_has_straddle=local_has_straddle)

        if exchange_has_positions and not local_has_straddle:
            details = "\n".join(
                f"  • {p['instrument_name']}  amt={p['amount']:+.4f}  "
                f"avg=${p['average_price']:,.2f}  mark=${p['mark_price']:,.2f}  "
                f"uPnL=${p['unrealized_pnl']:+,.2f}"
                for p in exchange_positions
            )
            self._entry_locked = True
            self._lock_reason = (
                f"Exchange has {len(exchange_positions)} open position(s) "
                f"but algo state is empty — possible orphan"
            )
            await notifier.send(
                f"<b>⚠️ RECONCILIATION MISMATCH</b>\n"
                f"Exchange has open positions but algo state is empty.\n\n"
                f"<b>Exchange positions:</b>\n{details}\n\n"
                f"<b>ACTION</b>: Entry locked until manually resolved.\n"
                f"Either close the positions or update positions.json.\n"
            )
            return

        if local_has_straddle and not exchange_has_positions:
            self._entry_locked = True
            self._lock_reason = (
                "Algo state has open straddle but exchange shows flat — "
                "stale positions.json"
            )
            await notifier.send(
                f"<b>⚠️ RECONCILIATION MISMATCH</b>\n"
                f"Algo state claims open straddle but exchange shows flat.\n\n"
                f"<b>ACTION</b>: Entry locked. Clear state/positions.json "
                f"to reset."
            )
            return

        log.info("startup_reconcile_ok",
                 flat=(not exchange_has_positions and not local_has_straddle),
                 matched_open=(exchange_has_positions and local_has_straddle))

    async def _chase_pricing_selftest(self) -> None:
        """Simulate one maker-chase cap on a live option and lock entries if
        the price violates sanity bounds. Guards against a tick/unit
        regression sending a wild order (the Derive analogue of the OKX
        chase self-test after the 2026-05-07 unit bug).
        """
        if not config.CHASE_SELFTEST_ENABLED:
            return
        try:
            total = await self.chain.refresh()
            if total == 0:
                log.info("selftest_skipped_no_chain")
                return
            spot = await self.exchange.get_spot_price()
            pair = select_straddle_pair(self.chain, spot)
            if pair is None:
                log.info("selftest_skipped_no_pair")
                return
            leg = pair.call
            mark = float(getattr(leg, "mark", 0.0) or 0.0)
            ask = float(leg.ask or 0.0)
            # Price the chase would settle at: walk toward the ask but never
            # above the mark-based slippage cap.
            cap = mark * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR if mark > 0 else ask
            sim_price = min(ask, cap) if (ask > 0 and cap > 0) else max(ask, cap)

            ok_positive = sim_price > 0 and mark > 0
            ok_absolute = sim_price <= config.CHASE_SELFTEST_MAX_ABSOLUTE_USD
            ok_over_mark = (
                mark <= 0 or sim_price <= mark * (1 + config.CHASE_SELFTEST_MAX_OVER_MARK)
            )
            log.info("chase_selftest",
                     symbol=leg.symbol, mark=mark, ask=ask,
                     sim_price=sim_price, ok_positive=ok_positive,
                     ok_absolute=ok_absolute, ok_over_mark=ok_over_mark)
            if not (ok_positive and ok_absolute and ok_over_mark):
                self._entry_locked = True
                self._lock_reason = (
                    f"Chase self-test failed: sim_price=${sim_price:,.2f} "
                    f"vs mark=${mark:,.2f} (abs_ok={ok_absolute}, "
                    f"over_mark_ok={ok_over_mark}) — possible unit/tick bug"
                )
                await notifier.send(
                    f"<b>⚠️ CHASE SELF-TEST FAILED</b>\n"
                    f"Symbol: <code>{leg.symbol}</code>\n"
                    f"mark=${mark:,.2f}  ask=${ask:,.2f}  "
                    f"sim=${sim_price:,.2f}\n"
                    f"Entries LOCKED — investigate pricing/units before "
                    f"unlocking (restart)."
                )
        except Exception:
            # A self-test infra failure should not hard-crash the boot, but
            # we surface it; entries remain allowed only if reconcile passed.
            log.warning("chase_selftest_error", exc_info=True)

    # ──────────────────── Entry ───────────────────────────────────

    async def _on_entry(self) -> None:
        try:
            await self._run_entry()
        except Exception:
            log.error("entry_error", exc_info=True)
            await notifier.notify_error("Entry", "Unhandled exception — check logs")

    async def _run_entry(self) -> None:
        log.info("session_entry_start")

        if self._entry_locked:
            log.warning("entry_blocked_lock", reason=self._lock_reason)
            await notifier.notify_skip(f"Entry locked: {self._lock_reason}")
            return

        api_check = self.risk.check_api_health(
            self.exchange.error_count_effective())
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

        # ── Pre-entry exchange-flat guard (defence-in-depth) ──
        # Local state can be wrong: a failed close can leave the exchange
        # holding legs even though local state is flat. Query the exchange
        # directly and refuse (lock) rather than stacking a new straddle on
        # top of an orphan. Ports the OKX pre-entry flat guard.
        if not config.DRY_RUN:
            try:
                live_positions = await self.exchange.list_open_positions()
            except Exception:
                log.warning("preentry_position_check_failed", exc_info=True)
                live_positions = []
            if live_positions:
                detail = ", ".join(
                    f"{p['instrument_name']} {p['amount']:+.4f}"
                    for p in live_positions
                )
                self._entry_locked = True
                self._lock_reason = (
                    f"Pre-entry exchange not flat: {len(live_positions)} "
                    f"open position(s) — possible orphan, refusing to stack"
                )
                log.error("entry_blocked_exchange_not_flat", positions=detail)
                await notifier.send(
                    f"<b>⚠️ ENTRY BLOCKED — EXCHANGE NOT FLAT</b>\n"
                    f"{len(live_positions)} open position(s): {detail}\n\n"
                    f"<b>ACTION</b>: possible orphan. Entries LOCKED — "
                    f"flatten manually (tools/force_liquidate.py) and restart."
                )
                return

        total_options = await self.chain.refresh()
        if total_options == 0:
            log.error("no_0dte_options")
            await notifier.notify_skip("No 0DTE options found on Derive")
            return

        spot = await self.exchange.get_spot_price()
        pair = select_straddle_pair(self.chain, spot)
        if pair is None:
            await notifier.notify_skip(
                f"No valid OTM strangle (next OTM call + next OTM put) "
                f"near spot ${spot:,.0f}"
            )
            return

        if not config.DRY_RUN:
            live_equity = await self.exchange.get_subaccount_collateral()
            if live_equity is None:
                log.warning("entry_equity_read_failed_keeping_persisted",
                            persisted=self.portfolio.equity)
            else:
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

        # ── Pre-entry collateral check (FAILS CLOSED) ──
        # An unreadable balance and a genuinely EMPTY subaccount must both
        # block the entry. The old gate was `available > 0 and available <
        # required`, so a $0 subaccount (which reads 0) skipped the check
        # entirely and every order then died on 11000 Insufficient funds.
        live_collateral: Optional[float] = None
        if not config.DRY_RUN:
            available = await self.exchange.get_subaccount_collateral()
            live_collateral = available
            required = sizing.total_capital_required \
                * config.COLLATERAL_BUFFER_FACTOR
            if available is None:
                msg = (
                    "Could not read Derive subaccount collateral — "
                    "blocking entry (fail-closed). Check API/session key."
                )
                log.warning("collateral_check_unavailable", msg=msg)
                await notifier.notify_skip(msg)
                return
            if available < required:
                msg = (
                    f"Insufficient collateral on Derive subaccount.\n"
                    f"Available: ${available:,.2f}\n"
                    f"Required (× {config.COLLATERAL_BUFFER_FACTOR:.2f} "
                    f"buffer): ${required:,.2f}"
                )
                log.warning("collateral_check_failed", msg=msg)
                await notifier.notify_skip(msg)
                return
            log.info("collateral_check_ok",
                     available=f"${available:,.2f}",
                     required=f"${required:,.2f}")

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

        # Show the LIVE exchange collateral, not just the equity-derived
        # allocation — these diverged badly once (stale persisted equity
        # printed "Available: $5,242" for an empty subaccount).
        collateral_line = (
            f"  Live collateral: ${live_collateral:,.2f}\n"
            if live_collateral is not None else ""
        )
        await notifier.send(
            f"<b>PRE-FLIGHT CHECK</b>\n"
            f"Straddles: {sizing.num_straddles}\n"
            f"Spot: ${spot:,.0f}\n"
            f"Strikes: C ${pair.call.strike:,.0f} / P "
            f"${pair.put.strike:,.0f}  (OTM strangle)\n"
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
            f"{collateral_line}"
        )

        straddle, outcome = await build_straddle(
            self.exchange, self.market, self.portfolio, pair, sizing.num_straddles,
        )
        if straddle:
            self._consecutive_failures = 0
            volume_tracker.record_trade(sizing.num_straddles)
            await notifier.notify_entry(
                num_straddles=sizing.num_straddles,
                equity=equity,
                straddle_cost=sizing.straddle_cost,
                strike=pair.call.strike,
                put_strike=pair.put.strike,
                call_fill=straddle.entry_call_price,
                put_fill=straddle.entry_put_price,
                call_cost_total=straddle.entry_call_price * config.QTY_PER_LEG * sizing.num_straddles,
                put_cost_total=straddle.entry_put_price * config.QTY_PER_LEG * sizing.num_straddles,
            )
            log.info("session_entry_done", num_straddles=sizing.num_straddles)
        elif outcome == "skipped":
            # Market/account condition (wide spread, no usable collateral) —
            # we chose not to trade. NOT a fault, so it must not feed the
            # circuit breaker; otherwise a run of illiquid 0DTE sessions
            # locks the algo for no reason. The builder already alerted.
            log.info("entry_skipped_not_a_failure", session_outcome=outcome)
        else:
            log.error("straddle_build_failed", session_outcome=outcome)
            self._register_session_failure(f"build_straddle {outcome}")

    # ──────────────────── Failure tracking / circuit breaker ─────

    def _register_session_failure(self, reason: str) -> None:
        """Increment failure counter; lock entries if threshold exceeded.

        ``CONSECUTIVE_FAILURE_LIMIT <= 0`` DISABLES the breaker (OKX parity):
        the counter still increments for logging but entries are never locked.
        """
        self._consecutive_failures += 1
        log.warning("session_failure_recorded",
                    count=self._consecutive_failures,
                    limit=config.CONSECUTIVE_FAILURE_LIMIT, reason=reason)
        if (config.CONSECUTIVE_FAILURE_LIMIT > 0
                and self._consecutive_failures
                >= config.CONSECUTIVE_FAILURE_LIMIT):
            self._entry_locked = True
            self._lock_reason = (
                f"{self._consecutive_failures} consecutive session failures "
                f"— restart algo to reset"
            )
            asyncio.create_task(notifier.send(
                f"<b>⚠️ CIRCUIT BREAKER TRIPPED</b>\n"
                f"{self._consecutive_failures} consecutive session failures.\n"
                f"Entry LOCKED until restart."
            ))

    # ──────────────────── End-of-session reconciliation ──────────

    async def _flatten_residual_until_flat(
        self, straddle,
    ) -> tuple[bool, dict[str, float]]:
        """After the unwind, if the exchange is NOT flat, persistently
        re-flatten residual legs (sell longs / buy back shorts) until the
        account is flat or the wall-clock budget is exhausted.

        Returns ``(flat, exit_overrides)`` where ``exit_overrides`` maps a
        straddle leg instrument to the average price we actually got when
        re-flattening it (used for accurate close P&L). Ports the OKX
        ``_flatten_residual_until_flat`` behaviour.
        """
        instruments = {
            straddle.call_leg.instrument, straddle.put_leg.instrument,
        }
        overrides: dict[str, float] = {}
        deadline = _time.monotonic() + config.CLOSE_FLATTEN_BUDGET_MIN * 60.0
        alerted = False
        taker_alerted = False
        round_i = 0

        while True:
            try:
                positions = await self.exchange.list_open_positions()
            except Exception:
                log.warning("reflatten_fetch_failed", exc_info=True)
                positions = None

            if positions is not None:
                residual = [
                    p for p in positions
                    if abs(float(p.get("amount", 0.0))) > 1e-9
                ]
                if not residual:
                    log.info("reflatten_flat", rounds=round_i)
                    return True, overrides

                round_i += 1
                if not alerted:
                    alerted = True
                    detail = ", ".join(
                        f"{p['instrument_name']} {float(p['amount']):+.4f}"
                        for p in residual
                    )
                    await notifier.send(
                        f"<b>🔁 POST-CLOSE RE-FLATTEN</b>\n"
                        f"Exchange not flat after unwind — re-selling "
                        f"residual (budget {config.CLOSE_FLATTEN_BUDGET_MIN:.0f} "
                        f"min): {detail}"
                    )

                # Taker escalation (OKX parity): after N maker rounds fail
                # to clear the residual, cross the spread with a TAKER order
                # to guarantee the close instead of locking as an orphan.
                use_taker = round_i > config.CLOSE_FLATTEN_TAKER_AFTER_ROUNDS
                if use_taker and not taker_alerted:
                    taker_alerted = True
                    await notifier.send(
                        f"<b>⚠️ RE-FLATTEN → TAKER</b>\n"
                        f"{config.CLOSE_FLATTEN_TAKER_AFTER_ROUNDS} maker "
                        f"round(s) did not clear the residual — crossing the "
                        f"spread (taker) to guarantee the close."
                    )

                for p in residual:
                    inst = p["instrument_name"]
                    amt = float(p.get("amount", 0.0))
                    if use_taker:
                        r = await self.exchange.taker_flatten(inst, amt)
                        if r and inst in instruments:
                            avg = float(r.get("average_price", 0.0))
                            if avg > 0:
                                overrides[inst] = avg
                        continue
                    try:
                        ticker = await self.exchange.get_ticker(inst)
                    except Exception:
                        log.warning("reflatten_ticker_failed", instrument=inst)
                        continue
                    if amt > 0:
                        initial = ticker.bid if ticker.bid > 0 else ticker.ask
                        r = await self.exchange.chase_sell(inst, abs(amt), initial)
                    else:
                        initial = ticker.ask if ticker.ask > 0 else ticker.bid
                        r = await self.exchange.chase_buy(inst, abs(amt), initial)
                    if r and r.get("order_status") != "partial" and inst in instruments:
                        overrides[inst] = float(r.get("average_price", initial))

            if _time.monotonic() >= deadline:
                break
            await asyncio.sleep(config.CLOSE_FLATTEN_ROUND_MIN * 60.0)

        # Final authoritative check.
        try:
            positions = await self.exchange.list_open_positions()
            flat = not any(
                abs(float(p.get("amount", 0.0))) > 1e-9 for p in positions
            )
        except Exception:
            log.warning("reflatten_final_check_failed", exc_info=True)
            flat = False
        return flat, overrides

    # ──────────────────── Close ───────────────────────────────────

    async def _on_close(self) -> None:
        try:
            if not self.portfolio.has_open:
                log.info("close_nothing_open")
                return

            straddle = self.portfolio.open_straddle
            equity_before = self.portfolio.equity

            result = await self.exit_mgr.hard_close(reason="session_close")
            if result is None:
                log.info("close_nothing_open")
                return

            exit_call = result.exit_call_price
            exit_put = result.exit_put_price

            # ── Reconcile-then-lock ──
            # Verify the exchange is actually flat before recording ANY close.
            # If not, persistently re-flatten within budget; if it still is
            # not flat, LOCK entries (orphan) rather than booking a phantom
            # close with fabricated exit prices.
            flat = True
            if not config.DRY_RUN:
                flat, overrides = await self._flatten_residual_until_flat(straddle)
                if straddle.call_leg.instrument in overrides:
                    exit_call = overrides[straddle.call_leg.instrument]
                if straddle.put_leg.instrument in overrides:
                    exit_put = overrides[straddle.put_leg.instrument]

            if not flat:
                self._entry_locked = True
                self._lock_reason = (
                    "Post-close exchange NOT flat after re-flatten budget — "
                    "orphan; straddle kept OPEN locally, entries locked"
                )
                log.error("post_close_not_flat_lock", id=straddle.id)
                await notifier.send(
                    f"<b>⚠️ POST-CLOSE ORPHAN — ENTRIES LOCKED</b>\n"
                    f"Straddle <code>{straddle.id}</code> could not be fully "
                    f"flattened within the re-flatten budget.\n\n"
                    f"Local state kept OPEN (no phantom close). "
                    f"<b>ACTION</b>: flatten manually "
                    f"(tools/force_liquidate.py) and restart."
                )
                return

            # Confirmed flat → finalise the close (records P&L + trade log).
            if not result.both_sold:
                log.info("close_finalized_via_reflatten", id=straddle.id)
            pnl = self.portfolio.close_straddle(
                exit_call, exit_put, "session_close",
            )
            await notifier.notify_close(pnl, "session_close")

            if not config.DRY_RUN:
                live_equity = await self.exchange.get_subaccount_collateral()
                if live_equity is None:
                    log.warning("close_equity_read_failed_keeping_persisted",
                                persisted=self.portfolio.equity)
                else:
                    self.portfolio.sync_equity(live_equity)

            actual_pnl = self.portfolio.equity - equity_before
            if actual_pnl != 0.0:
                cum_return = (
                    self.portfolio.equity - config.INITIAL_CAPITAL_USD
                ) / config.INITIAL_CAPITAL_USD
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

    # ──────────────────── Daily Report (15:00 UTC) ────────────────

    async def _on_report(self) -> None:
        try:
            await notifier.send_daily_report(self.portfolio.equity)
        except Exception:
            log.error("report_error", exc_info=True)
            await notifier.notify_error("Report", "Daily report failed — check logs")

    # ──────────────────── Weekly Report (Fri 16:00 UTC) ──────────

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
            straddle = self.portfolio.open_straddle
            result = await unwind_straddle(
                self.exchange, self.market, self.portfolio, reason="shutdown",
            )
            # Best-effort finalise on shutdown. We do not run the full
            # re-flatten budget here (we're exiting); the next boot's startup
            # reconcile is the backstop if a leg failed to sell.
            if result is not None:
                self.portfolio.close_straddle(
                    result.exit_call_price, result.exit_put_price, "shutdown",
                )
                if not result.both_sold:
                    log.warning("shutdown_unwind_incomplete", id=straddle.id)

        _release_singleton_lock()
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
