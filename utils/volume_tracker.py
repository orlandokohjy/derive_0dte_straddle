"""Monthly volume tracking for option contracts and BTC notional."""
from __future__ import annotations

import csv
import os
from datetime import datetime

import structlog

import config

log = structlog.get_logger(__name__)


def _current_month_key() -> str:
    return datetime.utcnow().strftime("%Y-%m")


def record_trade(num_straddles: int) -> None:
    """
    Append volume for one session's trades.

    Per straddle (pure: 1 call + 1 put):
      option_contracts = 4  (buy call + buy put + sell call + sell put)
      option_btc       = 2 × QTY_PER_LEG × 2 (buy+sell for each leg)
    """
    contracts_per = 4  # buy call, buy put, sell call, sell put
    option_btc_per = 2 * config.QTY_PER_LEG * 2

    row = {
        "month": _current_month_key(),
        "num_straddles": num_straddles,
        "option_contracts": contracts_per * num_straddles,
        "option_btc_notional": option_btc_per * num_straddles,
    }

    os.makedirs(os.path.dirname(config.VOLUME_FILE), exist_ok=True)
    file_exists = os.path.exists(config.VOLUME_FILE)

    with open(config.VOLUME_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    log.info("volume_recorded", **row)
