"""APScheduler wrapper — single session: entry at 14:00, close at 18:00 UTC."""
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
        weekdays = ",".join(
            ["mon", "tue", "wed", "thu", "fri"][d] for d in sorted(config.ALLOWED_WEEKDAYS)
        )

        entry_t = config.SESSION_ENTRY_UTC
        self._scheduler.add_job(
            on_entry,
            CronTrigger(
                hour=entry_t.hour, minute=entry_t.minute,
                day_of_week=weekdays, timezone=UTC,
            ),
            id="session_entry",
            name=f"Session Entry ({entry_t.hour:02d}:{entry_t.minute:02d} UTC)",
            replace_existing=True,
        )

        close_t = config.SESSION_CLOSE_UTC
        self._scheduler.add_job(
            on_close,
            CronTrigger(
                hour=close_t.hour, minute=close_t.minute,
                day_of_week=weekdays, timezone=UTC,
            ),
            id="session_close",
            name=f"Session Close ({close_t.hour:02d}:{close_t.minute:02d} UTC)",
            replace_existing=True,
        )

        report_t = config.REPORT_UTC
        self._scheduler.add_job(
            on_report,
            CronTrigger(
                hour=report_t.hour, minute=report_t.minute,
                day_of_week=weekdays, timezone=UTC,
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
            "session_scheduled",
            entry=f"{entry_t.hour:02d}:{entry_t.minute:02d} UTC",
            close=f"{close_t.hour:02d}:{close_t.minute:02d} UTC",
            report=f"{report_t.hour:02d}:{report_t.minute:02d} UTC",
            weekly_report=f"Fri {config.WEEKLY_REPORT_UTC.hour:02d}:{config.WEEKLY_REPORT_UTC.minute:02d} UTC",
            days=weekdays,
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
