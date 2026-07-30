"""
Derive 0DTE Pure Straddle — Configuration.

All tunables in one place. Env-var overrides for deployment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time

# ──────────────────── Derive Credentials ──────────────────────────
DERIVE_ENV: str = os.getenv("DERIVE_ENV", "PROD")           # PROD or TEST
DERIVE_WALLET: str = os.getenv("DERIVE_WALLET", "")         # MetaMask wallet address
DERIVE_SESSION_KEY: str = os.getenv("DERIVE_SESSION_KEY", "")  # Session key private key
DERIVE_SUBACCOUNT_ID: int = int(os.getenv("DERIVE_SUBACCOUNT_ID", "0"))

DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() == "true"

# ──────────────────── WebSocket / REST endpoints ──────────────────
WS_MAINNET: str = "wss://api.lyra.finance/ws"
WS_TESTNET: str = "wss://api-demo.lyra.finance/ws"
REST_MAINNET: str = "https://api.lyra.finance"
REST_TESTNET: str = "https://api-demo.lyra.finance"

def ws_url() -> str:
    return WS_TESTNET if DERIVE_ENV == "TEST" else WS_MAINNET

def rest_url() -> str:
    return REST_TESTNET if DERIVE_ENV == "TEST" else REST_MAINNET

# ──────────────────── Strategy Constants ──────────────────────────
BASE_COIN: str = "BTC"
QTY_PER_LEG: float = float(os.getenv("QTY_PER_LEG", "1.0"))

INITIAL_CAPITAL_USD: float = float(os.getenv("INITIAL_CAPITAL_USD", "8000.0"))
ALLOC_PCT: float = 0.80
NUM_STRADDLES_OVERRIDE: int = int(os.getenv("NUM_STRADDLES_OVERRIDE", "1"))  # >0 forces exact count; default 1 to prevent auto-sizing multiple straddles

# ──────────────────── Session Schedule (UTC) ──────────────────────
# MULTI-SESSION schedule mirroring the OKX ATM-wings stack EXACTLY (minus
# wings — this stack never sells wings). Each window holds 30 min, except
# where the next entry is 30 min away, in which case the close rolls to
# ``next_entry − _SESSION_ROLL_BUFFER_MIN`` (chained roll, so consecutive
# windows never overlap → the single-straddle model still holds). All
# entries are ≥ 09:00 UTC (after the 08:00 expiry cutoff) so every session
# trades the next 08:00-UTC expiry, same as the current 12:00 session.
_SESSION_ROLL_BUFFER_MIN: int = 5

_WEEKDAY_ENTRIES: list[tuple[str, int, int]] = [
    ("wd_0900", 9, 0),
    ("wd_1100", 11, 0),
    ("wd_1130", 11, 30),
    ("wd_1200", 12, 0),
    ("wd_1230", 12, 30),
    ("wd_1300", 13, 0),
    ("wd_1330", 13, 30),
    ("wd_1400", 14, 0),
    ("wd_1430", 14, 30),
    ("wd_1500", 15, 0),
    ("wd_1530", 15, 30),
    ("wd_2330", 23, 30),
]
_WEEKDAY_DOW: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")

_WEEKEND_ENTRIES: list[tuple[str, int, int]] = [
    ("we_1100", 11, 0),
    ("we_1200", 12, 0),
    ("we_1230", 12, 30),
    ("we_1330", 13, 30),
    ("we_1430", 14, 30),
    ("we_1500", 15, 0),
    ("we_1700", 17, 0),
    ("we_1900", 19, 0),
    ("we_2200", 22, 0),
]
_WEEKEND_DOW: tuple[str, ...] = ("sat", "sun")

_DOW_ORDER: tuple[str, ...] = (
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
)


def _derive_close(entry_h: int, entry_m: int, next_entry_min: int | None) -> time:
    """Close = entry+30 unless the next entry is exactly 30 min away (a
    chained roll), in which case close = next_entry − buffer. Wraps past
    midnight (e.g. 23:30 + 30 = 00:00)."""
    entry_min = entry_h * 60 + entry_m
    full_close_min = entry_min + 30
    if next_entry_min is not None and next_entry_min == full_close_min:
        close_min = next_entry_min - _SESSION_ROLL_BUFFER_MIN
    else:
        close_min = full_close_min
    close_min %= (24 * 60)
    return time(close_min // 60, close_min % 60)


def _shift_days(days: tuple[str, ...]) -> tuple[str, ...]:
    """Shift each weekday token forward one day (for a cross-midnight close
    whose cron must fire on the following calendar day)."""
    return tuple(
        _DOW_ORDER[(_DOW_ORDER.index(d) + 1) % 7] for d in days
    )


@dataclass(frozen=True)
class SessionSpec:
    name: str
    entry_utc: time
    close_utc: time
    entry_days: tuple[str, ...]   # cron day-of-week tokens for the ENTRY cron
    close_days: tuple[str, ...]   # cron day-of-week tokens for the CLOSE cron

    @property
    def window_min(self) -> int:
        e = self.entry_utc.hour * 60 + self.entry_utc.minute
        c = self.close_utc.hour * 60 + self.close_utc.minute
        if c <= e:  # crosses midnight
            c += 24 * 60
        return c - e

    @property
    def label(self) -> str:
        return (
            f"{self.entry_utc.strftime('%H:%M')}-"
            f"{self.close_utc.strftime('%H:%M')} UTC"
        )


def _build_specs(
    entries: list[tuple[str, int, int]], dow: tuple[str, ...],
) -> list[SessionSpec]:
    entry_mins = {h * 60 + m for (_, h, m) in entries}
    specs: list[SessionSpec] = []
    for (name, h, m) in entries:
        nxt = h * 60 + m + 30
        next_entry_min = nxt if nxt in entry_mins else None
        close = _derive_close(h, m, next_entry_min)
        crosses = (close.hour * 60 + close.minute) <= (h * 60 + m)
        close_days = _shift_days(dow) if crosses else dow
        specs.append(SessionSpec(name, time(h, m), close, dow, close_days))
    return specs


SESSION_SCHEDULE: list[SessionSpec] = (
    _build_specs(_WEEKDAY_ENTRIES, _WEEKDAY_DOW)
    + _build_specs(_WEEKEND_ENTRIES, _WEEKEND_DOW)
)

# Legacy single-session anchors — kept for any introspection / backward
# reference. Scheduling now runs off SESSION_SCHEDULE above.
SESSION_ENTRY_UTC: time = SESSION_SCHEDULE[0].entry_utc
SESSION_CLOSE_UTC: time = SESSION_SCHEDULE[0].close_utc
REPORT_UTC: time = time(15, 0)
WEEKLY_REPORT_UTC: time = time(16, 0)
ALLOWED_WEEKDAYS: set[int] = {0, 1, 2, 3, 4}  # Mon–Fri (legacy)

# ──────────────────── Execution Settings ──────────────────────────
OPTION_CHASE_INTERVAL_SEC: float = 5.0
OPTION_CHASE_MAX_ATTEMPTS: int = 60
# Price grid the chase snaps to. $5 is coarse for a 0DTE BTC option quoted
# near $200 (a 2.5 % step), which blunts the gap-narrowing chase — override
# it once Derive's per-instrument `tick_size` is confirmed.
OPTION_TICK_SIZE: float = float(os.getenv("OPTION_TICK_SIZE", "5.0"))

# Chase logic: on each retry narrow the gap to the ask/bid by this pct
OPTION_CHASE_GAP_NARROW_PCT: float = float(os.getenv("OPTION_CHASE_GAP_NARROW_PCT", "0.5"))
# Hard cap on our buy price: mark * MAX_SLIPPAGE_FACTOR. Also floor on sell.
OPTION_CHASE_MAX_SLIPPAGE_FACTOR: float = float(os.getenv("OPTION_CHASE_MAX_SLIPPAGE_FACTOR", "1.15"))
# Abort chase after this many minutes of no full fill
OPTION_CHASE_DEADLINE_MIN: float = float(os.getenv("OPTION_CHASE_DEADLINE_MIN", "10.0"))

# Pre-entry spread sanity gate — skip session if either leg's
# (ask − bid) / mid > this. 0.30 = skip if spread is wider than 30 % of mid.
OPTION_MAX_ENTRY_SPREAD_PCT: float = float(
    os.getenv("OPTION_MAX_ENTRY_SPREAD_PCT", "0.30")
)

# ──────────────────── Risk Management ─────────────────────────────
MAX_DAILY_LOSS_PCT: float | None = None
CIRCUIT_BREAKER_API_ERRORS: int = 5
CIRCUIT_BREAKER_COOLDOWN_SEC: float = 300.0

# Pre-entry collateral safety buffer — entry is skipped unless available
# subaccount collateral ≥ expected_premium × this factor.
COLLATERAL_BUFFER_FACTOR: float = float(
    os.getenv("COLLATERAL_BUFFER_FACTOR", "1.2")
)

# Lock the algo after this many consecutive session failures.
# Manual unlock (restart) required.
CONSECUTIVE_FAILURE_LIMIT: int = int(
    os.getenv("CONSECUTIVE_FAILURE_LIMIT", "3")
)

# ──────────────────── OKX-parity reliability hardening ────────────
# Persistent post-close re-flatten: after the unwind, if the exchange is
# NOT flat we loop selling the residual long legs until either the account
# is flat or this wall-clock budget (minutes) is exhausted. If still not
# flat at the end, the algo LOCKS entries (orphan) rather than recording a
# phantom close. CLOSE_FLATTEN_ROUND_MIN is the pause between rounds.
CLOSE_FLATTEN_BUDGET_MIN: float = float(
    os.getenv("CLOSE_FLATTEN_BUDGET_MIN", "10.0")
)
CLOSE_FLATTEN_ROUND_MIN: float = float(
    os.getenv("CLOSE_FLATTEN_ROUND_MIN", "1.0")
)
# Taker escalation (OKX parity): after this many maker-only re-flatten
# rounds fail to clear a residual leg, the post-close reconcile crosses the
# spread with a TAKER order to guarantee the close, rather than locking as
# an orphan. Set very high to keep the reconcile maker-only.
CLOSE_FLATTEN_TAKER_AFTER_ROUNDS: int = int(
    os.getenv("CLOSE_FLATTEN_TAKER_AFTER_ROUNDS", "2")
)

# Singleton lock: refuse to boot a second algo instance sharing the same
# session key / subaccount (prevents two processes racing on the same
# orders). Path is under STATE_DIR. Disable only for tooling.
SINGLETON_LOCK_ENABLED: bool = os.getenv(
    "SINGLETON_LOCK_ENABLED", "true",
).lower() in ("true", "1", "yes", "on")

# Startup chase-pricing self-test: simulate one maker-chase iteration on a
# live option and abort (entry-lock) if the capped price violates sanity
# bounds — guards against a unit/tick regression sending a wild price.
CHASE_SELFTEST_ENABLED: bool = os.getenv(
    "CHASE_SELFTEST_ENABLED", "true",
).lower() in ("true", "1", "yes", "on")
# Capped chase price must not exceed mark × (1 + this).
CHASE_SELFTEST_MAX_OVER_MARK: float = float(
    os.getenv("CHASE_SELFTEST_MAX_OVER_MARK", "0.20")
)
# Absolute ceiling (USD) on any single-leg option price we would ever pay.
# A BTC 0DTE option premium should never approach this; a larger value
# signals a unit bug (e.g. price expressed in the wrong scale).
CHASE_SELFTEST_MAX_ABSOLUTE_USD: float = float(
    os.getenv("CHASE_SELFTEST_MAX_ABSOLUTE_USD", "20000.0")
)

# Optionally wipe local state (positions.json / equity.json) on boot. Off
# by default; use only for a deliberate clean restart.
RESET_STATE_ON_BOOT: bool = os.getenv(
    "RESET_STATE_ON_BOOT", "false",
).lower() in ("true", "1", "yes", "on")

# ──────────────────── Telegram ────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_REPORT_BOT_TOKEN: str = os.getenv("TELEGRAM_REPORT_BOT_TOKEN", "")
TELEGRAM_REPORT_CHAT_ID: str = os.getenv("TELEGRAM_REPORT_CHAT_ID", "")
TELEGRAM_ENABLED: bool = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ──────────────────── Logging & Persistence ───────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_JSON: bool = True
LOG_FILE: str = "logs/algo.log"
STATE_DIR: str = "state"
EQUITY_FILE: str = f"{STATE_DIR}/equity.json"
POSITIONS_FILE: str = f"{STATE_DIR}/positions.json"
TRADE_LOG_FILE: str = f"{STATE_DIR}/trade_log.csv"
VOLUME_FILE: str = f"{STATE_DIR}/volume.csv"
