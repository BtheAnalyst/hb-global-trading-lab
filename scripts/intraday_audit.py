"""Read-only broker audit for the intraday sleeve (Oct 28 directive). Places NO orders, changes nothing.

Checks: account type and cash-account behavior, PDT status and day-trade count, buying power vs cash,
settled vs unsettled cash (as Alpaca reports it), fractional and bracket support facts, the Founder's ETF
holdings vs the books, asset flags for those ETFs, market-data permissions (SIP vs IEX, real-time vs delayed),
and the screener endpoint. Prints GitHub ::notice:: lines and writes intraday_audit.json (uploaded as an
artifact by the workflow, also echoed to the log).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

out: dict = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def note(title, msg, level="notice"):
    print(f"::{level} title={title}::{' '.join(str(msg).split())[:3500]}")


def safe(name, fn):
    try:
        out[name] = fn()
    except Exception as e:  # noqa: BLE001 - report every check, never stop at the first failure
        out[name] = {"error": f"{type(e).__name__}: {' '.join(str(e).split())[:300]}"}


from alpaca.trading.client import TradingClient  # noqa: E402
from alpaca.data.historical.stock import StockHistoricalDataClient  # noqa: E402

k, s = os.environ["ALPACA_PAPER_KEY"].strip(), os.environ["ALPACA_PAPER_SECRET"].strip()
tc = TradingClient(k, s, paper=True)          # paper only
dc = StockHistoricalDataClient(k, s)


def account():
    a = tc.get_account()
    keys = ("status", "account_number", "currency", "cash", "equity", "last_equity", "buying_power",
            "regt_buying_power", "daytrading_buying_power", "non_marginable_buying_power", "multiplier",
            "pattern_day_trader", "daytrade_count", "trading_blocked", "account_blocked", "shorting_enabled",
            "options_trading_level", "crypto_status", "accrued_fees", "pending_transfer_in", "sma",
            "initial_margin", "maintenance_margin")
    d = {x: str(getattr(a, x, None)) for x in keys}
    d["account_number"] = "***" + d["account_number"][-3:] if d.get("account_number") else None
    return d


def configs():
    c = tc.get("/account/configurations")
    return {x: c.get(x) for x in ("dtbp_check", "pdt_check", "no_shorting", "fractional_trading",
                                  "max_margin_multiplier", "max_options_trading_level", "suspend_trade",
                                  "trade_confirm_email")}


def positions():
    return {p.symbol: {"qty": float(p.qty), "avg": float(p.avg_entry_price), "mv": float(p.market_value or 0),
                       "side": str(getattr(p.side, "value", p.side)),
                       "qty_available": float(getattr(p, "qty_available", 0) or 0)}
            for p in tc.get_all_positions()}


def open_orders():
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    return [{"symbol": o.symbol, "side": str(o.side.value), "type": str(o.order_type.value),
             "status": str(o.status.value), "cid": o.client_order_id}
            for o in tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))]


def activities():
    # Fills of the last 5 days (settlement / day-trade evidence)
    acts = tc.get("/account/activities/FILL", {"after": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()})
    return [{"symbol": x.get("symbol"), "side": x.get("side"), "qty": x.get("qty"), "price": x.get("price"),
             "time": x.get("transaction_time")} for x in (acts or [])][:40]


def assets():
    res = {}
    for sym in ("VTI", "SPY", "QQQ", "F", "SOFI", "AAPL", "TQQQ"):
        a = tc.get_asset(sym)
        res[sym] = {"tradable": a.tradable, "fractionable": a.fractionable, "exchange": str(a.exchange.value),
                    "class": str(a.asset_class.value), "status": str(a.status.value),
                    "shortable": a.shortable, "attributes": list(getattr(a, "attributes", None) or [])}
    return res


def clock():
    c = tc.get_clock()
    return {"is_open": c.is_open, "next_open": str(c.next_open), "next_close": str(c.next_close)}


def data_feeds():
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest, StockLatestQuoteRequest
    from alpaca.data.timeframe import TimeFrame
    res = {}
    for feed in (DataFeed.SIP, DataFeed.IEX):
        r = {}
        try:
            b = dc.get_stock_latest_bar(StockLatestBarRequest(symbol_or_symbols="F", feed=feed))["F"]
            r["latest_bar"] = {"t": str(b.timestamp), "c": b.close, "v": b.volume}
        except Exception as e:  # noqa: BLE001
            r["latest_bar"] = f"ERROR {type(e).__name__}: {' '.join(str(e).split())[:160]}"
        try:
            q = dc.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols="F", feed=feed))["F"]
            r["latest_quote"] = {"t": str(q.timestamp), "bid": q.bid_price, "ask": q.ask_price}
        except Exception as e:  # noqa: BLE001
            r["latest_quote"] = f"ERROR {type(e).__name__}: {' '.join(str(e).split())[:160]}"
        try:   # historical minute bars from yesterday (older than 15 min: allowed on the free plan for SIP)
            end = datetime.now(timezone.utc) - timedelta(minutes=20)
            bars = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols="F", timeframe=TimeFrame.Minute,
                                                      start=end - timedelta(days=4), end=end, feed=feed)).df
            r["hist_1min_rows"] = int(len(bars))
            r["hist_1min_volume_sum"] = float(bars["volume"].sum()) if len(bars) else 0
        except Exception as e:  # noqa: BLE001
            r["hist_1min"] = f"ERROR {type(e).__name__}: {' '.join(str(e).split())[:160]}"
        res[feed.value] = r
    return res


def screener():
    r = tc.get  # the screener lives on the data host; use the SDK's screener client
    from alpaca.data.historical.screener import ScreenerClient
    from alpaca.data.requests import MostActivesRequest
    sc = ScreenerClient(k, s)
    ma = sc.get_most_actives(MostActivesRequest(top=20))
    return [x.symbol for x in ma.most_actives][:20]


def books():
    st = json.loads((ROOT / "state" / "equity_state.json").read_text())
    f = st["portfolios"]["FOUNDER"]
    return {"positions": f["positions"], "settled": f["settled"], "unsettled": f["unsettled"],
            "contributed": f["contributed"]}


for name, fn in [("account", account), ("configurations", configs), ("positions", positions),
                 ("open_orders", open_orders), ("fills_5d", activities), ("assets", assets), ("clock", clock),
                 ("data_feeds", data_feeds), ("screener_most_actives", screener), ("founder_books", books)]:
    safe(name, fn)

# Reconciliation: Founder books vs broker positions (the paper account may hold other portfolios' shares too)
try:
    bp, fb = out["positions"], out["founder_books"]["positions"]
    out["recon"] = {sym: {"books": v["qty"], "broker": bp.get(sym, {}).get("qty"),
                          "match": abs(v["qty"] - (bp.get(sym, {}).get("qty") or 0)) < 1e-6}
                    for sym, v in fb.items()}
    out["recon"]["_extra_broker_symbols"] = sorted(set(bp) - set(fb))
except Exception as e:  # noqa: BLE001
    out["recon"] = {"error": str(e)}

Path("intraday_audit.json").write_text(json.dumps(out, indent=1, default=str))
print("AUDIT_JSON_BEGIN")
print(json.dumps(out, default=str))
print("AUDIT_JSON_END")
note("Intraday audit", "read-only; orders placed: 0")
