"""Backtest on market-wide GAPPERS (open >= 10% above prior close, prior close $2-$20, ADV20 >= 300k; selection
uses only information available at the open). Same simulator, costs, portfolio rules and activation criteria
as research/intraday_backtest.py.

  GAPGO_PB_V1   Setup D: Ross Cameron-style first pullback after the opening push
  ORB15_INPLAY  Setup A (opening-range breakout) applied to the gapper "stocks in play" list (SMB-style)

Two price bands are reported: $2-$20 (Cameron) and $5-$20 (the directive's $5 floor).
Usage: python research/gapper_backtest.py <gapper_data_dir>
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))
GDATA = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "research" / "gapper_data"
sys.argv = sys.argv[:1]
import intraday_backtest as ib  # noqa: E402
from hbgtl.candles import rvol_daily, session_frame  # noqa: E402
from hbgtl.setups import Signal, evaluate, reason_key  # noqa: E402

GAPPERS = pd.read_csv(GDATA / "gappers.csv")


def day_signals(date):
    f = GDATA / "minute" / f"{date}.csv.gz"
    if not f.exists():
        return []
    df = pd.read_csv(f, parse_dates=["t"])
    out, rej = [], defaultdict(int)
    for _, g in GAPPERS[GAPPERS["date"] == date].iterrows():
        bars = df[df["symbol"] == g["symbol"]]
        if len(bars) < 30:
            continue
        d = session_frame(bars[["t", "o", "h", "l", "c", "v"]])
        d = d[(d["m"] >= 0) & (d["m"] < 390)].reset_index(drop=True)
        if d.empty or d["m"].iloc[0] > 2:
            continue
        for setup, sid in (("GAPGO_PB_V1", "GAPGO_PB_V1"), ("ORB15_V1", "ORB15_INPLAY")):
            st = {"gap": float(g["gap"])}
            idx = range(len(d)) if setup == "GAPGO_PB_V1" else ib.candidate_idx("ORB15_V1", d)
            for i in idx:
                if i + 1 >= len(d) or int(d["m"].iloc[i]) > 120:
                    continue
                rv = rvol_daily(float(d["cumv"].iloc[i]), int(d["m"].iloc[i]), float(g["adv20"]))
                s = evaluate(setup, d, i, g["symbol"], rv, None, st)
                if isinstance(s, Signal):
                    s.setup = sid
                    k = int(d.index[d["m"] == s.m][0])
                    out.append((date, float(g["prev_close"]), s, d.iloc[k:].reset_index(drop=True)))
                elif isinstance(s, str):
                    rej[reason_key(sid, s)] += 1
    return out


def main():
    dates = sorted(GAPPERS["date"].unique())
    with Pool() as p:
        res = p.map(day_signals, dates)
    sigs = [x for r in res for x in r]
    cut = dates[int(len(dates) * 2 / 3)]
    out = {"method": __doc__.split("Usage")[0].strip(), "gapper_days": int(len(GAPPERS)), "sessions": len(dates),
           "oos_from": cut, "setups": {}}
    for sid in ("GAPGO_PB_V1", "ORB15_INPLAY"):
        for band, lo in (("2-20", 2.0), ("5-20", 5.0)):
            key = f"{sid} ${band}"
            out["setups"][key] = {}
            for sname, slip in ib.SLIPS.items():
                by_day = defaultdict(list)
                for date, pc, s, d in sigs:
                    if s.setup != sid or pc < lo:
                        continue
                    t = ib.simulate_trade(s, d, slip)
                    if t:
                        by_day[date].append(t)
                taken, skips, _ = ib.portfolio(by_day)
                ins = [t for t in taken if t["date"] < cut]
                oos = [t for t in taken if t["date"] >= cut]
                blk = {"all": ib.stats(taken), "in_sample": ib.stats(ins), "out_of_sample": ib.stats(oos),
                       "portfolio_skips": skips}
                if sname == "stress":
                    blk["by_exit"] = {k: v["n"] for k, v in ib.by(taken, lambda t: t["how"]).items()}
                    blk["by_month"] = ib.by(taken, lambda t: t["date"][:7])
                    acct, askips, curve = ib.portfolio(by_day, account=True)
                    blk["account_100"] = {"trades": len(acct), "pnl_usd": round(sum(t["pnl"] for t in acct), 2),
                                          "end_value": round(curve[-1], 2) if curve else 100.0, "skips": askips}
                out["setups"][key][sname] = blk
            st = out["setups"][key]["stress"]
            crit = {"in_sample_expectancy_gt_0": (st["in_sample"].get("expectancy_R") or -1) > 0,
                    "oos_expectancy_gt_0": (st["out_of_sample"].get("expectancy_R") or -1) > 0,
                    "oos_trades_ge_30": st["out_of_sample"].get("n", 0) >= 30,
                    "oos_profit_factor_gt_1_1": (st["out_of_sample"].get("profit_factor") or 0) > 1.1,
                    "account_100_can_trade": st["account_100"]["trades"] > 0}
            out["setups"][key]["activation_criteria_stress_costs"] = crit
            out["setups"][key]["passes"] = all(crit.values())
            print(key, "PASS" if out["setups"][key]["passes"] else "fail", json.dumps(st["all"]),
                  "OOS", json.dumps(st["out_of_sample"]), "$100:", st["account_100"])
    (ROOT / "research" / "gapper_backtest_results.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
