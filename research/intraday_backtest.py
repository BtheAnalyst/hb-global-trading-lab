"""Backtest of the three intraday setups on historical 1-minute bars (research only, no orders).

Method (predeclared, Oct 28 2026):
  * Same signal code as the live engine (hbgtl.setups); decisions at the CLOSE of a completed bar, fills at
    the NEXT bar (no look-ahead). Relative volume uses only PRIOR sessions' volume profile.
  * Eligibility per day from data known before the open: prior close $5-$100, 20-day average volume >= 1M.
  * Entry: marketable limit capped at signal close + 0.25R; no fill if the next bar never trades at/below it.
  * Exits: stop (fills at the stop or worse on a gap), 2R target (needs a trade-through), setup invalidation
    on a completed bar (next-bar open), flatten 15:50 ET. If one bar touches both stop and target: stop.
  * Costs: slippage per side on every market-style fill, 5 bps base and 10 bps stress; $0 commission.
  * Portfolio rules as live: one position at a time, max 3 entries/day, stop for the day at -2R
    (0.5% risk/trade, 1% daily loss), signals ranked by relative volume.
  * In-sample = first 2/3 of sessions, out-of-sample = last 1/3. Parameters were NOT tuned on either.
  * $100 cash-account simulation: whole shares, 0.5% risk ($0.50), settled cash only (T+1), skips logged.

Usage: python research/intraday_backtest.py <data_dir>   -> research/intraday_backtest_results.json
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
sys.path.insert(0, str(ROOT))
from hbgtl.candles import session_frame, volume_profile, rvol  # noqa: E402
from hbgtl.setups import SETUP_IDS, Signal, evaluate, reason_key  # noqa: E402
from hbgtl.intraday_sim import simulate_trade as _sim  # noqa: E402

CHASE_R = 0.25
SLIPS = {"base": 0.0005, "stress": 0.0010}
MAX_ENTRIES, DAY_STOP_R = 3, -2.0
DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "research" / "intraday_data"


def load_daily():
    d = pd.read_csv(DATA / "daily.csv.gz")
    d["date"] = pd.to_datetime(d["timestamp"], utc=True).dt.tz_convert("America/New_York").dt.date.astype(str)
    out = {}
    for sym, g in d.sort_values("date").groupby("symbol"):
        g = g.set_index("date")
        out[sym] = pd.DataFrame({"prev_close": g["close"].shift(1),
                                 "adv20": g["volume"].shift(1).rolling(20).mean()})
    return out


def candidate_idx(setup, d):
    m = d["m"].values
    if setup == "ORB15_V1":
        orb = d[d["m"] < 15]
        if orb.empty:
            return []
        hit = np.where((m >= 15) & (d["c"].values > orb["h"].max()))[0]
        return hit[:1].tolist()
    if setup == "PULLBACK_VWAP_V1":
        conf = (d["bull_engulf"] | d["hammer"] | (d["bull"] & d["close_above_prev_high"])).values
        near = (d["l"].rolling(4).min() <= d["vwap"] * 1.0015).values
        return np.where(conf & near & (m >= 30) & (m <= 330))[0].tolist()
    if setup == "MOMO_CONT_V1":
        prior10 = d["h"].shift(1).rolling(10).max().values
        return np.where((d["c"].values > prior10) & (m >= 30) & (m <= 330))[0].tolist()
    return []


def symbol_signals(sym):
    """All signals (and rejections) for one symbol over all sessions."""
    f = DATA / f"{sym}.csv.gz"
    if not f.exists():
        return sym, [], {}
    df = pd.read_csv(f, parse_dates=["t"])
    df["date"] = df["t"].dt.tz_convert("America/New_York").dt.date.astype(str)
    daily = DAILY.get(sym)
    sessions, out, rej = [], [], defaultdict(int)
    for date, g in df.groupby("date", sort=True):
        d = session_frame(g)
        hist = sessions[-20:]
        sessions.append(d)
        if len(hist) < 10 or daily is None or date not in daily.index:
            continue
        pc, adv = daily.loc[date, "prev_close"], daily.loc[date, "adv20"]
        if not (5 <= pc <= 100) or not (adv >= 1_000_000):
            rej["ineligible (price/ADV)"] += 1
            continue
        prof = volume_profile(hist)
        for setup in SETUP_IDS:
            st = {}
            for i in candidate_idx(setup, d):
                if i + 1 >= len(d):
                    continue
                rv = rvol(float(d["cumv"].iloc[i]), int(d["m"].iloc[i]), prof)
                s = evaluate(setup, d, i, sym, rv, None, st)
                if isinstance(s, Signal):
                    out.append((date, s, d))
                elif isinstance(s, str):
                    rej[reason_key(setup, s)] += 1
    # keep only what simulation needs: the session frame from the signal bar on
    return sym, [(date, s, d.iloc[int(np.searchsorted(d['m'].values, s.m)):].reset_index(drop=True))
                 for date, s, d in out], dict(rej)


def simulate_trade(s, d, slip):
    return _sim(s, d, slip, CHASE_R, session_over=True)


def portfolio(trades_by_day, account=False):
    """Apply live portfolio rules. trades_by_day: {date: [trade dicts incl. 'date']} (all candidate fills)."""
    taken, skips = [], defaultdict(int)
    cash, unsettled, eq_curve = 100.0, 0.0, []
    for date in sorted(trades_by_day):
        cash += unsettled; unsettled = 0.0            # T+1: yesterday's sale proceeds settle today
        day_r, n, free_at = 0.0, 0, -1
        for t in sorted(trades_by_day[date], key=lambda t: (t["m"], -(t["rvol"] or 0))):
            if t["m"] <= free_at:
                skips["one position at a time"] += 1; continue
            if n >= MAX_ENTRIES:
                skips["max 3 entries/day"] += 1; continue
            if day_r <= DAY_STOP_R:
                skips["daily loss limit"] += 1; continue
            if account:
                sh = min(math.floor(0.50 / t["risk"]), math.floor(cash / t["entry"]))
                if sh < 1:
                    skips["$100 account: no whole share fits risk/settled cash"] += 1; continue
                pnl = sh * (t["exit"] - t["entry"])
                cash -= sh * t["entry"]; unsettled += sh * t["exit"]
                t = {**t, "shares": sh, "pnl": pnl}
            taken.append({**t, "date": date})
            day_r += t["R"]; n += 1; free_at = t["exit_m"]
        eq_curve.append(cash + unsettled)
    return taken, dict(skips), eq_curve


def stats(trs):
    if not trs:
        return {"n": 0}
    r = np.array([t["R"] for t in trs])
    w, l = r[r > 0], r[r <= 0]
    cum = np.cumsum(r)
    dd = float((cum - np.maximum.accumulate(np.concatenate([[0], cum]))[1:]).min())
    return {"n": int(len(r)), "win_rate": round(float((r > 0).mean()), 3), "avg_win_R": round(float(w.mean()), 3) if len(w) else 0,
            "avg_loss_R": round(float(l.mean()), 3) if len(l) else 0, "expectancy_R": round(float(r.mean()), 4),
            "expectancy_se": round(float(r.std(ddof=1) / math.sqrt(len(r))), 4) if len(r) > 1 else None,
            "profit_factor": round(float(w.sum() / -l.sum()), 3) if l.sum() < 0 else None,
            "total_R": round(float(r.sum()), 2), "max_drawdown_R": round(dd, 2)}


def by(trs, key):
    g = defaultdict(list)
    for t in trs:
        g[key(t)].append(t)
    return {k: stats(v) for k, v in sorted(g.items())}


def main():
    syms = json.loads((DATA / "meta.json").read_text())["symbols"]
    with Pool() as p:
        res = p.map(symbol_signals, syms)
    sigs = [(date, s, d) for _, lst, _ in res for date, s, d in lst]
    rej = defaultdict(int)
    for _, _, r in res:
        for k, v in r.items():
            rej[k] += v
    dates = sorted({x[0] for x in sigs})
    all_days = sorted({d for _, lst, _ in res for d, _, _ in lst})
    cut = all_days[int(len(all_days) * 2 / 3)] if all_days else None
    out = {"method": __doc__.split("Usage")[0].strip(), "data": json.loads((DATA / "meta.json").read_text()) | {"symbols": len(syms)},
           "sessions": len(all_days), "oos_from": cut, "rejections_sample": dict(sorted(rej.items(), key=lambda x: -x[1])[:25]),
           "setups": {}}
    for setup in SETUP_IDS:
        out["setups"][setup] = {}
        for sname, slip in SLIPS.items():
            by_day = defaultdict(list)
            for date, s, d in sigs:
                if s.setup != setup:
                    continue
                t = simulate_trade(s, d, slip)
                if t:
                    by_day[date].append(t)
            taken, skips, _ = portfolio(by_day)
            ins = [t for t in taken if t["date"] < cut]
            oos = [t for t in taken if t["date"] >= cut]
            block = {"all": stats(taken), "in_sample": stats(ins), "out_of_sample": stats(oos), "portfolio_skips": skips}
            if sname == "stress":
                block["by_exit"] = {k: v["n"] for k, v in by(taken, lambda t: t["how"]).items()}
                block["by_time"] = by(taken, lambda t: f"{9 + (30 + t['m']) // 60:02d}:{'00' if (30 + t['m']) % 60 < 30 else '30'}")
                block["by_month"] = by(taken, lambda t: t["date"][:7])
                acct, askips, curve = portfolio(by_day, account=True)
                block["account_100"] = {"trades": len(acct), "pnl_usd": round(sum(t["pnl"] for t in acct), 2),
                                        "end_value": round(curve[-1], 2) if curve else 100.0,
                                        "min_value": round(min(curve), 2) if curve else 100.0, "skips": askips}
            out["setups"][setup][sname] = block
        st = out["setups"][setup]["stress"]
        crit = {"in_sample_expectancy_gt_0": (st["in_sample"].get("expectancy_R") or -1) > 0,
                "oos_expectancy_gt_0": (st["out_of_sample"].get("expectancy_R") or -1) > 0,
                "oos_trades_ge_30": st["out_of_sample"].get("n", 0) >= 30,
                "oos_profit_factor_gt_1_1": (st["out_of_sample"].get("profit_factor") or 0) > 1.1,
                "account_100_can_trade": st["account_100"]["trades"] > 0}
        out["setups"][setup]["activation_criteria_stress_costs"] = crit
        out["setups"][setup]["passes"] = all(crit.values())
    Path(ROOT / "research" / "intraday_backtest_results.json").write_text(json.dumps(out, indent=1, default=str))
    for k, v in out["setups"].items():
        print(k, "PASS" if v["passes"] else "fail", json.dumps(v["stress"]["all"]), json.dumps(v["stress"]["out_of_sample"]))


DAILY = load_daily() if (DATA / "daily.csv.gz").exists() else {}

if __name__ == "__main__":
    main()
