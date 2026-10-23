"""One-time research download: adjusted daily bars for the ETFs being studied. Read-only on the account."""
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

SYMS = ["SPY", "VTI", "QQQ", "IWM", "EFA", "EEM", "VEA", "TLT", "IEF", "SHY", "BIL", "GLD", "VNQ", "SCHD", "VYM", "VXUS"]
c = StockHistoricalDataClient(os.environ["ALPACA_PAPER_KEY"].strip(), os.environ["ALPACA_PAPER_SECRET"].strip())
out = Path("research/data"); out.mkdir(parents=True, exist_ok=True)
for feed in (DataFeed.SIP, DataFeed.IEX):
    try:
        bars = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMS, timeframe=TimeFrame.Day,
                                                 start=datetime(2015, 1, 1, tzinfo=timezone.utc),
                                                 end=datetime(2026, 10, 22, tzinfo=timezone.utc),
                                                 adjustment=Adjustment.ALL, feed=feed)).data
        break
    except Exception as e:
        print(f"::warning::feed {feed} failed: {type(e).__name__}")
for s in SYMS:
    rows = bars.get(s, [])
    with open(out / f"{s}.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "open", "high", "low", "close", "volume"])
        for b in rows:
            w.writerow([b.timestamp.date().isoformat(), b.open, b.high, b.low, b.close, int(b.volume)])
    first = rows[0].timestamp.date() if rows else None
    print(f"{s}: {len(rows)} bars from {first}")
print(f"::notice title=History downloaded::feed {feed.value}; " + ", ".join(f"{s}:{len(bars.get(s, []))}" for s in SYMS))
