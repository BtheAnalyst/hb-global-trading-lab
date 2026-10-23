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

    def orders_since(self, prefix: str) -> list[dict]:
        return [{"cid": c, **o} for c, o in self.orders.items() if c.startswith(prefix)]

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
        final = ("filled", "canceled", "rejected", "expired")
        for _ in range(15):                       # marketable limit: normally fills within seconds
            d = self._o(o)
            if d["status"] in final:
                return d
            time.sleep(2)
            o = self.trading.get_order_by_client_id(cid)
        # Never leave an order working after the pass: cancel the rest, keep whatever filled.
        try:
            self.trading.cancel_order_by_id(o.id)
        except Exception:  # noqa: BLE001 - it may have just filled
            pass
        for _ in range(10):
            time.sleep(1)
            o = self.trading.get_order_by_client_id(cid)
            if self._o(o)["status"] in final:
                break
        return self._o(o)

    def orders_since(self, prefix: str, days: int = 10) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        after = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)
        out = []
        for o in self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=after, limit=500)):
            if (o.client_order_id or "").startswith(prefix):
                out.append({"cid": o.client_order_id, "symbol": o.symbol, "side": str(getattr(o.side, "value", o.side)),
                            "qty": float(o.qty or 0), **self._o(o)})
        return out

    def account(self) -> dict:
        a = self.trading.get_account()
        return {"status": str(getattr(a.status, "value", a.status)), "cash": float(a.cash),
                "buying_power": float(a.buying_power), "multiplier": str(a.multiplier),
                "shorting_enabled": bool(a.shorting_enabled), "pattern_day_trader": bool(a.pattern_day_trader),
                "trading_blocked": bool(a.trading_blocked), "account_blocked": bool(a.account_blocked)}

    def harden(self) -> dict:
        """Broker-side backstops on the PAPER account: no shorting, no margin multiplier, no options."""
        # Raw REST call (PATCH /v2/account/configurations) so it works whatever the SDK model supports.
        before = self.trading.get("/account/configurations")
        want = {"no_shorting": True, "max_margin_multiplier": "1", "max_options_trading_level": 0}
        results = {}
        for k, v in want.items():
            if k in before and str(before[k]).lower() == str(v).lower():
                results[k] = f"already {before[k]}"
                continue
            try:
                self.trading.patch("/account/configurations", {k: v})
                results[k] = f"set to {v}"
            except Exception as e:  # noqa: BLE001 - one unsupported field must not block the others
                results[k] = f"not changed ({' '.join(str(e).split())[:120]})"
        after = self.trading.get("/account/configurations")
        return {"changes": results, "now": {k: after.get(k) for k in
                ("no_shorting", "max_margin_multiplier", "max_options_trading_level", "fractional_trading",
                 "pdt_check", "suspend_trade")}}

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


class BrokerSim(SimGateway):
    """A pretend broker for controlled Gate 3 scenario tests (no network): keeps positions and a huge buying
    power like the Alpaca paper account, and can be told to reject, partially fill, go stale or 'crash'."""
    is_broker = True

    def __init__(self, *a, buying_power: float = 200_000.0, **kw):
        super().__init__(*a, **kw)
        self.pos: dict[str, float] = {}
        self.buying_power = buying_power
        self.reject_next = 0            # reject the next N orders
        self.partial = None             # fraction to fill, rest cancelled
        self.crash_after_submit = False # raise after the broker accepted and filled (simulates a dead run)
        self.age = 0.0                  # quote age in seconds

    def quote(self, sym):
        q = super().quote(sym)
        return (q[0], q[1], self.age) if q else None

    def submit(self, cid, sym, side, qty, limit_price):
        if is_option_symbol(sym):
            raise PermissionError(f"Options orders are disabled by mandate: refused {sym}")
        if cid in self.orders:
            raise ValueError(f"duplicate client order id {cid}")
        if side == "sell" and qty > self.pos.get(sym, 0.0) + 1e-9:
            o = {"status": "rejected", "filled_qty": 0.0, "filled_avg_price": None}
        elif self.reject_next > 0:
            self.reject_next -= 1
            o = {"status": "rejected", "filled_qty": 0.0, "filled_avg_price": None}
        else:
            f = qty if self.partial is None else qty * self.partial
            o = {"status": "filled" if self.partial is None else "canceled", "filled_qty": f,
                 "filled_avg_price": limit_price}
            self.pos[sym] = self.pos.get(sym, 0.0) + (f if side == "buy" else -f)
            if abs(self.pos[sym]) < 1e-9:
                self.pos.pop(sym)
        self.orders[cid] = {"symbol": sym, "side": side, "qty": qty, **o}
        if self.crash_after_submit:
            self.crash_after_submit = False
            raise SystemExit("simulated runner crash after the order was sent")
        return {k: o[k] for k in ("status", "filled_qty", "filled_avg_price")}

    def get_order(self, cid):
        o = self.orders.get(cid)
        return None if o is None else {k: o[k] for k in ("status", "filled_qty", "filled_avg_price")}

    def orders_since(self, prefix):
        return [{"cid": c, **o} for c, o in self.orders.items() if c.startswith(prefix)]

    def positions(self):
        return dict(self.pos)
