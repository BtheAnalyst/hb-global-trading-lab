"""Download regular-session 1-minute bars (and daily bars) for the intraday research pool. Read-only, no orders.

Output: research/intraday_data/<SYM>.csv.gz (UTC timestamps; columns t,o,h,l,c,v,n,vw), daily.csv.gz, meta.json.
The workflow commits these to the `research-data` branch (never to main).
Feed: SIP historical (allowed on the free plan when older than 15 minutes); falls back to IEX if refused.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hbgtl.intraday_universe import RESEARCH_POOL  # noqa: E402

OUT = ROOT / "research" / "intraday_data"
OUT.mkdir(parents=True, exist_ok=True)
DAYS = int(os.environ.get("DAYS", "190"))

k, s = os.environ["ALPACA_PAPER_KEY"].strip(), os.environ["ALPACA_PAPER_SECRET"].strip()
dc = StockHistoricalDataClient(k, s)
end = datetime.now(timezone.utc) - timedelta(minutes=20)
start = end - timedelta(days=DAYS)


def bars(syms, tf, st, feed):
    df = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=tf, start=st, end=end, feed=feed,
                                            adjustment=Adjustment.SPLIT)).df
    return df


feed = DataFeed.SIP
try:
    bars(["F"], TimeFrame.Minute, end - timedelta(days=3), feed)
except Exception as e:  # noqa: BLE001
    print(f"::warning title=SIP refused::{e}; using IEX")
    feed = DataFeed.IEX

daily = bars(RESEARCH_POOL, TimeFrame.Day, start - timedelta(days=60), feed).reset_index()
daily.to_csv(OUT / "daily.csv.gz", index=False)
got = sorted(daily["symbol"].unique())
print(f"daily bars for {len(got)} symbols")

written = []
for i in range(0, len(got), 10):
    chunk = got[i:i + 10]
    df = bars(chunk, TimeFrame.Minute, start, feed).reset_index()
    df["t"] = pd.to_datetime(df["timestamp"], utc=True)
    et = df["t"].dt.tz_convert("America/New_York")
    mins = et.dt.hour * 60 + et.dt.minute
    df = df[(mins >= 570) & (mins < 960)]                 # regular session 9:30-15:59 ET only
    for sym, g in df.groupby("symbol"):
        g = g.rename(columns={"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v",
                              "trade_count": "n", "vwap": "vw"})[["t", "o", "h", "l", "c", "v", "n", "vw"]]
        g.to_csv(OUT / f"{sym}.csv.gz", index=False)
        written.append(sym)
    print(f"{min(i + 10, len(got))}/{len(got)}")

(OUT / "meta.json").write_text(json.dumps({"feed": feed.value, "start": start.isoformat(), "end": end.isoformat(),
                                           "symbols": written, "adjustment": "split"}, indent=1))
print(f"::notice title=Intraday history::{len(written)} symbols, feed {feed.value}, {start.date()} to {end.date()}")
