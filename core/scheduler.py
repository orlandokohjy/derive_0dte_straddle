"""APScheduler wrapper — single session: entry at 12:00, close at 14:00 UTC."""
from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import structlog

import config
from utils.time_utils import UTC

log = structlog.get_logger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    def register_session(
        self, on_entry: callable, on_close: callable, on_report: callable,
        on_weekly_report: callable | None = None,
    ) -> None:
        # Register an entry + close cron per session in the (multi-session)
        # SESSION_SCHEDULE. Handlers are session-agnostic: entry opens a
        # straddle if none is open, close unwinds whatever is open. Because
        # the schedule is contiguous (close = next_entry − buffer) windows
        # never overlap, so the single-straddle model still holds.
        for spec in config.SESSION_SCHEDULE:
            self._scheduler.add_job(
                on_entry,
                CronTrigger(
                    hour=spec.entry_utc.hour, minute=spec.entry_utc.minute,
                    day_of_week=",".join(spec.entry_days), timezone=UTC,
                ),
                id=f"session_entry_{spec.name}",
                name=f"Session Entry [{spec.name}] ({spec.label})",
                replace_existing=True,
            )
            self._scheduler.add_job(
                on_close,
                CronTrigger(
                    hour=spec.close_utc.hour, minute=spec.close_utc.minute,
                    day_of_week=",".join(spec.close_days), timezone=UTC,
                ),
                id=f"session_close_{spec.name}",
                name=f"Session Close [{spec.name}] ({spec.label})",
                replace_existing=True,
            )

        report_t = config.REPORT_UTC
        # Daily report fires every day — the schedule spans weekday AND weekend
        # sessions, so weekend trades must be reported too.
        report_days = ",".join(config._WEEKDAY_DOW + config._WEEKEND_DOW)
        self._scheduler.add_job(
            on_report,
            CronTrigger(
                hour=report_t.hour, minute=report_t.minute,
                day_of_week=report_days, timezone=UTC,
            ),
            id="daily_report",
            name=f"Daily Report ({report_t.hour:02d}:{report_t.minute:02d} UTC)",
            replace_existing=True,
        )

        if on_weekly_report is not None:
            weekly_t = config.WEEKLY_REPORT_UTC
            self._scheduler.add_job(
                on_weekly_report,
                CronTrigger(
                    hour=weekly_t.hour, minute=weekly_t.minute,
                    day_of_week="fri", timezone=UTC,
                ),
                id="weekly_report",
                name=f"Weekly Report (Fri {weekly_t.hour:02d}:{weekly_t.minute:02d} UTC)",
                replace_existing=True,
            )

        log.info(
            "sessions_scheduled",
            sessions=[
                f"{s.name} {s.label} [{','.join(s.entry_days)}]"
                for s in config.SESSION_SCHEDULE
            ],
            count=len(config.SESSION_SCHEDULE),
            report=f"{report_t.hour:02d}:{report_t.minute:02d} UTC",
            weekly_report=(
                f"Fri {config.WEEKLY_REPORT_UTC.hour:02d}:"
                f"{config.WEEKLY_REPORT_UTC.minute:02d} UTC"
            ),
        )

    def start(self) -> None:
        self._scheduler.start()
        log.info("scheduler_started", jobs=len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")

    def get_next_fire_times(self) -> dict[str, datetime | None]:
        return {
            job.id: job.next_run_time
            for job in self._scheduler.get_jobs()
        }
