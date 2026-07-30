"""Print the RAW exception Derive raises when we place a post-only order.

Why this exists: on 2026-07-30 three sessions logged `order_failed` and the
chase abandoned itself after one attempt. `_place_limit_order` swallows the
exception into `log.warning("order_failed", error=...)` and returns `{}`, and
because structlog was never actually writing to `logs/algo.log`, a
`docker-compose up --force-recreate` destroyed the error strings. This probe
reproduces the exact call the algo makes and prints the unswallowed exception,
so we don't have to wait for a live session to see it.

SAFETY: bids at a deliberately deep discount to mark (default 50 %) with
post_only, so it cannot cross the spread. Any order that does rest is
cancelled immediately. Use --dry to resolve + quote only, placing nothing.

    python tools/probe_order.py                 # auto-pick nearest expiry ATM call
    python tools/probe_order.py --instrument BTC-20260731-64500-C
    python tools/probe_order.py --dry
"""
from __future__ import annotations

import argparse
import asyncio
import traceback

import config
from core.exchange import DeriveExchange, _round_price


async def _resolve_instrument(ex: DeriveExchange, explicit: str | None) -> str:
    if explicit:
        return explicit
    spot = await ex.get_spot_price()
    instruments = await ex.get_instruments("BTC", "option")
    calls = []
    for i in instruments:
        name = getattr(i, "instrument_name", None) or i.get("instrument_name")
        if name and name.endswith("-C"):
            calls.append(name)
    if not calls:
        raise SystemExit("no BTC calls returned by get_instruments")
    # nearest expiry, then strike closest to spot
    def _key(n: str):
        parts = n.split("-")
        return (parts[1], abs(float(parts[2]) - spot))
    calls.sort(key=_key)
    print(f"spot=${spot:,.0f}  picked nearest-expiry ATM call")
    return calls[0]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default=None)
    ap.add_argument("--qty", type=float, default=config.QTY_PER_LEG)
    ap.add_argument("--discount", type=float, default=0.50,
                    help="bid this fraction of mark (0.50 = half mark)")
    ap.add_argument("--dry", action="store_true",
                    help="quote only; place nothing")
    args = ap.parse_args()

    ex = DeriveExchange()
    ex.connect()
    print(f"connected  subaccount={config.DERIVE_SUBACCOUNT_ID}")

    instrument = await _resolve_instrument(ex, args.instrument)
    ticker = await ex.get_ticker(instrument)
    print(f"instrument = {instrument}")
    print(f"  bid={ticker.bid}  ask={ticker.ask}  mark={ticker.mark}")
    print(f"  tick(config)={config.OPTION_TICK_SIZE}  qty={args.qty}")

    collateral = await ex.get_subaccount_collateral()
    print(f"  collateral={collateral}")

    if ticker.mark <= 0:
        raise SystemExit("mark is 0 — cannot size a safe probe price")

    price = _round_price(ticker.mark * args.discount, "down")
    if price <= 0:
        price = config.OPTION_TICK_SIZE
    print(f"\nprobe: post-only BUY {args.qty} @ ${price} "
          f"({args.discount:.0%} of mark — will NOT cross)")

    if args.dry:
        print("--dry set; placing nothing.")
        return

    # Call the client directly so the exception is NOT swallowed the way
    # _place_limit_order swallows it into log.warning("order_failed").
    from derive_client.data_types import D, Direction, OrderType, TimeInForce

    kwargs = dict(
        instrument_name=instrument,
        amount=D(str(args.qty)),
        limit_price=D(str(price)),
        direction=Direction.buy,
        order_type=OrderType.limit,
        time_in_force=getattr(TimeInForce, "post_only", "post_only"),
    )
    print(f"orders.create kwargs = {kwargs}\n")

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: ex._client.orders.create(**kwargs)
        )
    except Exception as exc:
        print("=" * 70)
        print("REJECTED — this is the error the algo was hiding:")
        print("=" * 70)
        print(f"type: {type(exc).__name__}")
        print(f"str : {exc}")
        for attr in ("response", "body", "args", "message", "detail"):
            if hasattr(exc, attr):
                print(f"{attr}: {getattr(exc, attr)}")
        print("-" * 70)
        traceback.print_exc()
        return

    order_id = getattr(result, "order_id", "")
    print("ACCEPTED — so order placement itself is fine.")
    print(f"  order_id={order_id}  status={getattr(result, 'order_status', '')}")
    if order_id:
        await ex.cancel_order(str(order_id), instrument)
        print("  cancelled.")


if __name__ == "__main__":
    asyncio.run(main())
