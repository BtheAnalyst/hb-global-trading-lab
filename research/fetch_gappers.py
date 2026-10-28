"""Research data for gap-and-go (Ross Cameron style) and "stocks in play" (SMB style) testing. Read-only.

1. All active, tradable U.S. common stocks on NYSE/Nasdaq/NYSE American/Arca (ETFs, warrants, units, rights
   and leveraged/inverse products removed by name/symbol rules).
2. Daily bars (SIP, split-adjusted) for all of them -> per-day gappers known AT THE OPEN only:
   open >= 10% above prior close, prior close $2-$20, prior 20-day average volume >= 300k.
   (Selection never uses the day's own volume or later prices: no look-ahead.)
3. Regular-session 1-minute bars for each gapper day.
Output: research/gapper_data/{gappers.csv, minute/<DATE>.csv.gz, meta.json} -> committed to research-data branch.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hbgtl.intraday_universe import asset_ok  # noqa: E402

OUT = ROOT / "research" / "gapper_data"
(OUT / "minute").mkdir(parents=True, exist_ok=True)
DAYS = int(os.environ.get("DAYS", "190"))
k, s = os.environ["ALPACA_PAPER_KEY"].strip(), os.environ["ALPACA_PAPER_SECRET"].strip()
tc, dc = TradingClient(k, s, paper=True), StockHistoricalDataClient(k, s)
end = datetime.now(timezone.utc) - timedelta(minutes=20)
start = end - timedelta(days=DAYS)

syms = []
for a in tc.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)):
    d = {"symbol": a.symbol, "tradable": a.tradable, "status": "active", "exchange": str(a.exchange.value),
         "class": "us_equity", "name": a.name or ""}
    if asset_ok(d) or not a.symbol.isalpha() or (len(a.symbol) == 5 and a.symbol[-1] in "WRUZ"):
        continue
    if any(w in (a.name or "").upper() for w in ("WARRANT", "RIGHT", " UNIT", "ACQUISITION CORP", "PREFERRED", "NOTES")):
        continue
    syms.append(a.symbol)
print(f"{len(syms)} candidate common stocks")

frames = []
for i in range(0, len(syms), 200):
    try:
        df = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 200], timeframe=TimeFrame.Day,
                                                start=start - timedelta(days=45), end=end, feed=DataFeed.SIP,
                                                adjustment=Adjustment.SPLIT)).df
        if df is not None and len(df):
            frames.append(df.reset_index())
    except Exception as e:  # noqa: BLE001
        print(f"daily chunk {i}: {e}")
daily = pd.concat(frames)
daily["date"] = pd.to_datetime(daily["timestamp"], utc=True).dt.tz_convert("America/New_York").dt.date.astype(str)
daily = daily.sort_values(["symbol", "date"])
g = daily.groupby("symbol")
daily["prev_close"] = g["close"].shift(1)
daily["adv20"] = g["volume"].transform(lambda v: v.shift(1).rolling(20, min_periods=15).mean())
daily["gap"] = daily["open"] / daily["prev_close"] - 1
gp = daily[(daily["gap"] >= 0.10) & daily["prev_close"].between(2, 20) & (daily["adv20"] >= 300_000)
           & (daily["date"] >= start.date().isoformat())]
gp = gp[["date", "symbol", "prev_close", "open", "gap", "adv20"]]
gp.to_csv(OUT / "gappers.csv", index=False)
print(f"{len(gp)} gapper symbol-days over {gp['date'].nunique()} sessions")

n = 0
for date, grp in gp.groupby("date"):
    st = pd.Timestamp(f"{date} 09:30", tz="America/New_York").tz_convert("UTC")
    try:
        df = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=list(grp["symbol"]), timeframe=TimeFrame.Minute,
                                                start=st, end=st + pd.Timedelta(minutes=390), feed=DataFeed.SIP,
                                                adjustment=Adjustment.SPLIT)).df
    except Exception as e:  # noqa: BLE001
        print(f"{date}: {e}"); continue
    if df is None or df.empty:
        continue
    df = df.reset_index().rename(columns={"timestamp": "t", "open": "o", "high": "h", "low": "l", "close": "c",
                                          "volume": "v"})[["symbol", "t", "o", "h", "l", "c", "v"]]
    df.to_csv(OUT / "minute" / f"{date}.csv.gz", index=False)
    n += 1
# generic intraday cumulative-volume curve (fraction of a day's volume done by each 5-minute mark), from pool data
(OUT / "meta.json").write_text(json.dumps({"start": start.isoformat(), "end": end.isoformat(), "universe": len(syms),
                                           "gapper_days": int(len(gp)), "sessions": n, "feed": "sip"}, indent=1))
print(f"::notice title=Gapper data::{len(gp)} gapper symbol-days, {n} sessions with minute bars")
