"""Order gateways for the stock/ETF engine.

SimGateway   : fills instantly against supplied quotes (backtest replay, unit tests, and "shadow" portfolios
               that use real broker quotes but never send orders). Trading cost is modeled as price slippage
               of `cost` per side, the same assumption as research/backtest_v2.py.
AlpacaEquityGateway : Alpaca PAPER only. Fractional DAY limit orders, client order ids for duplicate
               protection. Refuses option symbols. Never used for live money (assert_paper_or_approved).
"""
from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone

from .gateway import is_option_symbol
from .risk import assert_paper_or_approved


def floor_to(x: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(x * f + 1e-6) / f


class SimGateway:
    is_broker = False

    def __init__(self, quotes=None, cost: float = 0.0005, assets: dict | None = None):
        self.quotes = quotes or {}          # sym -> (bid, ask, age_s) or a callable sym -> that tuple
        self.cost = cost
        self._assets = assets or {}
        self.orders: dict[str, dict] = {}

    def set_prices(self, px: dict) -> None:
        self.quotes = {s: (p, p, 0.0) for s, p in px.items()}

    def assets(self, symbols) -> dict:
        return {s: self._assets.get(s, {"tradable": True, "fractionable": True, "status": "active", "name": s})
                for s in symbols}

    def quote(self, sym: str):
        q = self.quotes(sym) if callable(self.quotes) else self.quotes.get(sym)
        return q

    def buy_terms(self, dollars: float, bid: float, ask: float, fractionable: bool):
        px = ask / (1 - self.cost)
        qty = dollars / px if fractionable else math.floor(dollars / px)
        return qty, px

    def sell_terms(self, qty: float, bid: float, ask: float):
        return qty, bid * (1 - self.cost)

    def submit(self, cid: str, sym: str, side: str, qty: float, limit_price: float) -> dict:
        if is_option_symbol(sym):
            raise PermissionError(f"Options orders are disabled by mandate: refused {sym}")
        if cid in self.orders:
            raise ValueError(f"duplicate client order id {cid}")
        o = {"status": "filled", "filled_qty": qty, "filled_avg_price": limit_price}
        self.orders[cid] = o
        return dict(o)

    def get_order(self, cid: str):
        return self.orders.get(cid)

    def positions(self) -> dict:
        return {}


class AlpacaEquityGateway:  # pragma: no cover - network
    is_broker = True

    def __init__(self, limit_buffer: float = 0.002):
        assert_paper_or_approved("paper")
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient
        k, s = os.environ["ALPACA_PAPER_KEY"].strip(), os.environ["ALPACA_PAPER_SECRET"].strip()
        self.trading = TradingClient(k, s, paper=True)
        self.data = StockHistoricalDataClient(k, s)
        self.buf = limit_buffer
        self.cost = 0.0

    def assets(self, symbols) -> dict:
        out = {}
        for s in symbols:
            a = self.trading.get_asset(s)
            out[s] = {"tradable": bool(a.tradable), "fractionable": bool(a.fractionable),
                      "status": str(getattr(a.status, "value", a.status)), "name": a.name or ""}
        return out

    def quote(self, sym: str):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest
        q = self.data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym, feed=DataFeed.IEX)).get(sym)
        if q is None:
            return None
        age = (datetime.now(timezone.utc) - q.timestamp).total_seconds()
        return float(q.bid_price or 0), float(q.ask_price or 0), age

    def buy_terms(self, dollars: float, bid: float, ask: float, fractionable: bool):
        limit = round(ask * (1 + self.buf), 2)
        qty = floor_to(dollars / limit, 9) if fractionable else math.floor(dollars / limit)
        return qty, limit

    def sell_terms(self, qty: float, bid: float, ask: float):
        return floor_to(qty, 9), round(bid * (1 - self.buf), 2)

    def submit(self, cid: str, sym: str, side: str, qty: float, limit_price: float) -> dict:
        if is_option_symbol(sym):
            raise PermissionError(f"Options orders are disabled by mandate: refused {sym}")
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest
        o = self.trading.submit_order(LimitOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=limit_price, client_order_id=cid))
        for _ in range(15):                       # marketable limit: normally fills within seconds
            d = self._o(o)
            if d["status"] in ("filled", "canceled", "rejected", "expired"):
                return d
            time.sleep(2)
            o = self.trading.get_order_by_client_id(cid)
        return self._o(o)

    def get_order(self, cid: str):
        try:
            return self._o(self.trading.get_order_by_client_id(cid))
        except Exception:
            return None

    def positions(self) -> dict:
        return {p.symbol: float(p.qty) for p in self.trading.get_all_positions()}

    @staticmethod
    def _o(o) -> dict:
        return {"status": str(getattr(o.status, "value", o.status)),
                "filled_qty": float(o.filled_qty or 0),
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None}
