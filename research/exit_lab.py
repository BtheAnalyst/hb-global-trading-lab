"""Exit-rule comparison for the live entry signals (research only, no orders). Nov 2 2026.

Question (founder): instead of taking profit at a fixed 2R target, should the bot "ride the wave" — a further
target, a trailing stop, or hold until the 15:50 flatten?

Method (predeclared before running):
  * SAME entry signals, entry fills, initial stop, VWAP invalidation and 15:50 flatten as the live engine
    (signals from research/intraday_backtest.symbol_signals; next-bar entry with the 0.25R chase cap).
  * Only the profit-taking rule changes. Variants:
      T2      fixed 2R target (live today, baseline)
      T3      fixed 3R target
      T5      fixed 5R target
      HOLD    no target: stop, VWAP invalidation, or 15:50 flatten
      BE_T3   move stop to breakeven after +1R, 3R target
      TRAIL1  move stop to breakeven after +1R, then trail 1R under the highest high, no target
      TRAIL15 same, trail 1.5R
      HALF    sell half at 2R, rest trails 1R under the high (stop to breakeven after 2R)
  * Trailing stops update only from COMPLETED bars and apply from the next bar (no look-ahead). A bar touching
    the stop is treated as stopped first. Stops/invalidations/flatten pay slippage; targets fill at the limit.
  * Costs: stress 10 bps per side (the bar every strategy must clear). Live portfolio rules (one position,
    max 3/day, -2R day stop). In-sample first 2/3 of sessions, out-of-sample last 1/3.

Usage: python research/exit_lab.py <data_dir>   -> research/exit_lab_results.json
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))
import intraday_backtest as ib  # noqa: E402  (reads the data dir from argv[1])
from hbgtl.setups import invalidated  # noqa: E402

SLIP = 0.0010
VARIANTS = {
    "T2": dict(target=2.0), "T3": dict(target=3.0), "T5": dict(target=5.0), "HOLD": dict(),
    "BE_T3": dict(target=3.0, be_at=1.0), "TRAIL1": dict(be_at=1.0, trail=1.0),
    "TRAIL15": dict(be_at=1.0, trail=1.5), "HALF": dict(half_at=2.0, be_at=2.0, trail=1.0),
}


def sim(s, d, v, slip=SLIP, chase_r=0.25):
    if len(d) < 2:
        return None
    risk0 = s.ref - s.stop
    limit = s.ref + chase_r * risk0
    b = d.iloc[1]
    if b["o"] <= limit:
        entry = b["o"] * (1 + slip)
    elif b["l"] <= limit:
        entry = limit
    else:
        return None
    if entry <= s.stop:
        return None
    risk = entry - s.stop
    stop = s.stop
    target = entry + v["target"] * risk if v.get("target") else None
    half_px = entry + v["half_at"] * risk if v.get("half_at") else None
    banked, size = 0.0, 1.0                     # R already realized on the sold half; fraction still held
    hi = entry
    H, L, O, C, M = (d[k].to_numpy(float) for k in ("h", "l", "o", "c", "m"))

    def done(px, how, m):
        r = banked + size * (px - entry) / risk
        return {"symbol": s.symbol, "m": s.m, "exit_m": int(m), "entry": entry, "risk": risk, "R": round(r, 4),
                "how": how, "rvol": s.rvol, "exit": entry + r * risk}

    for j in range(1, len(d)):
        if L[j] <= stop:
            px = (min(O[j], stop) if j > 1 else stop) * (1 - slip)
            return done(px, "stop" if stop < entry else "trail/BE stop", M[j])
        if half_px is not None and size == 1.0 and H[j] >= half_px + 0.01:
            banked, size = 0.5 * (half_px - entry) / risk, 0.5
        if target is not None and H[j] >= target + 0.01:
            return done(target, "target", M[j])
        why = invalidated(s.setup, d, j, entry)
        if why:
            px = (O[j + 1] if j + 1 < len(d) else C[j]) * (1 - slip)
            return done(px, why.split(":")[0], M[j] + 1)
        # end of completed bar j: update the protective stop for the next bar
        hi = max(hi, H[j])
        if v.get("be_at") and hi >= entry + v["be_at"] * risk:
            stop = max(stop, entry)
            if v.get("trail"):
                stop = max(stop, hi - v["trail"] * risk)
    return done(C[-1] * (1 - slip), "session end", M[-1])


def stats(trs):
    if not trs:
        return {"n": 0}
    r = np.array([t["R"] for t in trs])
    w, l = r[r > 0], r[r <= 0]
    se = r.std(ddof=1) / math.sqrt(len(r)) if len(r) > 1 else float("nan")
    return {"n": int(len(r)), "win_rate": round(float((r > 0).mean()), 3), "avg_win_R": round(float(w.mean()), 3) if len(w) else 0,
            "avg_loss_R": round(float(l.mean()), 3) if len(l) else 0, "expectancy_R": round(float(r.mean()), 4),
            "t_stat": round(float(r.mean() / se), 2) if se > 0 else None,
            "profit_factor": round(float(w.sum() / -l.sum()), 3) if l.sum() < 0 else None,
            "total_R": round(float(r.sum()), 2), "best_R": round(float(r.max()), 2)}


def account_100(taken):
    """$100 cash account, fractional shares, 0.5% risk, settled cash only (T+1)."""
    eq, low = 100.0, 100.0
    by_day = defaultdict(list)
    for t in taken:
        by_day[t["date"]].append(t)
    for dte in sorted(by_day):
        settled = eq
        for t in by_day[dte]:
            notional = min(settled, 0.005 * eq / (t["risk"] / t["entry"]))
            if notional < 1.0:
                continue
            settled -= notional
            eq += notional * (t["exit"] / t["entry"] - 1)
            low = min(low, eq)
    return {"end_value": round(eq, 2), "min_value": round(low, 2)}


def main():
    syms = json.loads((ib.DATA / "meta.json").read_text())["symbols"]
    with Pool() as p:
        res = p.map(ib.symbol_signals, syms)
    sigs = [(date, s, d) for _, lst, _ in res for date, s, d in lst]
    all_days = sorted({x[0] for x in sigs})
    cut = all_days[int(len(all_days) * 2 / 3)]
    out = {"method": __doc__.split("Usage")[0].strip(), "sessions": len(all_days), "first": all_days[0],
           "last": all_days[-1], "oos_from": cut, "setups": {}}
    for setup in ib.SETUP_IDS:
        out["setups"][setup] = {}
        for name, v in VARIANTS.items():
            by_day = defaultdict(list)
            for date, s, d in sigs:
                if s.setup == setup:
                    t = sim(s, d, v)
                    if t:
                        by_day[date].append(t)
            taken, _, _ = ib.portfolio(by_day)
            ins = [t for t in taken if t["date"] < cut]
            oos = [t for t in taken if t["date"] >= cut]
            hows = defaultdict(int)
            for t in taken:
                hows[t["how"]] += 1
            blk = {"all": stats(taken), "in_sample": stats(ins), "out_of_sample": stats(oos),
                   "account_100": account_100(taken), "by_exit": dict(hows)}
            out["setups"][setup][name] = blk
            a, o = blk["all"], blk["out_of_sample"]
            print(f"{setup:17s} {name:8s} n={a.get('n',0):4d} win={a.get('win_rate')} E={a.get('expectancy_R')}R "
                  f"t={a.get('t_stat')} PF={a.get('profit_factor')} best={a.get('best_R')} "
                  f"IS={blk['in_sample'].get('expectancy_R')} OOS={o.get('expectancy_R')}({o.get('n',0)}) "
                  f"$100->{blk['account_100']['end_value']}", flush=True)
    (ROOT / "research" / "exit_lab_results.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
