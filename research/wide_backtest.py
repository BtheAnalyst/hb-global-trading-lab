"""Setup A (ORB15_V1) on the WIDE universe: every liquid U.S. common stock that day (prior close $5-$100,
20-day average volume >= 1M), unchanged rules (relative volume >= 2.0 etc.), same simulator, costs, portfolio
rules and activation bar as research/intraday_backtest.py. Also reports the $100 Founder account with
whole shares only vs fractional shares.

Usage: python research/wide_backtest.py <wide_data_dir> [<pool_data_dir> for the 172-stock comparison]
"""
from __future__ import annotations

import gzip
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
sys.path.insert(0, str(ROOT / "research"))
W = Path(sys.argv[1])
sys.argv = sys.argv[:1]
import intraday_backtest as ib  # noqa: E402
from hbgtl.candles import rvol, session_frame  # noqa: E402
from hbgtl.intraday_universe import RESEARCH_POOL  # noqa: E402
from hbgtl.setups import Signal, evaluate  # noqa: E402

DAILY = pd.read_csv(W / "daily.csv.gz")
ELIG = DAILY[DAILY["prev_close"].between(5, 100) & (DAILY["adv20"] >= 1_000_000)]
ELIG = {(r.symbol, r.date) for r in ELIG.itertuples()}


def profiles_for(date):
    f = W / "profiles" / f"{date}.json.gz"
    if not f.exists():
        return {}
    with gzip.open(f, "rt") as fh:
        return json.load(fh)


def day_signals(f):
    date = f.name.split(".")[0]
    df = pd.read_csv(f, parse_dates=["t"])
    profs = profiles_for(date)
    out = []
    for sym, g in df.groupby("symbol"):
        if (sym, date) not in ELIG:
            continue
        d = session_frame(g[["t", "o", "h", "l", "c", "v"]])
        d = d[(d["m"] >= 0) & (d["m"] < 390)].reset_index(drop=True)
        prof = profs.get(sym)
        if d.empty or not prof:
            continue
        prof = {int(k): v for k, v in prof.items()}
        st = {}
        for i in ib.candidate_idx("ORB15_V1", d):
            if i + 1 >= len(d):
                continue
            s = evaluate("ORB15_V1", d, i, sym, rvol(float(d["cumv"].iloc[i]), int(d["m"].iloc[i]), prof), None, st)
            if isinstance(s, Signal):
                out.append((date, s, d.iloc[i:].reset_index(drop=True)))
    return out


def account(by_day, fractional: bool, cash0=100.0, risk_pct=0.005):
    """$100 cash account: settled cash only (T+1), 0.5% risk, whole shares (>= 75% of exact size) or fractional."""
    cash, unsettled, taken, skips, curve = cash0, 0.0, [], defaultdict(int), []
    for date in sorted(by_day):
        cash += unsettled; unsettled = 0.0
        eq = cash
        day_r, n, free_at = 0.0, 0, -1
        for t in sorted(by_day[date], key=lambda t: (t["m"], -(t["rvol"] or 0))):
            if t["m"] <= free_at or n >= ib.MAX_ENTRIES or day_r <= ib.DAY_STOP_R:
                continue
            exact = min(risk_pct * eq / t["risk"], cash / t["entry"])
            sh = math.floor(exact)
            if sh < 1 or sh < 0.75 * exact:
                if fractional and exact * t["entry"] >= 1.0:
                    sh = math.floor(exact * 1e6) / 1e6
                elif sh < 1:
                    skips["no whole share fits"] += 1
                    continue
            pnl = sh * (t["exit"] - t["entry"])
            cash -= sh * t["entry"]; unsettled += sh * t["exit"]
            taken.append({**t, "shares": sh, "pnl": pnl})
            day_r += t["R"]; n += 1; free_at = t["exit_m"]
        curve.append(cash + unsettled)
    return {"trades": len(taken), "pnl_usd": round(sum(t["pnl"] for t in taken), 2),
            "end_value": round(curve[-1], 2) if curve else cash0, "min_value": round(min(curve), 2) if curve else cash0,
            "skips": dict(skips)}


def main():
    files = sorted((W / "minute").glob("*.csv.gz"))
    with Pool() as p:
        res = p.map(day_signals, files)
    sigs = [x for r in res for x in r]
    dates = sorted(f.name.split(".")[0] for f in files)
    cut = "2026-08-31"                       # same split date as the pool backtest
    out = {"method": __doc__.split("Usage")[0].strip(), "meta": json.loads((W / "meta.json").read_text()),
           "sessions_with_candidates": len(dates), "oos_from": cut, "variants": {}}
    pool = set(RESEARCH_POOL)
    for vname, keep in (("wide", lambda s: True), ("pool_only", lambda s: s.symbol in pool),
                        ("wide_minus_pool", lambda s: s.symbol not in pool)):
        blk = {}
        for sname, slip in ib.SLIPS.items():
            by_day = defaultdict(list)
            for date, s, d in sigs:
                if keep(s):
                    t = ib.simulate_trade(s, d, slip)
                    if t:
                        by_day[date].append(t)
            taken, skips, _ = ib.portfolio(by_day)
            ins = [t for t in taken if t["date"] < cut]
            oos = [t for t in taken if t["date"] >= cut]
            blk[sname] = {"all": ib.stats(taken), "in_sample": ib.stats(ins), "out_of_sample": ib.stats(oos),
                          "portfolio_skips": skips}
            if sname == "stress":
                blk[sname]["by_month"] = {k: (v["n"], v.get("expectancy_R")) for k, v in
                                          ib.by(taken, lambda t: t["date"][:7]).items()}
                blk[sname]["trades_per_week"] = round(len(taken) / max(1, len(set(d for d, _, _ in sigs)) / 5), 2)
                blk[sname]["account_100_whole"] = account(by_day, False)
                blk[sname]["account_100_fractional"] = account(by_day, True)
                blk[sname]["account_10000"] = account(by_day, False, 10000.0)
        st = blk["stress"]
        crit = {"in_sample_expectancy_gt_0": (st["in_sample"].get("expectancy_R") or -1) > 0,
                "oos_expectancy_gt_0": (st["out_of_sample"].get("expectancy_R") or -1) > 0,
                "oos_trades_ge_30": st["out_of_sample"].get("n", 0) >= 30,
                "oos_profit_factor_gt_1_1": (st["out_of_sample"].get("profit_factor") or 0) > 1.1}
        blk["activation_criteria_stress_costs"] = crit
        blk["passes"] = all(crit.values())
        out["variants"][vname] = blk
        print(vname, "PASS" if blk["passes"] else "fail", "ALL", json.dumps(st["all"]), "\n   IS", json.dumps(st["in_sample"]),
              "\n   OOS", json.dumps(st["out_of_sample"]), "\n   $100 whole", st["account_100_whole"],
              "\n   $100 frac", st["account_100_fractional"], "\n   $10k", st["account_10000"], flush=True)
    (ROOT / "research" / "wide_backtest_results.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
