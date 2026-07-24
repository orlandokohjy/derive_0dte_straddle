"""
Derive exchange wrapper.

Uses derive-client (HTTPClient) for authentication, order signing (EIP-712),
and REST communication. Provides maker-only order placement with escalating
chase logic.

derive-client handles:
  - Session key auth and EIP-712 order signing
  - Instrument spec quantization
  - Order lifecycle management
"""
from __future__ import annotations

import asyncio
import math
import time as _time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import structlog

import config

log = structlog.get_logger(__name__)

INSUFFICIENT_FUNDS_TOKENS = (
    "INSUFFICIENT FUNDS",
    "INSUFFICIENT_FUNDS",
    "INSUFFICIENT_MARGIN",
    "INSUFFICIENT MARGIN",
    "INSUFFICIENT_BALANCE",
    "INSUFFICIENT BALANCE",
    "11000",
)


def _is_insufficient_funds(err_str: str) -> bool:
    upper = err_str.upper()
    return any(token in upper for token in INSUFFICIENT_FUNDS_TOKENS)


def _round_price(price: float, direction: str = "down") -> float:
    """Round option price to tick size."""
    tick = config.OPTION_TICK_SIZE
    if direction == "down":
        return round(math.floor(price / tick) * tick, 2)
    return round(math.ceil(price / tick) * tick, 2)


@dataclass
class TickerSnapshot:
    bid: float = 0.0
    ask: float = 0.0
    mark: float = 0.0
    index: float = 0.0


class DeriveExchange:
    """Wraps derive-client HTTPClient for the 0DTE straddle algo."""

    def __init__(self) -> None:
        self._client = None
        self.error_count: int = 0
        self._last_error_ts: float = 0.0

    def _note_error(self) -> None:
        """Record an API error: bump the counter and stamp the time so the
        circuit breaker can auto-recover after CIRCUIT_BREAKER_COOLDOWN_SEC."""
        self.error_count += 1
        self._last_error_ts = _time.time()

    def error_count_effective(self) -> int:
        """Error count after applying the cooldown. If no error has occurred
        for CIRCUIT_BREAKER_COOLDOWN_SEC, the breaker resets to 0 so a
        transient burst doesn't lock entries permanently (previously the
        cooldown config was defined but never used)."""
        cooldown = config.CIRCUIT_BREAKER_COOLDOWN_SEC
        if (self.error_count > 0 and cooldown > 0
                and _time.time() - self._last_error_ts >= cooldown):
            log.info("api_breaker_cooldown_reset",
                     prev=self.error_count, cooldown_sec=cooldown)
            self.error_count = 0
        return self.error_count

    def connect(self) -> None:
        """Initialize and connect the derive-client."""
        from derive_client import HTTPClient

        self._client = HTTPClient.from_env()
        self._client.connect()
        log.info("derive_client_connected",
                 env=config.DERIVE_ENV,
                 wallet=config.DERIVE_WALLET[:10] + "..." if config.DERIVE_WALLET else "N/A",
                 subaccount=config.DERIVE_SUBACCOUNT_ID)

    # ──────────────────── Market Data ─────────────────────────────

    async def get_ticker(self, instrument_name: str) -> TickerSnapshot:
        """Fetch current bid/ask/mark for an instrument via bulk tickers."""
        try:
            parts = instrument_name.split("-")
            expiry = parts[1] if len(parts) >= 3 else ""
            currency = parts[0] if parts else "BTC"

            tickers = await self.get_tickers_for_expiry(currency, expiry)
            if instrument_name in tickers:
                return tickers[instrument_name]

            return TickerSnapshot()
        except Exception:
            log.warning("get_ticker_failed", instrument=instrument_name, exc_info=True)
            self._note_error()
            return TickerSnapshot()

    async def get_tickers_for_expiry(
        self, currency: str, expiry_date: str,
    ) -> dict[str, TickerSnapshot]:
        """Bulk-fetch tickers for all options of an expiry (YYYYMMDD)."""
        from derive_client.data_types import AssetType
        try:
            data = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.markets.get_tickers(
                    instrument_type=AssetType.option,
                    currency=currency,
                    expiry_date=expiry_date,
                )
            )
            result = {}
            for name, t in data.items():
                bid = float(getattr(t, "b", 0) or getattr(t, "best_bid_price", 0) or 0)
                ask = float(getattr(t, "a", 0) or getattr(t, "best_ask_price", 0) or 0)
                mark = float(getattr(t, "M", 0) or getattr(t, "mark_price", 0) or 0)
                index = float(getattr(t, "I", 0) or getattr(t, "index_price", 0) or 0)
                result[name] = TickerSnapshot(bid=bid, ask=ask, mark=mark, index=index)
            return result
        except Exception:
            log.warning("get_tickers_failed", currency=currency, expiry=expiry_date,
                        exc_info=True)
            self._note_error()
            return {}

    async def get_spot_price(self) -> float:
        """Get BTC spot/index price from the perp ticker."""
        from derive_client.data_types import AssetType
        try:
            data = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.markets.get_tickers(
                    instrument_type=AssetType.perp, currency=config.BASE_COIN,
                )
            )
            perp_key = f"{config.BASE_COIN}-PERP"
            if perp_key in data:
                t = data[perp_key]
                index = float(getattr(t, "I", 0) or getattr(t, "index_price", 0) or 0)
                mark = float(getattr(t, "M", 0) or getattr(t, "mark_price", 0) or 0)
                if index > 0:
                    return index
                if mark > 0:
                    return mark
        except Exception:
            log.warning("get_spot_price_via_tickers_failed", exc_info=True)

        ticker = await self.get_ticker(f"{config.BASE_COIN}-PERP")
        if ticker.index > 0:
            return ticker.index
        if ticker.mark > 0:
            return ticker.mark
        raise RuntimeError("Cannot fetch BTC spot price from Derive")

    async def get_instruments(
        self, currency: str = "BTC", kind: str = "option",
    ) -> list:
        """Fetch all active instruments for a currency and type."""
        from derive_client.data_types import AssetType
        type_map = {"option": AssetType.option, "perp": AssetType.perp}
        asset_type = type_map.get(kind, AssetType.option)

        instruments = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._client.markets.get_instruments(
                currency=currency, expired=False, instrument_type=asset_type,
            )
        )
        return instruments if isinstance(instruments, list) else []

    async def get_subaccount_equity(self) -> float:
        """Get the subaccount's total equity (positions + collateral) in USD."""
        try:
            sub = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.active_subaccount.refresh()
            )
            state = sub.state
            equity = float(getattr(state, "subaccount_value", 0) or 0)
            collateral = float(getattr(state, "collaterals_value", 0) or 0)
            log.debug("subaccount_state", equity=equity, collateral=collateral)
            return equity if equity > 0 else collateral
        except Exception:
            log.warning("get_equity_failed", exc_info=True)
            return 0.0

    # ──────────────────── Order Placement ─────────────────────────

    async def _place_limit_order(
        self, instrument: str, direction: str, qty: float, price: float,
        post_only: bool = True,
    ) -> dict:
        """Place a limit order via derive-client (handles EIP-712 signing).

        post_only=True (default): order is rejected by exchange if it would
        cross the spread — guarantees maker fill or no fill. Returns
        {'rejected_post_only': True} when rejected so the caller can
        reprice and retry.
        """
        from derive_client.data_types import D, Direction, OrderType

        dir_enum = Direction.buy if direction == "buy" else Direction.sell
        try:
            create_kwargs = dict(
                instrument_name=instrument,
                amount=D(str(qty)),
                limit_price=D(str(price)),
                direction=dir_enum,
                order_type=OrderType.limit,
            )
            tif_name = "post_only" if post_only else "gtc"
            try:
                from derive_client.data_types import TimeInForce
                tif_enum = getattr(TimeInForce, tif_name, None)
                create_kwargs["time_in_force"] = tif_enum if tif_enum is not None else tif_name
            except ImportError:
                create_kwargs["time_in_force"] = tif_name

            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.orders.create(**create_kwargs)
            )
            order_id = getattr(result, "order_id", "")
            status = getattr(result, "order_status", "")
            avg_price = getattr(result, "average_price", price)

            log.debug("order_placed", instrument=instrument, direction=direction,
                      qty=qty, price=price, order_id=order_id, status=status,
                      post_only=post_only)
            return {"order_id": str(order_id), "order_status": str(status),
                    "average_price": str(avg_price)}
        except Exception as exc:
            err_str = str(exc)
            err_upper = err_str.upper()
            if _is_insufficient_funds(err_str):
                log.error("order_insufficient_funds", instrument=instrument,
                          direction=direction, qty=qty, price=price,
                          error=err_str)
                return {"rejected_insufficient_funds": True, "error": err_str}
            if post_only and (
                "POST_ONLY" in err_upper
                or "WOULD_CROSS" in err_upper
                or "WOULD CROSS" in err_upper
                or "CROSS_SPREAD" in err_upper
                or "CROSSES" in err_upper
            ):
                log.debug("post_only_rejected", instrument=instrument, price=price)
                return {"rejected_post_only": True}
            log.warning("order_failed", instrument=instrument, error=err_str)
            self._note_error()
            return {}

    async def taker_flatten(
        self, instrument: str, signed_amt: float,
    ) -> Optional[dict]:
        """Flatten one leg with a TAKER cross (post_only=False), guaranteeing
        a fill by crossing the spread. Used by the post-close reconcile as an
        escalation after maker rounds fail (OKX parity). ``signed_amt`` is the
        live position (+long / −short, in BTC): a long is SOLD at the bid, a
        short is BOUGHT at the ask. Returns the order dict on success (with
        ``average_price``), or None on a bad book / rejection.
        """
        qty = abs(signed_amt)
        if qty < 1e-9:
            return None
        try:
            ticker = await self.get_ticker(instrument)
        except Exception:
            log.warning("taker_flatten_ticker_failed", instrument=instrument)
            return None
        bid, ask = float(ticker.bid), float(ticker.ask)
        if bid <= 0 or ask <= 0:
            log.warning("taker_flatten_bad_book", instrument=instrument,
                        bid=bid, ask=ask)
            return None
        if signed_amt > 0:
            direction, price = "sell", bid   # cross DOWN to guarantee fill
        else:
            direction, price = "buy", ask     # cross UP
        log.warning("taker_flatten_crossing", instrument=instrument,
                    direction=direction, qty=qty, price=price,
                    book=f"{bid}/{ask}")
        order = await self._place_limit_order(
            instrument, direction, qty, price, post_only=False,
        )
        if order.get("rejected_insufficient_funds"):
            log.error("taker_flatten_insufficient_funds", instrument=instrument)
            return None
        if not order.get("order_id"):
            log.error("taker_flatten_not_accepted", instrument=instrument,
                      order=order)
            return None
        return order

    async def _get_order(self, order_id: str) -> dict:
        """Get the state of a single order."""
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.orders.get(order_id=order_id)
            )
            return {
                "order_id": str(getattr(result, "order_id", "")),
                "order_status": str(getattr(result, "order_status", "")),
                "average_price": str(getattr(result, "average_price", 0)),
                "filled_amount": str(getattr(result, "filled_amount", 0)),
            }
        except Exception:
            return {}

    async def _wait_fill(self, order_id: str, timeout: float) -> dict:
        """Poll for order fill status."""
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            order = await self._get_order(order_id)
            status = order.get("order_status", "")
            if status == "filled":
                return order
            if status in ("cancelled", "rejected", "expired"):
                return {}
            await asyncio.sleep(0.5)
        return {}

    async def cancel_order(self, order_id: str, instrument: str) -> None:
        """Cancel an open order."""
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.orders.cancel(
                    instrument_name=instrument, order_id=order_id,
                )
            )
        except Exception:
            log.debug("cancel_failed", order_id=order_id, exc_info=True)

    async def list_open_orders(self) -> list[dict]:
        """List all open orders on the active subaccount."""
        try:
            sub = self._client.active_subaccount
            orders = await asyncio.get_running_loop().run_in_executor(
                None, lambda: list(sub.orders.list_open())
            )
            out = []
            for o in orders:
                out.append({
                    "order_id": str(getattr(o, "order_id", "")),
                    "instrument_name": str(getattr(o, "instrument_name", "")),
                    "direction": str(getattr(o, "direction", "")),
                    "amount": str(getattr(o, "amount", "")),
                    "limit_price": str(getattr(o, "limit_price", "")),
                    "order_status": str(getattr(o, "order_status", "")),
                })
            return out
        except Exception:
            log.warning("list_open_orders_failed", exc_info=True)
            return []

    async def cancel_all_open_orders(self) -> int:
        """Cancel every resting order on the active subaccount.

        Returns the number of orders cancelled. Called at startup to clear
        stale orders left from a previous run (which eat margin).
        """
        orders = await self.list_open_orders()
        if not orders:
            log.info("cancel_all_no_open_orders")
            return 0

        log.warning("cancel_all_found_stale_orders", count=len(orders),
                    orders=[f"{o['instrument_name']} {o['direction']} "
                            f"{o['amount']}@{o['limit_price']}"
                            for o in orders])

        cancelled = 0
        for o in orders:
            oid = o.get("order_id", "")
            inst = o.get("instrument_name", "")
            if not oid or not inst:
                continue
            try:
                await self.cancel_order(oid, inst)
                cancelled += 1
            except Exception:
                log.warning("cancel_one_failed", order_id=oid,
                            instrument=inst, exc_info=True)
        log.info("cancel_all_done", cancelled=cancelled, attempted=len(orders))
        return cancelled

    async def list_open_positions(self) -> list[dict]:
        """List non-zero positions on the active subaccount."""
        try:
            sub = self._client.active_subaccount
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: sub.refresh()
            )
            positions = await asyncio.get_running_loop().run_in_executor(
                None, lambda: list(sub.positions.list())
            )
            out = []
            for p in positions:
                amt = float(getattr(p, "amount", 0) or 0)
                if amt == 0:
                    continue
                out.append({
                    "instrument_name": str(getattr(p, "instrument_name", "")),
                    "amount": amt,
                    "average_price": float(getattr(p, "average_price", 0) or 0),
                    "mark_price": float(getattr(p, "mark_price", 0) or 0),
                    "unrealized_pnl": float(getattr(p, "unrealized_pnl", 0) or 0),
                })
            return out
        except Exception:
            log.warning("list_positions_failed", exc_info=True)
            return []

    # ──────────────────── Chase Buy (Escalating Maker) ────────────

    async def chase_buy(
        self, instrument: str, qty: float, initial_bid: float,
    ) -> dict | None:
        """
        Maker-only buy with 50%-gap narrowing + fair-value cap + deadline.

        On each attempt:
          - new_price = current_price + (ask - current_price) * GAP_NARROW_PCT
          - price is hard-capped at min(ask - tick, mark * MAX_SLIPPAGE_FACTOR)
          - if cap reached and still no fill, waits (does not cross)

        Aborts on "Insufficient funds" errors (propagates rejected_insufficient_funds).
        Uses post_only (exchange rejects if crosses spread). Never a taker.
        """
        if config.DRY_RUN:
            return {"order_id": f"dry-{uuid.uuid4().hex[:12]}",
                    "order_status": "filled", "average_price": str(initial_bid)}

        log.info("chase_buy_maker_start", instrument=instrument, qty=qty,
                 initial_bid=initial_bid,
                 gap_pct=config.OPTION_CHASE_GAP_NARROW_PCT,
                 slip_factor=config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR,
                 deadline_min=config.OPTION_CHASE_DEADLINE_MIN)
        tick = config.OPTION_TICK_SIZE
        remaining_qty = qty
        weighted_cost = 0.0
        total_filled = 0.0

        deadline = _time.time() + config.OPTION_CHASE_DEADLINE_MIN * 60.0
        current_price = _round_price(
            initial_bid if initial_bid > 0 else tick, "down")
        attempt = 0

        while _time.time() < deadline and remaining_qty > 0:
            attempt += 1
            ticker = await self.get_ticker(instrument)

            ask = ticker.ask if ticker.ask > 0 else initial_bid + tick * 10
            bid = ticker.bid if ticker.bid > 0 else initial_bid
            mark = ticker.mark if ticker.mark > 0 else (bid + ask) / 2.0

            slip_cap = mark * config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
            spread_cap = ask - tick
            hard_cap = min(slip_cap, spread_cap)

            if attempt == 1:
                candidate = max(bid, current_price)
            else:
                gap = max(0.0, ask - current_price)
                candidate = current_price + gap * config.OPTION_CHASE_GAP_NARROW_PCT
                candidate = max(candidate, current_price + tick)

            price = _round_price(min(candidate, hard_cap), "up")
            if price < tick:
                price = tick

            if price > slip_cap:
                log.warning("chase_buy_cap_reached", instrument=instrument,
                            price=price, slip_cap=slip_cap, mark=mark,
                            attempt=attempt,
                            remaining_qty=remaining_qty)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            current_price = price

            result = await self._place_limit_order(
                instrument, "buy", remaining_qty, price, post_only=True)
            if result.get("rejected_insufficient_funds"):
                log.error("chase_buy_insufficient_funds_abort",
                          instrument=instrument, price=price,
                          total_filled=total_filled, remaining=remaining_qty)
                return {"rejected_insufficient_funds": True,
                        "error": result.get("error", "insufficient funds"),
                        "filled_amount": str(total_filled),
                        "remaining_amount": str(remaining_qty),
                        "average_price": str(weighted_cost / total_filled)
                                         if total_filled > 0 else "0"}
            if result.get("rejected_post_only"):
                log.debug("chase_buy_post_only_reject", instrument=instrument,
                          price=price, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue
            order_id = result.get("order_id", "")
            if not order_id:
                break

            if result.get("order_status") == "filled":
                fill_price = float(result.get("average_price", price))
                weighted_cost += fill_price * remaining_qty
                total_filled += remaining_qty
                avg_price = weighted_cost / total_filled
                log.info("chase_filled_immediate", instrument=instrument,
                         price=fill_price, total_filled=total_filled,
                         attempt=attempt)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(avg_price)}

            fill = await self._wait_fill(order_id,
                                         timeout=config.OPTION_CHASE_INTERVAL_SEC)

            if fill and fill.get("order_status") == "filled":
                fill_price = float(fill.get("average_price", price))
                weighted_cost += fill_price * remaining_qty
                total_filled += remaining_qty
                avg_price = weighted_cost / total_filled
                log.info("chase_filled", instrument=instrument,
                         price=fill_price, total_filled=total_filled,
                         attempt=attempt)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(avg_price)}

            await self.cancel_order(order_id, instrument)

            final = await self._get_order(order_id)
            if final.get("order_status") == "filled":
                fill_price = float(final.get("average_price", price))
                weighted_cost += fill_price * remaining_qty
                total_filled += remaining_qty
                avg_price = weighted_cost / total_filled
                log.info("chase_filled_post_cancel", instrument=instrument,
                         price=fill_price, total_filled=total_filled,
                         attempt=attempt)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(avg_price)}

            filled_amt = float(final.get("filled_amount", 0) or 0)
            if filled_amt > 0:
                fill_price = float(final.get("average_price", price))
                weighted_cost += fill_price * filled_amt
                total_filled += filled_amt
                remaining_qty -= filled_amt
                remaining_qty = round(remaining_qty, 8)

                if remaining_qty <= 0:
                    avg_price = weighted_cost / total_filled
                    log.info("chase_filled_post_cancel", instrument=instrument,
                             price=fill_price, total_filled=total_filled,
                             attempt=attempt)
                    return {"order_id": order_id, "order_status": "filled",
                            "average_price": str(avg_price)}

                log.info("chase_partial_fill", instrument=instrument,
                         filled=filled_amt, remaining=remaining_qty,
                         attempt=attempt)

            log.debug("chase_reprice", instrument=instrument,
                      attempt=attempt, remaining_qty=remaining_qty,
                      price=price)

        if total_filled > 0 and remaining_qty > 0:
            avg_price = weighted_cost / total_filled
            log.warning("chase_buy_partial_deadline", instrument=instrument,
                        total_filled=total_filled, remaining=remaining_qty,
                        avg_price=avg_price, attempts=attempt)
            return {"order_id": "", "order_status": "partial",
                    "average_price": str(avg_price),
                    "filled_amount": str(total_filled),
                    "remaining_amount": str(remaining_qty)}

        log.warning("chase_buy_deadline_no_fill", instrument=instrument,
                    total_filled=total_filled, remaining=remaining_qty,
                    attempts=attempt)
        return None

    # ──────────────────── Chase Sell (Escalating Maker) ───────────

    async def chase_sell(
        self, instrument: str, qty: float, initial_ask: float,
    ) -> dict | None:
        """
        Maker-only sell with 50%-gap narrowing + fair-value floor + deadline.

        On each attempt:
          - new_price = current_price - (current_price - bid) * GAP_NARROW_PCT
          - price is hard-floored at max(bid + tick, mark / MAX_SLIPPAGE_FACTOR)

        Aborts on "Insufficient funds" errors. Uses post_only. Never a taker.
        """
        if config.DRY_RUN:
            return {"order_id": f"dry-{uuid.uuid4().hex[:12]}",
                    "order_status": "filled", "average_price": str(initial_ask)}

        log.info("chase_sell_maker_start", instrument=instrument, qty=qty,
                 initial_ask=initial_ask,
                 gap_pct=config.OPTION_CHASE_GAP_NARROW_PCT,
                 slip_factor=config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR,
                 deadline_min=config.OPTION_CHASE_DEADLINE_MIN)
        tick = config.OPTION_TICK_SIZE
        remaining_qty = qty
        weighted_revenue = 0.0
        total_filled = 0.0

        deadline = _time.time() + config.OPTION_CHASE_DEADLINE_MIN * 60.0
        current_price = _round_price(
            initial_ask if initial_ask > 0 else tick, "up")
        attempt = 0

        while _time.time() < deadline and remaining_qty > 0:
            attempt += 1
            ticker = await self.get_ticker(instrument)

            ask = ticker.ask if ticker.ask > 0 else initial_ask
            bid = ticker.bid if ticker.bid > 0 else initial_ask - tick * 10
            mark = ticker.mark if ticker.mark > 0 else (bid + ask) / 2.0

            slip_floor = mark / config.OPTION_CHASE_MAX_SLIPPAGE_FACTOR
            spread_floor = bid + tick
            hard_floor = max(slip_floor, spread_floor)

            if attempt == 1:
                candidate = min(ask, current_price) if current_price > 0 else ask
            else:
                gap = max(0.0, current_price - bid)
                candidate = current_price - gap * config.OPTION_CHASE_GAP_NARROW_PCT
                candidate = min(candidate, current_price - tick)

            price = _round_price(max(candidate, hard_floor), "down")
            if price < tick:
                price = tick

            if price < slip_floor:
                log.warning("chase_sell_floor_reached", instrument=instrument,
                            price=price, slip_floor=slip_floor, mark=mark,
                            attempt=attempt, remaining_qty=remaining_qty)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue

            current_price = price

            result = await self._place_limit_order(
                instrument, "sell", remaining_qty, price, post_only=True)
            if result.get("rejected_insufficient_funds"):
                log.error("chase_sell_insufficient_funds_abort",
                          instrument=instrument, price=price,
                          total_filled=total_filled, remaining=remaining_qty)
                return {"rejected_insufficient_funds": True,
                        "error": result.get("error", "insufficient funds"),
                        "filled_amount": str(total_filled),
                        "remaining_amount": str(remaining_qty),
                        "average_price": str(weighted_revenue / total_filled)
                                         if total_filled > 0 else "0"}
            if result.get("rejected_post_only"):
                log.debug("chase_sell_post_only_reject", instrument=instrument,
                          price=price, attempt=attempt)
                await asyncio.sleep(config.OPTION_CHASE_INTERVAL_SEC)
                continue
            order_id = result.get("order_id", "")
            if not order_id:
                break

            if result.get("order_status") == "filled":
                fill_price = float(result.get("average_price", price))
                weighted_revenue += fill_price * remaining_qty
                total_filled += remaining_qty
                avg_price = weighted_revenue / total_filled
                log.info("chase_sell_filled_immediate", instrument=instrument,
                         price=fill_price, total_filled=total_filled,
                         attempt=attempt)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(avg_price)}

            fill = await self._wait_fill(order_id,
                                         timeout=config.OPTION_CHASE_INTERVAL_SEC)

            if fill and fill.get("order_status") == "filled":
                fill_price = float(fill.get("average_price", price))
                weighted_revenue += fill_price * remaining_qty
                total_filled += remaining_qty
                avg_price = weighted_revenue / total_filled
                log.info("chase_sell_filled", instrument=instrument,
                         price=fill_price, total_filled=total_filled,
                         attempt=attempt)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(avg_price)}

            await self.cancel_order(order_id, instrument)

            final = await self._get_order(order_id)
            if final.get("order_status") == "filled":
                fill_price = float(final.get("average_price", price))
                weighted_revenue += fill_price * remaining_qty
                total_filled += remaining_qty
                avg_price = weighted_revenue / total_filled
                log.info("chase_sell_filled_post_cancel", instrument=instrument,
                         price=fill_price, total_filled=total_filled,
                         attempt=attempt)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(avg_price)}

            filled_amt = float(final.get("filled_amount", 0) or 0)
            if filled_amt > 0:
                fill_price = float(final.get("average_price", price))
                weighted_revenue += fill_price * filled_amt
                total_filled += filled_amt
                remaining_qty -= filled_amt
                remaining_qty = round(remaining_qty, 8)

                if remaining_qty <= 0:
                    avg_price = weighted_revenue / total_filled
                    log.info("chase_sell_filled_post_cancel", instrument=instrument,
                             price=fill_price, total_filled=total_filled,
                             attempt=attempt)
                    return {"order_id": order_id, "order_status": "filled",
                            "average_price": str(avg_price)}

                log.info("chase_sell_partial_fill", instrument=instrument,
                         filled=filled_amt, remaining=remaining_qty,
                         attempt=attempt)

            log.debug("chase_sell_reprice", instrument=instrument,
                      attempt=attempt, remaining_qty=remaining_qty,
                      price=price)

        if total_filled > 0 and remaining_qty > 0:
            avg_price = weighted_revenue / total_filled
            log.warning("chase_sell_partial_deadline", instrument=instrument,
                        total_filled=total_filled, remaining=remaining_qty,
                        avg_price=avg_price, attempts=attempt)
            return {"order_id": "", "order_status": "partial",
                    "average_price": str(avg_price),
                    "filled_amount": str(total_filled),
                    "remaining_amount": str(remaining_qty)}

        log.warning("chase_sell_deadline_no_fill", instrument=instrument,
                    total_filled=total_filled, remaining=remaining_qty,
                    attempts=attempt)
        return None

    # ──────────────────── RFQ (Atomic Multi-Leg) ───────────────────

    async def send_rfq(
        self, call_instrument: str, put_instrument: str, qty: float,
    ) -> dict | None:
        """
        Send an RFQ for a straddle (buy call + buy put) and execute the
        best quote. Returns fill details or None on failure.
        """
        from derive_client.data_types.generated_models import LegUnpricedSchema, Direction
        from derive_client.data_types import D

        legs = [
            LegUnpricedSchema(
                instrument_name=call_instrument,
                amount=D(str(qty)),
                direction=Direction.buy,
            ),
            LegUnpricedSchema(
                instrument_name=put_instrument,
                amount=D(str(qty)),
                direction=Direction.buy,
            ),
        ]

        log.info("rfq_send", call=call_instrument, put=put_instrument, qty=qty)

        try:
            rfq_result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.active_subaccount.rfq.send_rfq(legs=legs)
            )
            rfq_id = getattr(rfq_result, "rfq_id", "")
            log.info("rfq_created", rfq_id=rfq_id)
        except Exception:
            log.error("rfq_send_failed", exc_info=True)
            self._note_error()
            return None

        best_quote = await self._poll_for_best_quote(rfq_id, legs, timeout=60.0)
        if best_quote is None:
            log.warning("rfq_no_quotes", rfq_id=rfq_id)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._client.active_subaccount.rfq.cancel_rfq(
                        rfq_id=rfq_id))
            except Exception:
                pass
            return None

        quote_id = getattr(best_quote, "quote_id", "")
        quote_legs = getattr(best_quote, "legs", [])
        direction = getattr(best_quote, "direction", None)

        log.info("rfq_best_quote", rfq_id=rfq_id, quote_id=quote_id,
                 maker_direction=str(direction),
                 legs=[f"{getattr(l, 'instrument_name', '?')}@{getattr(l, 'price', '?')}"
                       for l in quote_legs])

        taker_direction = Direction.sell if direction == Direction.buy else Direction.buy
        try:
            exec_result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.active_subaccount.rfq.execute_quote(
                    direction=taker_direction,
                    legs=quote_legs,
                    quote_id=quote_id,
                    rfq_id=rfq_id,
                )
            )
            status = str(getattr(exec_result, "status", ""))
            log.info("rfq_executed", rfq_id=rfq_id, quote_id=quote_id, status=status)

            call_price = 0.0
            put_price = 0.0
            for leg in quote_legs:
                name = getattr(leg, "instrument_name", "")
                price = float(getattr(leg, "price", 0))
                if name.endswith("-C"):
                    call_price = price
                elif name.endswith("-P"):
                    put_price = price

            return {
                "rfq_id": rfq_id,
                "quote_id": quote_id,
                "status": status,
                "call_price": call_price,
                "put_price": put_price,
                "qty": qty,
            }
        except Exception:
            log.error("rfq_execute_failed", rfq_id=rfq_id, exc_info=True)
            self._note_error()
            return None

    async def send_rfq_sell(
        self, call_instrument: str, put_instrument: str, qty: float,
    ) -> dict | None:
        """Send an RFQ to sell a straddle (sell call + sell put)."""
        from derive_client.data_types.generated_models import LegUnpricedSchema, Direction
        from derive_client.data_types import D

        legs = [
            LegUnpricedSchema(
                instrument_name=call_instrument,
                amount=D(str(qty)),
                direction=Direction.sell,
            ),
            LegUnpricedSchema(
                instrument_name=put_instrument,
                amount=D(str(qty)),
                direction=Direction.sell,
            ),
        ]

        log.info("rfq_sell_send", call=call_instrument, put=put_instrument, qty=qty)

        try:
            rfq_result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.active_subaccount.rfq.send_rfq(legs=legs)
            )
            rfq_id = getattr(rfq_result, "rfq_id", "")
            log.info("rfq_sell_created", rfq_id=rfq_id)
        except Exception:
            log.error("rfq_sell_send_failed", exc_info=True)
            self._note_error()
            return None

        best_quote = await self._poll_for_best_quote(rfq_id, legs, timeout=60.0)
        if best_quote is None:
            log.warning("rfq_sell_no_quotes", rfq_id=rfq_id)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._client.active_subaccount.rfq.cancel_rfq(
                        rfq_id=rfq_id))
            except Exception:
                pass
            return None

        quote_id = getattr(best_quote, "quote_id", "")
        quote_legs = getattr(best_quote, "legs", [])
        direction = getattr(best_quote, "direction", None)

        log.info("rfq_sell_best_quote", rfq_id=rfq_id, quote_id=quote_id,
                 maker_direction=str(direction))

        taker_direction = Direction.sell if direction == Direction.buy else Direction.buy
        try:
            exec_result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.active_subaccount.rfq.execute_quote(
                    direction=taker_direction,
                    legs=quote_legs,
                    quote_id=quote_id,
                    rfq_id=rfq_id,
                )
            )
            status = str(getattr(exec_result, "status", ""))
            log.info("rfq_sell_executed", rfq_id=rfq_id, status=status)

            call_price = 0.0
            put_price = 0.0
            for leg in quote_legs:
                name = getattr(leg, "instrument_name", "")
                price = float(getattr(leg, "price", 0))
                if name.endswith("-C"):
                    call_price = price
                elif name.endswith("-P"):
                    put_price = price

            return {
                "rfq_id": rfq_id,
                "quote_id": quote_id,
                "status": status,
                "call_price": call_price,
                "put_price": put_price,
                "qty": qty,
            }
        except Exception:
            log.error("rfq_sell_execute_failed", rfq_id=rfq_id, exc_info=True)
            self._note_error()
            return None

    async def _poll_for_best_quote(
        self, rfq_id: str, legs, timeout: float = 60.0,
    ):
        """Poll for quotes on an RFQ until timeout. Returns best quote or None."""
        from derive_client.data_types import Direction

        deadline = _time.time() + timeout
        best = None

        while _time.time() < deadline:
            await asyncio.sleep(2.0)
            try:
                quotes_result = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._client.active_subaccount.rfq.poll_quotes(
                        rfq_id=rfq_id)
                )
                quotes = getattr(quotes_result, "quotes", [])
                if quotes:
                    best = quotes[0]
                    for q in quotes[1:]:
                        best_cost = sum(
                            float(getattr(l, "price", 0)) * float(getattr(l, "amount", 0))
                            for l in getattr(best, "legs", []))
                        q_cost = sum(
                            float(getattr(l, "price", 0)) * float(getattr(l, "amount", 0))
                            for l in getattr(q, "legs", []))
                        if q_cost < best_cost:
                            best = q
                    log.info("rfq_quotes_received", rfq_id=rfq_id,
                             num_quotes=len(quotes))
                    return best
            except Exception:
                log.debug("rfq_poll_error", rfq_id=rfq_id, exc_info=True)

        return best
