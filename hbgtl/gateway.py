"""Alpaca PAPER gateway: the only module that talks to the broker.

Hard-wired to paper (paper=True); there is no code path to a live account.
Uses free data feeds: IEX for stocks, 'indicative' for options.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .risk import assert_paper_or_approved
from .selection import Candidate

ET = ZoneInfo("America/New_York")


class AlpacaPaperGateway:  # pragma: no cover - network
    def __init__(self):
        assert_paper_or_approved("paper")
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient
        k, s = os.environ["ALPACA_PAPER_KEY"].strip(), os.environ["ALPACA_PAPER_SECRET"].strip()
        self.trading = TradingClient(k, s, paper=True)
        self.stocks = StockHistoricalDataClient(k, s)
        self.options = OptionHistoricalDataClient(k, s)

    # ----- time -----
    def clock(self) -> tuple[bool, datetime]:
        c = self.trading.get_clock()
        return bool(c.is_open), datetime.now(timezone.utc).astimezone(ET)

    # ----- stock data -----
    def daily_bars(self, symbols: list[str], days: int = 120) -> dict[str, tuple[list[float], list[float]]]:
        """Completed daily bars only (today's partial bar is dropped)."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        start = datetime.now(timezone.utc) - timedelta(days=int(days * 1.6))
        bars = self.stocks.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start,
            feed=DataFeed.IEX, adjustment="all")).data
        today = datetime.now(ET).date()
        out = {}
        for sym in symbols:
            rows = [b for b in bars.get(sym, []) if b.timestamp.astimezone(ET).date() < today]
            out[sym] = ([float(b.close) for b in rows], [float(b.volume) for b in rows])
        return out

    # ----- options -----
    def option_candidates(self, underlying: str, strategy: str, price: float, today: date,
                          dte_min: int, dte_max: int) -> list[Candidate]:
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionSnapshotRequest
        from alpaca.trading.enums import ContractType
        from alpaca.trading.requests import GetOptionContractsRequest
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying], status="active",
            expiration_date_gte=today + timedelta(days=dte_min),
            expiration_date_lte=today + timedelta(days=dte_max),
            type=ContractType.CALL if strategy == "long_call" else ContractType.PUT,
            strike_price_gte=str(round(price * 0.85, 2)), strike_price_lte=str(round(price * 1.15, 2)),
            limit=1000)
        contracts = list(self.trading.get_option_contracts(req).option_contracts or [])
        if not contracts:
            return []
        snaps = {}
        syms = [c.symbol for c in contracts]
        for i in range(0, len(syms), 100):
            snaps.update(self.options.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=syms[i:i + 100], feed=OptionsFeed.INDICATIVE)))
        out = []
        for c in contracts:
            sn = snaps.get(c.symbol)
            q = getattr(sn, "latest_quote", None) if sn else None
            if q is None:
                continue
            g = getattr(sn, "greeks", None)
            exp = c.expiration_date if isinstance(c.expiration_date, date) else date.fromisoformat(str(c.expiration_date))
            cand = Candidate(c.symbol, underlying, strategy, float(c.strike_price), exp, (exp - today).days,
                             int(float(c.open_interest or 0)), float(q.bid_price or 0), float(q.ask_price or 0),
                             float(g.delta) if g is not None and g.delta is not None else None)
            cand.quote_time = q.timestamp  # type: ignore[attr-defined]
            out.append(cand)
        return out

    def option_quote(self, symbol: str) -> tuple[float, float, datetime] | None:
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionLatestQuoteRequest
        q = self.options.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbol, feed=OptionsFeed.INDICATIVE)).get(symbol)
        return (float(q.bid_price or 0), float(q.ask_price or 0), q.timestamp) if q else None

    # ----- orders -----
    def submit_limit(self, client_order_id: str, symbol: str, side: str, qty: int, limit_price: float) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest
        o = self.trading.submit_order(LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2), client_order_id=client_order_id))
        return self._o(o)

    def get_order(self, client_order_id: str) -> dict | None:
        try:
            return self._o(self.trading.get_order_by_client_id(client_order_id))
        except Exception:
            return None

    def cancel(self, client_order_id: str) -> None:
        o = self.trading.get_order_by_client_id(client_order_id)
        self.trading.cancel_order_by_id(o.id)

    def positions(self) -> dict[str, int]:
        return {p.symbol: int(float(p.qty)) for p in self.trading.get_all_positions()}

    @staticmethod
    def _o(o) -> dict:
        return {"status": str(getattr(o.status, "value", o.status)),
                "filled_qty": int(float(o.filled_qty or 0)),
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None}
