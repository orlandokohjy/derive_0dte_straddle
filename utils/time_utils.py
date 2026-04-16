"""UTC time helpers and 0DTE expiry-date logic for Derive."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def _expiry_date() -> datetime:
    """
    Derive options settle at 08:00 UTC. Before 08:00 the 0DTE expiry is
    today; after 08:00 the 0DTE is tomorrow.
    """
    now = now_utc()
    if now.hour < 8:
        return now
    return now + timedelta(days=1)


def today_expiry_date_str() -> str:
    """Instrument-name format: DDMMYYYY (e.g. 16042026)."""
    return _expiry_date().strftime("%d%m%Y")


def today_expiry_api_str() -> str:
    """API parameter format: YYYYMMDD (e.g. 20260416)."""
    return _expiry_date().strftime("%Y%m%d")


def format_utc_sgt(dt: datetime) -> str:
    sgt = dt.astimezone(timezone(timedelta(hours=8)))
    return sgt.strftime("%Y-%m-%d %H:%M SGT")


def is_weekday() -> bool:
    return now_utc().weekday() < 5
