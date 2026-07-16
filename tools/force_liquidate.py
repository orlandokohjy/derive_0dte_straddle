"""
Force-flatten Derive option positions and cancel pending orders.

OPERATOR EMERGENCY PANIC-BUTTON. For when the algo's auto-recovery is
stuck (e.g. a maker sell that won't fill, or a post-close orphan lock) and
you need to MANUALLY restore a flat subaccount before letting the algo run
again.

CRITICAL: run with the live algo STOPPED:

    docker-compose stop algo   (or Ctrl-C the process)

Running while the algo is up races its chase loops. The script refuses if
it detects the algo's singleton lock (state/algo.pid) is held by a live
process — pass --force to override (NOT recommended).

USAGE
-----
    # Flatten ALL option positions + cancel ALL orders (default)
    python tools/force_liquidate.py

    # Single instrument only
    python tools/force_liquidate.py BTC-20260709-62000-C

    # Preview without placing orders
    python tools/force_liquidate.py --dry-run

    # Override the lock-file safety check (only if the algo is really stopped)
    python tools/force_liquidate.py --force

BEHAVIOUR (per call)
--------------------
    A. Refuse if state/algo.pid is held by a live process.
    B. Discover all live Derive option positions + open orders.
    C. Print a plan summary.
    D. Cancel every open order.
    E. For each non-zero position:
         * Long  -> TAKER sell at the live BID (crosses the spread)
         * Short -> TAKER buy  at the live ASK
       Crossing the spread guarantees a near-instant fill. On Derive the
       position `amount` is already in the underlying unit (BTC), so the
       order size equals abs(amount) directly — no contract conversion.
    F. Poll up to 60 s per instrument for confirmation of zero position.

EXIT CODES
----------
    0  flat across all targeted instruments
    2  no Derive credentials in env
    3  singleton lock held by a live PID (pass --force to override)
    4  position fetch failed
    5  order placement failed for at least one instrument
    6  timeout — at least one instrument still open after 60 s
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from core.exchange import DeriveExchange

_LOCK_PATH = f"{config.STATE_DIR}/algo.pid"


def _check_lock(force: bool) -> int:
    """Refuse to run if state/algo.pid is held by a live process.

    Handles the Docker PID-1 collision: an ephemeral one-off container after
    `docker-compose stop` is also PID 1 and shares the state/ volume, so a
    lock file reading "1" that matches our own PID is treated as stale.
    """
    if not os.path.exists(_LOCK_PATH):
        return 0
    try:
        with open(_LOCK_PATH) as f:
            pid = int((f.read() or "0").strip())
    except Exception:
        pid = 0
    if pid <= 0:
        if force:
            return 0
        print(f"[force_liquidate] {_LOCK_PATH} unreadable/empty. "
              "Pass --force to override.")
        return 3
    if pid == os.getpid():
        print(f"[force_liquidate] note: lock pid={pid} == our PID "
              "(Docker PID-1 collision). Treating as stale — proceeding.")
        return 0
    try:
        os.kill(pid, 0)
        alive = True
    except (ProcessLookupError, PermissionError):
        alive = pid != 0 and _pid_ambiguous(pid)
    except Exception:
        alive = False
    if alive and not force:
        print(f"[force_liquidate] LOCK HELD: {_LOCK_PATH} -> pid={pid} alive.\n"
              "   ACTION: stop the algo, THEN re-run.\n"
              "   (or pass --force if you are CERTAIN it is stopped)")
        return 3
    if alive and force:
        print(f"[force_liquidate] WARN: lock held by pid={pid} but --force "
              "given. Proceeding (race risk).")
        return 0
    print(f"[force_liquidate] note: lock pid={pid} not visible on this host "
          "(likely container-internal / stale). Proceeding.")
    return 0


def _pid_ambiguous(pid: int) -> bool:
    # PermissionError => process exists but owned by another user => alive.
    return True


async def _flatten_one(
    exchange: DeriveExchange, symbol: str, *, dry_run: bool,
) -> tuple[bool, str]:
    """Flatten a single instrument via a taker cross. Returns (ok, status)."""
    # A) cancel any open orders on this symbol
    try:
        orders = await exchange.list_open_orders()
        for o in orders:
            if o.get("instrument_name") == symbol and o.get("order_id"):
                await exchange.cancel_order(o["order_id"], symbol)
    except Exception as exc:
        return False, f"{symbol}: cancel-orders failed: {exc}"

    # B) read the actual position
    try:
        positions = await exchange.list_open_positions()
    except Exception as exc:
        return False, f"{symbol}: position fetch failed: {exc}"
    target = next(
        (p for p in positions if p["instrument_name"] == symbol), None,
    )
    if target is None or abs(float(target.get("amount", 0.0))) < 1e-9:
        return True, f"{symbol}: already flat"

    amt = float(target["amount"])  # signed; +long / -short, in BTC
    print(f"  [{symbol}] live position: {amt:+.4f} "
          f"avg=${target.get('average_price', 0):,.2f} "
          f"mark=${target.get('mark_price', 0):,.2f} "
          f"uPnL=${target.get('unrealized_pnl', 0):+,.2f}")

    # C) book -> side & crossing price
    try:
        ticker = await exchange.get_ticker(symbol)
    except Exception as exc:
        return False, f"{symbol}: ticker fetch failed: {exc}"
    bid, ask = float(ticker.bid), float(ticker.ask)
    if bid <= 0 or ask <= 0:
        return False, f"{symbol}: invalid book bid={bid} ask={ask}"

    if amt > 0:
        direction, price = "sell", bid   # cross down to guarantee fill
    else:
        direction, price = "buy", ask     # cross up
    qty = abs(amt)

    print(f"  [{symbol}] flatten plan: {direction} {qty:.4f} @ ${price:,.2f} "
          f"(book {bid}/{ask}) TAKER")
    if dry_run:
        return True, f"{symbol}: dry-run plan ok"

    # D) taker limit crossing the spread (post_only=False)
    order = await exchange._place_limit_order(
        symbol, direction, qty, price, post_only=False,
    )
    if order.get("rejected_insufficient_funds"):
        return False, f"{symbol}: REJECTED insufficient funds"
    if not order.get("order_id"):
        return False, f"{symbol}: order not accepted: {order}"
    print(f"  [{symbol}] order placed: id={order.get('order_id')} "
          f"status={order.get('order_status')}")

    # E) poll for flat (up to 60 s)
    for attempt in range(12):
        await asyncio.sleep(5)
        try:
            pos_now = await exchange.list_open_positions()
        except Exception:
            continue
        live = next(
            (p for p in pos_now if p["instrument_name"] == symbol), None,
        )
        if live is None or abs(float(live.get("amount", 0.0))) < 1e-9:
            return True, f"{symbol}: FLAT after {(attempt + 1) * 5}s"
        print(f"  [{symbol}] still {live.get('amount'):+.4f}, waiting...")
    return False, f"{symbol}: TIMEOUT — still open after 60 s"


async def _liquidate(symbol: str | None, *, dry_run: bool) -> int:
    if not config.DERIVE_WALLET or not config.DERIVE_SESSION_KEY:
        print("[force_liquidate] No Derive credentials configured. Aborting.")
        return 2

    exchange = DeriveExchange()
    exchange.connect()

    try:
        positions = await exchange.list_open_positions()
        orders = await exchange.list_open_orders()
    except Exception as exc:
        print(f"[force_liquidate] discovery failed: {exc}")
        return 4

    if symbol is not None:
        positions = [p for p in positions if p["instrument_name"] == symbol]
        orders = [o for o in orders if o.get("instrument_name") == symbol]

    pos_symbols = {p["instrument_name"] for p in positions}
    ord_symbols = {o.get("instrument_name", "") for o in orders
                   if o.get("instrument_name")}
    targets = sorted(pos_symbols | ord_symbols)

    print("=" * 72)
    print(f"[force_liquidate] mode={'SINGLE' if symbol else 'ALL'} "
          f"dry_run={dry_run} env={config.DERIVE_ENV}")
    print(f"[force_liquidate] live positions: {len(positions)}")
    for p in positions:
        print(f"  • {p['instrument_name']:34s} {float(p['amount']):+.4f} "
              f"mark=${float(p.get('mark_price', 0)):,.2f} "
              f"uPnL=${float(p.get('unrealized_pnl', 0)):+,.2f}")
    print(f"[force_liquidate] open orders: {len(orders)}")
    for o in orders:
        print(f"  • {o.get('instrument_name', '?'):34s} "
              f"{o.get('direction', '?')} {o.get('amount', '?')}"
              f"@{o.get('limit_price', '?')}")
    print(f"[force_liquidate] symbols to flatten: {len(targets)}")
    print("=" * 72)

    if not targets:
        print("[force_liquidate] nothing to do — flat, no open orders.")
        return 0

    results = []
    for sym in targets:
        ok, line = await _flatten_one(exchange, sym, dry_run=dry_run)
        results.append((ok, line))
        print(f"[force_liquidate] {'OK ' if ok else 'ERR'} {line}")

    print("=" * 72)
    fails = [line for ok, line in results if not ok]
    if fails:
        print(f"[force_liquidate] {len(fails)} failure(s):")
        for line in fails:
            print(f"  ✗ {line}")
        return 5 if any("REJECTED" in f or "failed" in f for f in fails) else 6

    if dry_run:
        print("[force_liquidate] DRY-RUN OK. No orders placed.")
        return 0
    print(f"[force_liquidate] ALL CLEAR — {len(results)} symbol(s) flat. "
          "Safe to start the algo.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("symbol", nargs="?", default=None,
                        help="Optional Derive option instrument. If omitted, "
                             "ALL option positions/orders are flattened.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan but place no orders.")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the singleton lock check (only if the "
                             "algo is CERTAINLY stopped).")
    args = parser.parse_args(argv)

    rc = _check_lock(args.force)
    if rc != 0:
        return rc
    return asyncio.run(_liquidate(args.symbol, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
