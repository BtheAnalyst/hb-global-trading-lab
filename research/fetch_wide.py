"""Research data for Setup A on a WIDE universe (all liquid U.S. common stocks), read-only, no orders.

1. Universe: active U.S. common stocks (ETFs, warrants, units, rights, preferreds, leveraged/inverse removed)
   whose prior close was $5-$100 with 20-day average volume >= 1M on at least one day in the window.
2. Daily bars (SIP) and 5-minute bars (SIP) for all of them. 5-minute bars give each symbol's prior-20-session
   cumulative-volume profile, exactly as the live engine builds it.
3. A data-fetch prefilter (NOT a trading signal): a symbol-day gets 1-minute bars only if, at some 5-minute
   mark up to 11:30 ET, its cumulative volume was >= 1.8x its own prior-20-session profile. Setup A needs
   relative volume >= 2.0 at a breakout between 9:45 and 11:30, so every day that could signal is kept;
   the prefilter uses nothing after 11:30 and no prices, so it cannot select on the outcome.
Output: research/wide_data/{daily.csv.gz, profiles.json.gz, minute/<DATE>.csv.gz, meta.json}
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hbgtl.intraday_universe import asset_ok  # noqa: E402

OUT = ROOT / "research" / "wide_data"
(OUT / "minute").mkdir(parents=True, exist_ok=True)
DAYS = int(os.environ.get("DAYS", "190"))
k, s = os.environ["ALPACA_PAPER_KEY"].strip(), os.environ["ALPACA_PAPER_SECRET"].strip()
tc, dc = TradingClient(k, s, paper=True), StockHistoricalDataClient(k, s)
end = datetime.now(timezone.utc) - timedelta(minutes=20)
start = end - timedelta(days=DAYS)
ET = "America/New_York"


def common_stocks() -> list[str]:
    out = []
    for a in tc.get("/assets", {"status": "active", "asset_class": "us_equity"}):
        sym, name = a.get("symbol", ""), a.get("name") or ""
        d = {"symbol": sym, "tradable": a.get("tradable"), "status": "active", "exchange": a.get("exchange"),
             "class": "us_equity", "name": name}
        if asset_ok(d) or not sym.isalpha() or (len(sym) == 5 and sym[-1] in "WRUZ"):
            continue
        if any(w in name.upper() for w in ("WARRANT", "RIGHT", " UNIT", "ACQUISITION CORP", "PREFERRED", "NOTES")):
            continue
        out.append(sym)
    return out


def bars(syms, tf, st, en):
    df = dc.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=tf, start=st, end=en,
                                            feed=DataFeed.SIP, adjustment=Adjustment.SPLIT)).df
    return None if df is None or df.empty else df.reset_index()


syms = common_stocks()
print(f"{len(syms)} common stocks")
frames = []
for i in range(0, len(syms), 200):
    try:
        d = bars(syms[i:i + 200], TimeFrame.Day, start - timedelta(days=45), end)
        if d is not None:
            frames.append(d)
    except Exception as e:  # noqa: BLE001
        print(f"daily {i}: {e}")
daily = pd.concat(frames)
daily["date"] = pd.to_datetime(daily["timestamp"], utc=True).dt.tz_convert(ET).dt.date.astype(str)
daily = daily.sort_values(["symbol", "date"])
g = daily.groupby("symbol")
daily["prev_close"] = g["close"].shift(1)
daily["adv20"] = g["volume"].transform(lambda v: v.shift(1).rolling(20, min_periods=15).mean())
ok = daily[daily["prev_close"].between(5, 100) & (daily["adv20"] >= 1_000_000) & (daily["date"] >= start.date().isoformat())]
universe = sorted(ok["symbol"].unique())
daily[daily["symbol"].isin(universe)][["symbol", "date", "open", "high", "low", "close", "volume", "prev_close", "adv20"]] \
    .to_csv(OUT / "daily.csv.gz", index=False)
elig = set(zip(ok["symbol"], ok["date"]))
print(f"{len(universe)} symbols eligible on at least one day; {len(elig)} eligible symbol-days")

# 5-minute bars -> per-day cumulative profile and the fetch prefilter
five_tf = TimeFrame(5, TimeFrameUnit.Minute)
profiles: dict[str, dict] = {}          # sym -> {date: {bucket: cumv}} (regular session, prior days used later)
keep: dict[str, list[str]] = {}         # date -> symbols needing 1-minute bars
for i in range(0, len(universe), 50):
    chunk = universe[i:i + 50]
    try:
        f = bars(chunk, five_tf, start - timedelta(days=40), end)
    except Exception as e:  # noqa: BLE001
        print(f"5min {i}: {e}"); continue
    if f is None:
        continue
    t = pd.to_datetime(f["timestamp"], utc=True).dt.tz_convert(ET)
    f["date"] = t.dt.date.astype(str)
    f["m"] = t.dt.hour * 60 + t.dt.minute - 570
    f = f[(f["m"] >= 0) & (f["m"] < 390)]
    for sym, gs in f.groupby("symbol"):
        days = {}
        for date, gd in gs.groupby("date"):
            days[date] = gd.groupby("m")["volume"].sum().cumsum().to_dict()
        dates = sorted(days)
        for j, date in enumerate(dates):
            if (sym, date) not in elig or j < 10:
                continue
            prior = [days[x] for x in dates[max(0, j - 20):j]]
            prof = pd.DataFrame(prior).mean().to_dict()
            cur = days[date]
            hit = any(prof.get(m) and cur.get(m, 0) >= 1.8 * prof[m] for m in range(10, 125, 5))
            if hit:
                keep.setdefault(date, []).append(sym)
        profiles[sym] = {d: {int(m): float(v) for m, v in days[d].items()} for d in dates}
    print(f"5min {min(i + 50, len(universe))}/{len(universe)}", flush=True)
with gzip.open(OUT / "profiles.json.gz", "wt") as fh:
    json.dump(profiles, fh)
n_keep = sum(len(v) for v in keep.values())
print(f"prefilter kept {n_keep} symbol-days for 1-minute data")

for date in sorted(keep):
    st = pd.Timestamp(f"{date} 09:30", tz=ET).tz_convert("UTC")
    parts = []
    for i in range(0, len(keep[date]), 100):
        try:
            m = bars(keep[date][i:i + 100], TimeFrame.Minute, st, st + pd.Timedelta(minutes=390))
            if m is not None:
                parts.append(m)
        except Exception as e:  # noqa: BLE001
            print(f"{date}: {e}")
    if parts:
        m = pd.concat(parts).rename(columns={"timestamp": "t", "open": "o", "high": "h", "low": "l", "close": "c",
                                             "volume": "v"})[["symbol", "t", "o", "h", "l", "c", "v"]]
        m.to_csv(OUT / "minute" / f"{date}.csv.gz", index=False)
(OUT / "meta.json").write_text(json.dumps({"start": start.isoformat(), "end": end.isoformat(), "stocks": len(syms),
                                           "universe": len(universe), "eligible_symbol_days": len(elig),
                                           "prefilter_symbol_days": n_keep, "feed": "sip"}, indent=1))
print(f"::notice title=Wide data::{len(universe)} symbols, {len(elig)} eligible symbol-days, {n_keep} kept")
