"""
Derive 0DTE Pure Straddle — Configuration.

All tunables in one place. Env-var overrides for deployment.
"""
from __future__ import annotations

import os
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
SESSION_ENTRY_UTC: time = time(12, 0)
SESSION_CLOSE_UTC: time = time(14, 0)
REPORT_UTC: time = time(15, 0)
WEEKLY_REPORT_UTC: time = time(16, 0)
ALLOWED_WEEKDAYS: set[int] = {0, 1, 2, 3, 4}  # Mon–Fri

# ──────────────────── Execution Settings ──────────────────────────
OPTION_CHASE_INTERVAL_SEC: float = 5.0
OPTION_CHASE_MAX_ATTEMPTS: int = 60
OPTION_TICK_SIZE: float = 5.0

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
