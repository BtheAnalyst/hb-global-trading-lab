"""Broker + market-data access for the intraday sleeve. Alpaca PAPER only (paper=True is hard-coded and
risk.assert_paper_or_approved blocks anything else). Whole-share BRACKET orders so the stop and the target
live at the broker even if this program dies."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd

from .equity_risk import LEVERAGED_OR_INVERSE
from .gateway import is_option_symbol
from .risk import assert_paper_or_approved

FINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}


class AlpacaIntradayGateway:  # pragma: no cover - network
    def __init__(self, feed: str = "iex"):
        assert_paper_or_approved("paper")
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient
        k, s = os.environ["ALPACA_PAPER_KEY"].strip(), os.environ["ALPACA_PAPER_SECRET"].strip()
        self.tc = TradingClient(k, s, paper=True)
        self.dc = StockHistoricalDataClient(k, s)
        from alpaca.data.enums import DataFeed
        self.feed = DataFeed.IEX if feed == "iex" else DataFeed.SIP

    # ---------- market data ----------
    def clock(self) -> dict:
        c = self.tc.get_clock()
        return {"is_open": bool(c.is_open), "next_open": c.next_open, "next_close": c.next_close,
                "now": c.timestamp}

    def bars(self, symbols: list[str], start: datetime, minutes: int = 1) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        if not symbols:
            return {}
        tf = TimeFrame.Minute if minutes == 1 else TimeFrame(minutes, TimeFrameUnit.Minute)
        out = {}
        for i in range(0, len(symbols), 100):
            df = self.dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbols[i:i + 100], timeframe=tf,
                                                         start=start, feed=self.feed,
                                                         adjustment=Adjustment.SPLIT)).df
            if df is None or df.empty:
                continue
            df = df.reset_index().rename(columns={"timestamp": "t", "open": "o", "high": "h", "low": "l",
                                                  "close": "c", "volume": "v"})
            for sym, g in df.groupby("symbol"):
                out[sym] = g[["t", "o", "h", "l", "c", "v"]].reset_index(drop=True)
        return out

    def daily(self, symbols: list[str], days: int = 45) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        start = datetime.now(timezone.utc) - timedelta(days=days)
        df = self.dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                                                     start=start, feed=self.feed, adjustment=Adjustment.SPLIT)).df
        return {sym: g.reset_index() for sym, g in df.reset_index().groupby("symbol")} if df is not None else {}

    def daily_sip(self, symbols: list[str], days: int = 45) -> dict[str, pd.DataFrame]:
        """Consolidated (SIP) daily bars up to yesterday: allowed on the free plan (older than 15 minutes).
        Used for average daily volume, which IEX alone would understate ~50x."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        end = datetime.now(timezone.utc) - timedelta(minutes=20)
        df = self.dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                                                     start=end - timedelta(days=days), end=end, feed=DataFeed.SIP,
                                                     adjustment=Adjustment.SPLIT)).df
        return {sym: g.reset_index() for sym, g in df.reset_index().groupby("symbol")} if df is not None else {}

    def gainers(self, top: int = 50) -> list[dict]:
        """Market movers (top % gainers) from Alpaca's screener."""
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MarketMoversRequest
        import os as _os
        sc = ScreenerClient(_os.environ["ALPACA_PAPER_KEY"].strip(), _os.environ["ALPACA_PAPER_SECRET"].strip())
        r = sc.get_market_movers(MarketMoversRequest(top=top))
        return [{"symbol": g.symbol, "pct": float(g.percent_change), "price": float(g.price)} for g in r.gainers]

    def opening_range_sip(self, symbols: list[str], open_utc) -> dict[str, tuple]:
        """Consolidated 9:30-9:44 high/low. The plan allows SIP data once it is 15+ minutes old (from ~10:00 ET)."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        out = {}
        for i in range(0, len(symbols), 100):
            df = self.dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbols[i:i + 100], timeframe=TimeFrame.Minute,
                                                         start=open_utc, end=open_utc + timedelta(minutes=15),
                                                         feed=DataFeed.SIP, adjustment=Adjustment.SPLIT)).df
            if df is None or df.empty:
                continue
            for sym, g in df.reset_index().groupby("symbol"):
                if len(g) >= 12:
                    out[sym] = (float(g["high"].max()), float(g["low"].min()))
        return out

    def quote(self, sym: str):
        from alpaca.data.requests import StockLatestQuoteRequest
        q = self.dc.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym, feed=self.feed)).get(sym)
        if q is None:
            return None
        return float(q.bid_price or 0), float(q.ask_price or 0), (datetime.now(timezone.utc) - q.timestamp).total_seconds()

    def assets(self, symbols) -> dict:
        out = {}
        for s in symbols:
            try:
                a = self.tc.get_asset(s)
                out[s] = {"symbol": s, "tradable": bool(a.tradable), "status": str(a.status.value), "name": a.name or "",
                          "exchange": str(a.exchange.value), "class": str(a.asset_class.value)}
            except Exception:  # noqa: BLE001
                out[s] = None
        return out

    # ---------- account ----------
    def account(self) -> dict:
        a = self.tc.get_account()
        return {"status": str(getattr(a.status, "value", a.status)), "cash": float(a.cash),
                "trading_blocked": bool(a.trading_blocked), "account_blocked": bool(a.account_blocked),
                "multiplier": str(a.multiplier), "shorting_enabled": bool(a.shorting_enabled),
                "equity": float(a.equity)}

    def position_qty(self, sym: str) -> float:
        try:
            return float(self.tc.get_open_position(sym).qty)
        except Exception:  # noqa: BLE001 - no position
            return 0.0

    def positions(self) -> dict:
        return {p.symbol: float(p.qty) for p in self.tc.get_all_positions()}

    # ---------- orders ----------
    def submit_bracket(self, cid: str, sym: str, qty: int, limit: float, stop: float, target: float) -> dict:
        _guard(sym)
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest
        if int(qty) != qty or qty < 1:
            raise ValueError("bracket orders need whole shares")
        o = self.tc.submit_order(LimitOrderRequest(
            symbol=sym, qty=int(qty), side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            limit_price=round(limit, 2), order_class=OrderClass.BRACKET, client_order_id=cid,
            take_profit=TakeProfitRequest(limit_price=round(target, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop, 2))))
        return self._o(o)

    def order(self, cid: str) -> dict | None:
        try:
            return self._o(self.tc.get_order_by_client_id(cid))
        except Exception:  # noqa: BLE001
            return None

    def cancel(self, order_id: str) -> None:
        try:
            self.tc.cancel_order_by_id(order_id)
        except Exception:  # noqa: BLE001 - already final
            pass

    def market_sell(self, cid: str, sym: str, qty: float) -> dict:
        _guard(sym)
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        return self._o(self.tc.submit_order(MarketOrderRequest(symbol=sym, qty=qty, side=OrderSide.SELL,
                                                               time_in_force=TimeInForce.DAY, client_order_id=cid)))

    def open_orders(self, prefix: str) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        return [self._o(o) for o in self.tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500,
                                                                         nested=True))
                if (o.client_order_id or "").startswith(prefix)]

    @staticmethod
    def _o(o) -> dict:
        legs = [AlpacaIntradayGateway._o(x) for x in (getattr(o, "legs", None) or [])]
        return {"id": str(o.id), "cid": o.client_order_id, "symbol": o.symbol,
                "status": str(getattr(o.status, "value", o.status)),
                "type": str(getattr(o.order_type, "value", o.order_type)),
                "qty": float(o.qty or 0), "filled_qty": float(o.filled_qty or 0),
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                "stop_price": float(o.stop_price) if getattr(o, "stop_price", None) else None,
                "limit_price": float(o.limit_price) if getattr(o, "limit_price", None) else None, "legs": legs}


def _guard(sym: str) -> None:
    if is_option_symbol(sym) or sym in LEVERAGED_OR_INVERSE:
        raise PermissionError(f"{sym}: options and leveraged/inverse products are refused")
