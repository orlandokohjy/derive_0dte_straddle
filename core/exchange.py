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

NON_RETRYABLE_ERRORS = {"INSUFFICIENT_MARGIN", "INSUFFICIENT_BALANCE"}


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
        """Fetch current bid/ask/mark for an instrument."""
        try:
            data = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.markets.get_ticker(
                    instrument_name=instrument_name
                )
            )
            return TickerSnapshot(
                bid=float(getattr(data, "best_bid_price", 0) or 0),
                ask=float(getattr(data, "best_ask_price", 0) or 0),
                mark=float(getattr(data, "mark_price", 0) or 0),
                index=float(getattr(data, "index_price", 0) or 0),
            )
        except Exception:
            log.warning("get_ticker_failed", instrument=instrument_name, exc_info=True)
            self.error_count += 1
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
            self.error_count += 1
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
    ) -> dict:
        """Place a GTC limit order via derive-client (handles EIP-712 signing)."""
        from derive_client.data_types import D, Direction, OrderType, TimeInForce

        dir_enum = Direction.buy if direction == "buy" else Direction.sell
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.orders.create(
                    instrument_name=instrument,
                    amount=D(str(qty)),
                    limit_price=D(str(price)),
                    direction=dir_enum,
                    order_type=OrderType.limit,
                    time_in_force=TimeInForce.gtc,
                )
            )
            order_id = getattr(result, "order_id", "")
            status = getattr(result, "order_status", "")
            avg_price = getattr(result, "average_price", price)

            log.debug("order_placed", instrument=instrument, direction=direction,
                      qty=qty, price=price, order_id=order_id, status=status)
            return {"order_id": str(order_id), "order_status": str(status),
                    "average_price": str(avg_price)}
        except Exception as exc:
            err_str = str(exc).upper()
            for non_retry in NON_RETRYABLE_ERRORS:
                if non_retry in err_str:
                    log.error("order_non_retryable", instrument=instrument, error=str(exc))
                    raise
            log.warning("order_failed", instrument=instrument, error=str(exc))
            self.error_count += 1
            return {}

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

    # ──────────────────── Chase Buy (Escalating Maker) ────────────

    async def chase_buy(
        self, instrument: str, qty: float, initial_bid: float,
    ) -> dict | None:
        """
        Escalating maker buy: start at bid, walk up by 1 tick per attempt.

        Never crosses the spread (capped at ask - 1 tick).
        """
        if config.DRY_RUN:
            return {"order_id": f"dry-{uuid.uuid4().hex[:12]}",
                    "order_status": "filled", "average_price": str(initial_bid)}

        log.info("chase_buy_maker", instrument=instrument, qty=qty)
        tick = config.OPTION_TICK_SIZE

        for attempt in range(config.OPTION_CHASE_MAX_ATTEMPTS):
            ticker = await self.get_ticker(instrument)

            if ticker.bid > 0:
                base_price = ticker.bid
                ceiling = ticker.ask - tick if ticker.ask > 0 else ticker.bid + tick * 10
            else:
                base_price = initial_bid
                ceiling = initial_bid + tick * 10

            price = _round_price(base_price + tick * attempt, "up")
            price = min(price, _round_price(ceiling, "up"))

            result = await self._place_limit_order(instrument, "buy", qty, price)
            order_id = result.get("order_id", "")
            if not order_id:
                break

            if result.get("order_status") == "filled":
                fill_price = float(result.get("average_price", price))
                log.info("chase_filled_immediate", instrument=instrument,
                         price=fill_price, attempt=attempt + 1)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(fill_price)}

            fill = await self._wait_fill(order_id,
                                         timeout=config.OPTION_CHASE_INTERVAL_SEC)

            if fill and fill.get("order_status") == "filled":
                fill_price = float(fill.get("average_price", price))
                log.info("chase_filled", instrument=instrument,
                         price=fill_price, attempt=attempt + 1)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(fill_price)}

            await self.cancel_order(order_id, instrument)

            final = await self._get_order(order_id)
            if final.get("order_status") == "filled":
                fill_price = float(final.get("average_price", price))
                log.info("chase_filled_post_cancel", instrument=instrument,
                         price=fill_price, attempt=attempt + 1)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(fill_price)}

            filled_amt = float(final.get("filled_amount", 0) or 0)
            if filled_amt > 0:
                fill_price = float(final.get("average_price", price))
                log.info("chase_partial_post_cancel", instrument=instrument,
                         price=fill_price, filled=filled_amt, attempt=attempt + 1)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(fill_price)}

            log.debug("chase_reprice", instrument=instrument,
                      attempt=attempt + 1, price=price)

        log.warning("chase_exhausted", instrument=instrument)
        return None

    # ──────────────────── Chase Sell (Escalating Maker) ───────────

    async def chase_sell(
        self, instrument: str, qty: float, initial_ask: float,
    ) -> dict | None:
        """
        Escalating maker sell: start at ask, walk down by 1 tick per attempt.

        Never crosses the spread (capped at bid + 1 tick).
        """
        if config.DRY_RUN:
            return {"order_id": f"dry-{uuid.uuid4().hex[:12]}",
                    "order_status": "filled", "average_price": str(initial_ask)}

        log.info("chase_sell_maker", instrument=instrument, qty=qty)
        tick = config.OPTION_TICK_SIZE

        for attempt in range(config.OPTION_CHASE_MAX_ATTEMPTS):
            ticker = await self.get_ticker(instrument)

            if ticker.ask > 0:
                base_price = ticker.ask
                floor_price = ticker.bid + tick if ticker.bid > 0 else ticker.ask - tick * 10
            else:
                base_price = initial_ask
                floor_price = initial_ask - tick * 10

            price = _round_price(base_price - tick * attempt, "down")
            price = max(price, _round_price(floor_price, "down"))
            if price <= 0:
                price = tick

            result = await self._place_limit_order(instrument, "sell", qty, price)
            order_id = result.get("order_id", "")
            if not order_id:
                break

            if result.get("order_status") == "filled":
                fill_price = float(result.get("average_price", price))
                log.info("chase_sell_filled_immediate", instrument=instrument,
                         price=fill_price, attempt=attempt + 1)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(fill_price)}

            fill = await self._wait_fill(order_id,
                                         timeout=config.OPTION_CHASE_INTERVAL_SEC)

            if fill and fill.get("order_status") == "filled":
                fill_price = float(fill.get("average_price", price))
                log.info("chase_sell_filled", instrument=instrument,
                         price=fill_price, attempt=attempt + 1)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(fill_price)}

            await self.cancel_order(order_id, instrument)

            final = await self._get_order(order_id)
            if final.get("order_status") == "filled":
                fill_price = float(final.get("average_price", price))
                log.info("chase_sell_filled_post_cancel", instrument=instrument,
                         price=fill_price, attempt=attempt + 1)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(fill_price)}

            filled_amt = float(final.get("filled_amount", 0) or 0)
            if filled_amt > 0:
                fill_price = float(final.get("average_price", price))
                log.info("chase_sell_partial_post_cancel", instrument=instrument,
                         price=fill_price, filled=filled_amt, attempt=attempt + 1)
                return {"order_id": order_id, "order_status": "filled",
                        "average_price": str(fill_price)}

            log.debug("chase_sell_reprice", instrument=instrument,
                      attempt=attempt + 1, price=price)

        log.warning("chase_sell_exhausted", instrument=instrument)
        return None
