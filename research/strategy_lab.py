"""Strategy lab: many published / influencer day-trading strategies, one honest test (research only, no orders).

PREDECLARED (Nov 1, 2026, before any result was seen):
  * Long only, stocks/ETFs only, no leverage, no shorts, flat by 15:50 ET (the founder's mandate). Strategies that
    only work short, with options or with leverage are tested in their long-only form.
  * Decisions on COMPLETED 1-minute bars; entries at the NEXT bar (open, or a stop-entry trade-through); no look-ahead.
    Relative volume uses only PRIOR sessions. Daily inputs (ATR14, ADV20, prior close/high) use only prior days.
  * Every strategy has an initial protective stop (needed live); R = entry - stop. Stop fills at the stop or worse
    on a gap; if one bar touches stop and target, the stop is assumed. Targets need a 1-cent trade-through.
  * Costs per side on every fill: base 5 bps, stress 10 bps. $0 commission.
  * Split: in-sample = first 2/3 of sessions, out-of-sample = last 1/3. Rules were written from the sources, NOT
    tuned on this data.
  * MULTIPLE-TESTING BAR (stricter than the single-strategy bar, because many strategies are tried at once). At
    STRESS costs a strategy PASSES only if ALL hold: in-sample expectancy > 0; out-of-sample expectancy > 0;
    >= 30 out-of-sample trades; out-of-sample profit factor > 1.1; full-sample t-statistic of mean R >= 2.5;
    and the $100 cash-account simulation (fractional shares, settled cash T+1, 0.5% risk, 1 position, <= 3
    entries/day, -2R daily stop) ends above $100.
  * $100 simulation: notional = min(settled cash, 0.5% x equity / (risk/entry)); fractional shares allowed (min $1).

Usage: python research/strategy_lab.py <data_dir> [<data_dir> ...] -> research/strategy_lab_results.json
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SLIPS = {"gross": 0.0, "base": 0.0005, "stress": 0.0010}   # gross = no costs (diagnostic only, never used for the bar)
FLAT_M = 380                      # 15:50 ET
MAX_ENTRIES, DAY_STOP_R, RISK = 3, -2.0, 0.005
ETFS = {"SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLY", "XLI", "SMH", "GLD", "TLT", "ARKK"}

STRATEGIES = {
    # id: (source, universe)
    "ORB5_ZARATTINI": ("Zarattini, Barbon & Aziz 2024 'A Profitable Day Trading Strategy for the U.S. Equity Market': "
                       "5-min opening range, Stocks in Play (top-20 by opening relative volume), stop 10% of ATR, exit at close", "stocks"),
    "ORB30_ZARATTINI": ("Same paper, 30-minute opening range variant", "stocks"),
    "ORB5_ETF": ("Zarattini 5-min ORB rules applied to index/sector ETFs", "etfs"),
    "RED_TO_GREEN": ("Humbled Trader / Aziz 'How to Day Trade for a Living': gap-down stock reclaims the prior close", "stocks"),
    "VWAP_RECLAIM": ("Aziz / Humbled Trader VWAP trade: stock below VWAP reclaims it on volume", "stocks"),
    "BULL_FLAG": ("Ross Cameron / Aziz bull flag: strong 1-min pole, shallow 2-4 bar flag, break of flag high", "stocks"),
    "ABCD": ("Aziz ABCD pattern: morning push A-B, higher-low C above VWAP, break of B", "stocks"),
    "EMA9_VWAP_CROSS": ("SMB 'fashionably late': 9-EMA crosses above VWAP on 5-min bars, 10:00-13:00", "stocks"),
    "HOD_BREAK_PM": ("Momentum day-trader 'new high of day after 11:00' with relative volume >= 2", "stocks"),
    "OVERSOLD_BOUNCE": ("Aziz reversal trade: stretched >= 2.5% below VWAP + hammer/engulfing, target VWAP", "both"),
    "SECOND_DAY": ("SMB second-day play: yesterday's stock in play breaks yesterday's high", "stocks"),
    "GAP_FILL_LONG": ("Heather Love / classic gap fill: gap-down 0.25%-1.5% fills toward the prior close", "both"),
    "INTRADAY_MOM_LAST30": ("Gao, Han, Li & Zhou 2018 'Market Intraday Momentum': first half-hour return predicts the "
                            "last half-hour (adapted: hold 15:20-15:50)", "etfs"),
    "NOISE_BREAKOUT": ("Zarattini, Aziz & Barbon 2024 'Beat the Market' SPY noise-area breakout, long side, "
                       "VWAP/boundary trailing exit at half-hour checks", "etfs"),
    "VWAP_TREND_5M": ("Zarattini & Aziz 2023 'VWAP: the Holy Grail for Day Trading Systems': long above VWAP, "
                      "out below it (5-min bars, long only)", "etfs"),
}


# ---------------------------------------------------------------------------------------------------- data
def load_daily(dirs):
    frames = []
    for d in dirs:
        f = Path(d) / "daily.csv.gz"
        if f.exists():
            frames.append(pd.read_csv(f))
    d = pd.concat(frames).drop_duplicates(["symbol", "timestamp"])
    d["date"] = pd.to_datetime(d["timestamp"], utc=True).dt.tz_convert("America/New_York").dt.date.astype(str)
    out = {}
    for sym, g in d.sort_values("date").groupby("symbol"):
        g = g.set_index("date")
        tr = pd.concat([g["high"] - g["low"], (g["high"] - g["close"].shift()).abs(),
                        (g["low"] - g["close"].shift()).abs()], axis=1).max(axis=1)
        out[sym] = pd.DataFrame({"prev_close": g["close"].shift(1), "prev_high": g["high"].shift(1),
                                 "prev_open": g["open"].shift(1), "prev_vol": g["volume"].shift(1),
                                 "adv20": g["volume"].shift(1).rolling(20).mean(),
                                 "atr14": tr.shift(1).rolling(14).mean()})
    return out


def sessions_for(path):
    df = pd.read_csv(path, parse_dates=["t"])
    et = df["t"].dt.tz_convert("America/New_York")
    df["date"] = et.dt.date.astype(str)
    df["m"] = (et.dt.hour * 60 + et.dt.minute - 570).astype(int)
    out = []
    for date, g in df.groupby("date", sort=True):
        g = g[(g["m"] >= 0) & (g["m"] < 390)].sort_values("m")
        if len(g) < 300:                                   # half days / thin data: skip
            continue
        o, h, l, c, v = (g[k].to_numpy(float) for k in ("o", "h", "l", "c", "v"))
        pv = (g["vw"].to_numpy(float) if "vw" in g else (h + l + c) / 3) * v
        cv = np.cumsum(v)
        vwap = np.cumsum(pv) / np.maximum(cv, 1)
        out.append({"date": date, "m": g["m"].to_numpy(int), "o": o, "h": h, "l": l, "c": c, "v": v, "cv": cv,
                    "vwap": vwap})
    return out


def cum_at(s, minute):
    """Cumulative volume through bars with m < minute."""
    i = np.searchsorted(s["m"], minute)
    return float(s["cv"][i - 1]) if i > 0 else 0.0


def five_min(s):
    """Completed 5-minute bars from the 1-minute session (bucket k covers minutes 5k..5k+4)."""
    b = s["m"] // 5
    keys, idx = np.unique(b, return_index=True)
    ends = np.r_[idx[1:], len(b)] - 1
    o = s["o"][idx]; c = s["c"][ends]
    h = np.maximum.reduceat(s["h"], idx); l = np.minimum.reduceat(s["l"], idx)
    v = np.add.reduceat(s["v"], idx)
    return {"k": keys, "o": o, "h": h, "l": l, "c": c, "v": v, "vwap": s["vwap"][ends], "end_i": ends}


def ema(x, n):
    a = 2 / (n + 1)
    out = np.empty_like(x)
    acc = x[0]
    for i, xi in enumerate(x):
        acc = a * xi + (1 - a) * acc if i else xi
        out[i] = acc
    return out


# ---------------------------------------------------------------------------------------------------- simulator
def simulate(s, sig, slip):
    """sig: {"i": index of the completed signal bar, "entry": "open" | ("stop", price, until_m), "stop": float,
    "target": price or None, "r_target": multiple or None, "exit_mask": bool array (exit at next open when True
    on a completed bar) or None, "time_exit_m": minute or None}. Returns a trade dict or None."""
    m, o, h, l, c = s["m"], s["o"], s["h"], s["l"], s["c"]
    i = sig["i"]
    n = len(m)
    if i + 1 >= n:
        return None
    j = i + 1
    if sig["entry"] == "open":
        entry = o[j] * (1 + slip)
    else:
        _, trig, until = sig["entry"]
        while j < n and m[j] <= until and h[j] < trig + 0.01:
            j += 1
        if j >= n or m[j] > until:
            return None
        entry = max(o[j], trig + 0.01) * (1 + slip)
    stop = sig["stop"]
    if entry <= stop * 1.0005:
        return None
    risk = entry - stop
    target = sig.get("target")
    if sig.get("r_target"):
        target = entry + sig["r_target"] * risk
    if target is not None and target <= entry:
        return None
    t_exit = min(sig.get("time_exit_m") or FLAT_M, FLAT_M)
    mask = sig.get("exit_mask")
    for k in range(j, n):
        if m[k] >= t_exit:
            return _trade(sig, s, entry, o[k] * (1 - slip), risk, "time", m[j], m[k])
        if l[k] <= stop:
            px = (min(o[k], stop) if k > j else stop) * (1 - slip)
            return _trade(sig, s, entry, px, risk, "stop", m[j], m[k])
        if target is not None and h[k] >= target + 0.01:
            return _trade(sig, s, entry, target * (1 - slip), risk, "target", m[j], m[k])
        if mask is not None and k > j and mask[k] and k + 1 < n:
            return _trade(sig, s, entry, o[k + 1] * (1 - slip), risk, "rule exit", m[j], m[k + 1])
    return _trade(sig, s, entry, c[n - 1] * (1 - slip), risk, "close", m[j], m[n - 1])


def _trade(sig, s, entry, px, risk, how, m_in, m_out):
    return {"date": s["date"], "symbol": sig["sym"], "strategy": sig["strategy"], "m": int(m_in), "exit_m": int(m_out),
            "entry": round(entry, 4), "exit": round(px, 4), "risk": round(risk, 4), "R": round((px - entry) / risk, 4),
            "ret": (px - entry) / entry, "how": how, "rank": sig.get("rank", 0.0)}


# ---------------------------------------------------------------------------------------------------- strategies
def rvol_profile(hist, minute):
    vals = [cum_at(h, minute) for h in hist[-14:]]
    vals = [x for x in vals if x > 0]
    return float(np.mean(vals)) if vals else None


def signals_for_day(sym, s, hist, dd, is_etf):
    """All candidate signals (one per strategy per symbol per day) for one session."""
    m, o, h, l, c, v, vwap = s["m"], s["o"], s["h"], s["l"], s["c"], s["v"], s["vwap"]
    pc, atr, adv, ph = dd["prev_close"], dd["atr14"], dd["adv20"], dd["prev_high"]
    if not (pc > 0 and atr > 0 and adv > 0):
        return []
    eligible_stock = (not is_etf) and 5 <= pc <= 100 and adv >= 1_000_000
    out = []

    def add(strategy, i, **kw):
        out.append({"sym": sym, "strategy": strategy, "i": int(i), **kw})

    def first(cond_idx):
        return int(cond_idx[0]) if len(cond_idx) else None

    day_open = o[0]
    # opening-range breakouts (Zarattini): first candle direction, stop 10% ATR, exit at close
    for strat, mins, uni in (("ORB5_ZARATTINI", 5, "stocks"), ("ORB30_ZARATTINI", 30, "stocks"), ("ORB5_ETF", 5, "etfs")):
        if (uni == "stocks") != (not is_etf) or (uni == "stocks" and not eligible_stock):
            continue
        k = np.searchsorted(m, mins)
        if k < 2 or k >= len(m):
            continue
        or_h, or_c = h[:k].max(), c[k - 1]
        if not or_c > day_open:                            # bullish opening candle only (long side)
            continue
        base = rvol_profile(hist, mins)
        rv = cum_at(s, mins) / base if base else None
        if rv is None or (uni == "stocks" and rv < 1.0):
            continue
        add(strat, k - 1, entry=("stop", or_h, 330), stop=or_h - 0.10 * atr, target=None, rank=rv)

    if eligible_stock:
        base30 = rvol_profile(hist, 30)
        # RED TO GREEN
        if day_open < pc * 0.995:
            idx = np.where((m >= 5) & (m <= 90) & (c > pc) & (np.r_[c[0], c[:-1]] <= pc))[0]
            i = first(idx)
            if i is not None:
                bp = rvol_profile(hist, int(m[i]) + 1)
                rv = cum_at(s, int(m[i]) + 1) / bp if bp else 0
                lod = l[:i + 1].min()
                if rv >= 1.5 and (c[i] - lod) / c[i] <= 0.02:
                    add("RED_TO_GREEN", i, entry="open", stop=lod - 0.01, r_target=2.0, rank=rv)
        # VWAP RECLAIM: >= 10 bars below VWAP, then a close back above, 10:00-13:30
        below = c < vwap
        run = np.zeros(len(c), int)
        for t in range(1, len(c)):
            run[t] = run[t - 1] + 1 if below[t - 1] else 0
        idx = np.where((m >= 30) & (m <= 240) & ~below & (run >= 10))[0]
        i = first(idx)
        if i is not None:
            bp = rvol_profile(hist, int(m[i]) + 1)
            rv = cum_at(s, int(m[i]) + 1) / bp if bp else 0
            stp = l[max(0, i - 10):i + 1].min() - 0.01
            if rv >= 1.5 and (c[i] - stp) / c[i] <= 0.02:
                add("VWAP_RECLAIM", i, entry="open", stop=stp, r_target=2.0, rank=rv)
        # BULL FLAG on 1-min: pole >= 1% in <= 5 bars with >= 3 green bars, flag 2-4 bars retracing < 50%,
        # entry on a close above the flag high, 9:35-13:30, price above VWAP
        for i in range(10, len(c)):
            if not (5 <= m[i] <= 240) or c[i] <= vwap[i]:
                continue
            found = False
            for fl in (2, 3, 4):
                p_end = i - fl - 1
                p_start = p_end - 4
                if p_start < 0:
                    continue
                pole_lo, pole_hi = l[p_start:p_end + 1].min(), h[p_start:p_end + 1].max()
                if pole_hi / pole_lo - 1 < 0.01 or (c[p_start:p_end + 1] > o[p_start:p_end + 1]).sum() < 3:
                    continue
                flag_lo, flag_hi = l[p_end + 1:i].min(), h[p_end + 1:i].max()
                if flag_hi > pole_hi or flag_lo < pole_hi - 0.5 * (pole_hi - pole_lo):
                    continue
                if v[p_end + 1:i].mean() >= v[p_start:p_end + 1].mean():
                    continue
                if c[i] > flag_hi and (c[i] - flag_lo) / c[i] <= 0.02:
                    add("BULL_FLAG", i, entry="open", stop=flag_lo - 0.01, r_target=2.0,
                        rank=(pole_hi / pole_lo - 1) * 100)
                    found = True
                    break
            if found:
                break
        # ABCD: A = open area, B = morning high >= +2% from open by 10:30, C = higher low above VWAP
        # retracing 38-62% of A-B, then a close above B (by 13:00)
        kb = np.searchsorted(m, 60)
        if kb > 5:
            ib = int(np.argmax(h[:kb])); B = h[ib]; A = l[:ib + 1].min() if ib > 0 else day_open
            if B / A - 1 >= 0.02:
                after = np.arange(ib + 1, len(c))
                after = after[m[after] <= 210]
                if len(after):
                    run_lo = np.minimum.accumulate(l[after])
                    brk = np.where(c[after] > B)[0]
                    if len(brk):
                        k = after[brk[0]]
                        C = run_lo[brk[0]]
                        retr = (B - C) / (B - A)
                        if 0.38 <= retr <= 0.62 and C > vwap[k] * 0.995 and (c[k] - C) / c[k] <= 0.025:
                            add("ABCD", k, entry="open", stop=C - 0.01, r_target=2.0, rank=(B / A - 1) * 100)
        # 9-EMA crosses above VWAP on 5-min bars, 10:00-13:00
        f5 = five_min(s)
        e9 = ema(f5["c"], 9)
        cross = np.where((f5["k"] >= 6) & (f5["k"] <= 41) & (e9 > f5["vwap"]) &
                         (np.r_[True, e9[:-1] <= f5["vwap"][:-1]]))[0]
        if len(cross):
            q = cross[0]
            i = int(f5["end_i"][q])
            stp = min(f5["l"][q], f5["vwap"][q]) - 0.01
            if (c[i] - stp) / c[i] <= 0.02 and c[i] > stp:
                add("EMA9_VWAP_CROSS", i, entry="open", stop=stp, r_target=2.0, rank=1.0)
        # NEW HIGH OF DAY after 11:00, day change >= +2%, RVOL >= 2, stop at VWAP (risk <= 2.5%)
        hod = np.maximum.accumulate(h)
        idx = np.where((m >= 90) & (m <= 300) & (c >= np.r_[0, hod[:-1]]) & (c >= pc * 1.02))[0]
        for i in idx[:5]:
            bp = rvol_profile(hist, int(m[i]) + 1)
            rv = cum_at(s, int(m[i]) + 1) / bp if bp else 0
            if rv >= 2 and 0 < (c[i] - vwap[i]) / c[i] <= 0.025:
                add("HOD_BREAK_PM", i, entry="open", stop=vwap[i] - 0.01, r_target=2.0, rank=rv)
                break
        # SECOND DAY: yesterday up >= 5% on >= 2x volume; today break of yesterday's high (9:35-11:30)
        if dd["prev_close"] / max(dd["prev_open"], 1e-9) - 1 >= 0.05 and dd["prev_vol"] >= 2 * adv:
            idx = np.where((m >= 5) & (m <= 120) & (c > ph))[0]
            i = first(idx)
            if i is not None:
                stp = l[:i + 1].min() - 0.01
                if (c[i] - stp) / c[i] <= 0.03:
                    add("SECOND_DAY", i, entry="open", stop=stp, r_target=2.0, rank=2.0)

    # OVERSOLD BOUNCE (stocks and ETFs): >= 2.5% below VWAP (1.0% for ETFs), hammer or bullish engulfing
    if eligible_stock or is_etf:
        stretch = 0.010 if is_etf else 0.025
        rng = np.maximum(h - l, 1e-9)
        hammer = ((np.minimum(o, c) - l) / rng >= 0.6) & (c >= o)
        engulf = (c > o) & (np.r_[False, c[:-1] < o[:-1]]) & (c >= np.r_[0, o[:-1]]) & (o <= np.r_[0, c[:-1]])
        idx = np.where((m >= 15) & (m <= 300) & (c < vwap * (1 - stretch)) & (hammer | engulf))[0]
        i = first(idx)
        if i is not None and vwap[i] > c[i]:
            add("OVERSOLD_BOUNCE", i, entry="open", stop=l[i] - 0.01, target=vwap[i], rank=1.0)
    # GAP FILL (long side): gap-down 0.25%-1.5%, first 5-min bar closes green, target prior close, stop = day low,
    # out by 12:00
    if (eligible_stock or is_etf) and pc * (1 - 0.015) <= day_open <= pc * (1 - 0.0025):
        k = np.searchsorted(m, 5)
        if 0 < k < len(m) and c[k - 1] > day_open:
            stp = l[:k].min() - 0.01
            add("GAP_FILL_LONG", k - 1, entry="open", stop=stp, target=pc, time_exit_m=150, rank=1.0)
    if is_etf:
        # INTRADAY MOMENTUM: prior close -> 10:00 return > 0 => long 15:20-15:50
        k30 = np.searchsorted(m, 30)
        k350 = np.searchsorted(m, 350)
        if 0 < k30 < len(m) and 0 < k350 < len(m) and c[k30 - 1] > pc:
            add("INTRADAY_MOM_LAST30", k350 - 1, entry="open", stop=c[k350 - 1] * 0.99, target=None, rank=1.0)
        # NOISE AREA BREAKOUT (long): sigma_t = mean over 14 prior days of |close_t / open - 1|
        if len(hist) >= 14:
            checks = [mm for mm in range(30, 360, 30)]
            sig_t = {}
            for mm in checks:
                xs = []
                for hs in hist[-14:]:
                    q = np.searchsorted(hs["m"], mm) - 1
                    if q >= 0:
                        xs.append(abs(hs["c"][q] / hs["o"][0] - 1))
                sig_t[mm] = np.mean(xs) if xs else None
            upper = {mm: max(day_open, pc) * (1 + sig_t[mm]) for mm in checks if sig_t[mm]}
            mask = np.zeros(len(m), bool)
            entry_i = None
            for mm in checks:
                q = np.searchsorted(m, mm) - 1
                if q < 0 or mm not in upper:
                    continue
                if entry_i is None and c[q] > upper[mm]:
                    entry_i = q
                elif entry_i is not None and c[q] < max(upper[mm], vwap[q]):
                    mask[q] = True
            if entry_i is not None:
                stp = min(max(upper[min(upper)], vwap[entry_i]), c[entry_i] * 0.998)
                add("NOISE_BREAKOUT", entry_i, entry="open", stop=stp, target=None, exit_mask=mask, rank=1.0)
        # VWAP TREND on 5-min bars: first completed 5-min close above VWAP after 9:35 -> long; out on a close below
        f5 = five_min(s)
        above = f5["c"] > f5["vwap"]
        mask = np.zeros(len(m), bool)
        mask[f5["end_i"][~above]] = True
        q = np.where(above & (f5["k"] >= 1))[0]
        if len(q):
            i = int(f5["end_i"][q[0]])
            stp = min(f5["vwap"][q[0]], c[i] * 0.998) - 0.01
            add("VWAP_TREND_5M", i, entry="open", stop=stp, target=None, exit_mask=mask, rank=1.0)
    return out


def run_symbol(args):
    path, daily = args
    sym = Path(path).name.split(".")[0]
    is_etf = sym in ETFS
    sess = sessions_for(path)
    res = {name: [] for name in SLIPS}
    for t, s in enumerate(sess):
        if t < 14 or s["date"] not in daily.index:
            continue
        dd = daily.loc[s["date"]]
        for sig in signals_for_day(sym, s, sess[max(0, t - 20):t], dd, is_etf):
            for name, slip in SLIPS.items():
                tr = simulate(s, sig, slip)
                if tr:
                    res[name].append(tr)
    return sym, res, [s["date"] for s in sess]


# ---------------------------------------------------------------------------------------------------- scoring
def stats(trs):
    if not trs:
        return {"n": 0}
    r = np.array([t["R"] for t in trs])
    w, ls = r[r > 0], r[r <= 0]
    se = r.std(ddof=1) / math.sqrt(len(r)) if len(r) > 1 else float("nan")
    cum = np.cumsum(r)
    dd = float((cum - np.maximum.accumulate(np.r_[0, cum])[1:]).min())
    return {"n": int(len(r)), "win_rate": round(float((r > 0).mean()), 3),
            "avg_win_R": round(float(w.mean()), 3) if len(w) else 0, "avg_loss_R": round(float(ls.mean()), 3) if len(ls) else 0,
            "expectancy_R": round(float(r.mean()), 4), "t_stat": round(float(r.mean() / se), 2) if se == se and se > 0 else None,
            "profit_factor": round(float(w.sum() / -ls.sum()), 3) if ls.sum() < 0 else None,
            "total_R": round(float(r.sum()), 2), "max_drawdown_R": round(dd, 2),
            "avg_trade_return_pct": round(float(np.mean([t["ret"] for t in trs]) * 100), 4)}


def portfolio(trades, top_n=None):
    """Live portfolio rules on one strategy's trades: one at a time, <= 3/day, -2R daily stop, rank order on ties."""
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["date"]].append(t)
    taken = []
    for d in sorted(by_day):
        day = sorted(by_day[d], key=lambda t: (t["m"], -t["rank"]))
        if top_n:
            keep = {id(t) for t in sorted(by_day[d], key=lambda t: -t["rank"])[:top_n]}
            day = [t for t in day if id(t) in keep]
        free, n, dr = -1, 0, 0.0
        for t in day:
            if t["m"] <= free or n >= MAX_ENTRIES or dr <= DAY_STOP_R:
                continue
            taken.append(t); free = t["exit_m"]; n += 1; dr += t["R"]
    return taken


def account_100(taken):
    eq, low = 100.0, 100.0
    by_day = defaultdict(list)
    for t in taken:
        by_day[t["date"]].append(t)
    for d in sorted(by_day):
        settled = eq                                        # yesterday's proceeds have settled (T+1)
        for t in by_day[d]:
            notional = min(settled, RISK * eq / max(t["risk"] / t["entry"], 1e-9))
            if notional < 1.0:
                continue
            pnl = notional * (t["exit"] / t["entry"] - 1)
            settled -= notional                             # sale proceeds are unsettled until tomorrow
            eq += pnl
            low = min(low, eq)
    return {"end_value": round(eq, 2), "min_value": round(low, 2)}


def main():
    dirs = [Path(x) for x in sys.argv[1:]] or [ROOT / "research" / "intraday_data"]
    daily = load_daily(dirs)
    jobs = []
    for d in dirs:
        for f in sorted(d.glob("*.csv.gz")):
            sym = f.name.split(".")[0]
            if sym != "daily" and sym in daily:
                jobs.append((str(f), daily[sym]))
    with Pool() as p:
        results = p.map(run_symbol, jobs)
    all_dates = sorted({d for _, _, ds in results for d in ds})
    out = {"method": __doc__.split("Usage")[0].strip(), "sources": {k: v[0] for k, v in STRATEGIES.items()},
           "data": {"dirs": [str(d) for d in dirs], "symbols": len(jobs), "first": all_dates[0], "last": all_dates[-1],
                    "sessions": len(all_dates)}, "strategies": {}}
    for strat in STRATEGIES:
        blk = {}
        for name in SLIPS:
            trs = [t for _, r, _ in results for t in r[name] if t["strategy"] == strat]
            dates = sorted({t["date"] for t in trs}) or all_dates
            uni_dates = sorted({d for _, r, ds in results for d in ds})
            cut = uni_dates[int(len(uni_dates) * 2 / 3)]
            top = 20 if strat.startswith("ORB") and "ETF" not in strat else None
            taken = portfolio(trs, top_n=top)
            ins = [t for t in taken if t["date"] < cut]
            oos = [t for t in taken if t["date"] >= cut]
            blk[name] = {"raw_signals": stats(trs), "portfolio": stats(taken), "in_sample": stats(ins),
                         "out_of_sample": stats(oos), "oos_from": cut, "account_100_fractional": account_100(taken),
                         "trades_per_week": round(len(taken) / max(1, len(uni_dates) / 5), 2)}
        st = blk["stress"]
        crit = {"in_sample_expectancy_gt_0": (st["in_sample"].get("expectancy_R") or -1) > 0,
                "oos_expectancy_gt_0": (st["out_of_sample"].get("expectancy_R") or -1) > 0,
                "oos_trades_ge_30": st["out_of_sample"].get("n", 0) >= 30,
                "oos_profit_factor_gt_1_1": (st["out_of_sample"].get("profit_factor") or 0) > 1.1,
                "full_t_stat_ge_2_5": (st["portfolio"].get("t_stat") or 0) >= 2.5,
                "account_100_ends_above_100": st["account_100_fractional"]["end_value"] > 100}
        blk["criteria_stress"] = crit
        blk["passes"] = all(crit.values())
        out["strategies"][strat] = blk
        p = st["portfolio"]
        print(f"{strat:22s} {'PASS' if blk['passes'] else 'fail'}  n={p.get('n', 0):4d}  E={p.get('expectancy_R')}R "
              f"t={p.get('t_stat')} PF={p.get('profit_factor')}  IS={st['in_sample'].get('expectancy_R')} "
              f"OOS={st['out_of_sample'].get('expectancy_R')}({st['out_of_sample'].get('n', 0)})  "
              f"$100->{st['account_100_fractional']['end_value']}  /wk={st['trades_per_week']}", flush=True)
    name = "strategy_lab_results.json" if len(dirs) == 1 else "strategy_lab_results_combined.json"
    tag = "_".join(sorted({d.name for d in dirs}))
    (ROOT / "research" / f"strategy_lab_results_{tag}.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
