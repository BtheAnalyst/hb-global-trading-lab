"""Broker layer.

`SimulatedBroker` runs fully offline and is what the tests use.
`AlpacaPaperBroker` targets Alpaca's PAPER endpoint only. It is written but NOT yet
tested against Alpaca (no account or keys yet); treat it as unverified until a live paper run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from .risk import assert_paper_or_approved
from .store import Store


@dataclass
class OrderResult:
    client_order_id: str
    status: str            # submitted | filled | partially_filled | rejected | duplicate
    filled_qty: int = 0
    avg_fill_price: float | None = None
    broker_order_id: str | None = None
    message: str = ""


class Broker(Protocol):
    def submit_limit(self, client_order_id: str, symbol: str, side: str, qty: int,
                     limit_price: float) -> OrderResult: ...
    def order_status(self, client_order_id: str) -> OrderResult: ...


class SimulatedBroker:
    """Fills a limit order only if the limit crosses the current quote. Never fills better than the quote."""

    def __init__(self, quotes: dict[str, tuple[float, float]] | None = None, partial: dict[str, int] | None = None):
        self.quotes = quotes or {}         # symbol -> (bid, ask)
        self.partial = partial or {}       # symbol -> qty to fill (simulate partial fills)
        self._orders: dict[str, OrderResult] = {}

    def submit_limit(self, client_order_id, symbol, side, qty, limit_price):
        if client_order_id in self._orders:
            return OrderResult(client_order_id, "duplicate", message="already submitted")
        bid, ask = self.quotes.get(symbol, (None, None))
        if bid is None:
            res = OrderResult(client_order_id, "rejected", message="no quote")
        elif (side == "buy" and limit_price >= ask) or (side == "sell" and limit_price <= bid):
            fill_qty = min(qty, self.partial.get(symbol, qty))
            px = ask if side == "buy" else bid
            res = OrderResult(client_order_id, "filled" if fill_qty == qty else "partially_filled",
                              fill_qty, px, f"sim-{client_order_id}")
        else:
            res = OrderResult(client_order_id, "submitted", broker_order_id=f"sim-{client_order_id}")
        self._orders[client_order_id] = res
        return res

    def order_status(self, client_order_id):
        return self._orders.get(client_order_id, OrderResult(client_order_id, "rejected", message="unknown"))


class AlpacaPaperBroker:  # pragma: no cover - needs network + paper keys
    """Requires: pip install alpaca-py, and env vars ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET."""

    def __init__(self):
        assert_paper_or_approved("paper")
        from alpaca.trading.client import TradingClient
        self.client = TradingClient(os.environ["ALPACA_PAPER_KEY"], os.environ["ALPACA_PAPER_SECRET"],
                                    paper=True)  # hard-coded: this class can never trade live

    def submit_limit(self, client_order_id, symbol, side, qty, limit_price):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest
        o = self.client.submit_order(LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
            client_order_id=client_order_id))
        return self._to_result(o)

    def order_status(self, client_order_id):
        return self._to_result(self.client.get_order_by_client_id(client_order_id))

    @staticmethod
    def _to_result(o) -> OrderResult:
        return OrderResult(o.client_order_id, str(o.status.value), int(float(o.filled_qty or 0)),
                           float(o.filled_avg_price) if o.filled_avg_price else None, str(o.id))


def place_order(store: Store, broker: Broker, portfolio: str, client_order_id: str, symbol: str,
                side: str, qty: int, limit_price: float) -> OrderResult:
    """Idempotent: reserve in the DB first, then submit. Accounting uses broker-confirmed fills only."""
    if not store.reserve_order(client_order_id, portfolio, symbol, side, qty, limit_price):
        existing = store.order(client_order_id)
        return OrderResult(client_order_id, "duplicate", message=f"already {existing['status']}")
    res = broker.submit_limit(client_order_id, symbol, side, qty, limit_price)
    store.update_order(client_order_id, res.status, res.broker_order_id, res.filled_qty, res.avg_fill_price)
    store.log("order", res.__dict__, portfolio)
    return res
