"""Gate 2 evidence for the proposed risk thresholds (2017-01 to 2026-10, $100 + $100/month).

Measures, before any threshold is implemented:
  1. how often daily losses of various sizes happened, and what the following days looked like
     (does pausing after a bad day actually help?),
  2. the largest position weights the strategy reaches on its own (are 90% core / 15% per-ETF caps binding?),
  3. the Momentum sleeve's drawdown history (where should a sleeve circuit breaker sit?),
  4. losing-exit streaks (how often a 4-losses-in-a-row rule would trip),
  5. total-wealth drawdown episodes (the 20% owner alert).
Writes research/risk_evidence.json.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_v2 import CORE, DEFENSIVE, UNIVERSE, Params, load, run  # noqa: E402

O, C = load()
out = {}


def episodes(nav: pd.Series, thresh: float):
    """Count separate drawdown episodes deeper than thresh (an episode ends at a new high)."""
    peak = nav.cummax(); dd = 1 - nav / peak
    n, inside, depth, worst = 0, False, [], 0.0
    for v, at_peak in zip(dd, nav >= peak):
        if at_peak:
            if inside:
                depth.append(worst)
            inside, worst = False, 0.0
        else:
            worst = max(worst, v)
            if v > thresh and not inside:
                inside = True; n += 1
    if inside:
        depth.append(worst)
    return n, [round(x, 4) for x in depth]


# ---------- 1. Daily losses ----------
daily = {}
for name, w in {"A 100% VTI": 1.0, "C 80/20": 0.8}.items():
    r = run(O, C, Params(core_w=w))
    ret = r.nav.pct_change().dropna()
    fwd1, fwd5 = ret.shift(-1), r.nav.shift(-5) / r.nav - 1
    row = {"days": int(len(ret)), "worst_day": round(float(ret.min()), 4)}
    for t in (0.02, 0.03, 0.04, 0.05):
        m = ret <= -t
        row[f"days_down_{int(t*100)}pct_or_more"] = int(m.sum())
        row[f"after_{int(t*100)}pct_drop_next_day_avg"] = round(float(fwd1[m].mean()), 4) if m.any() else None
        row[f"after_{int(t*100)}pct_drop_next_5d_avg"] = round(float(fwd5.reindex(m[m].index).mean()), 4) if m.any() else None
    row["all_days_next_day_avg"] = round(float(ret.mean()), 4)
    # how much of the worst days' loss was an overnight gap (open vs prior close) for VTI: a pause can't stop gaps
    daily[name] = row
gap = (O[CORE] / C[CORE].shift(1) - 1).loc["2017":]
day = (C[CORE] / C[CORE].shift(1) - 1).loc["2017":]
worst10 = day.nsmallest(10)
daily["VTI worst 10 days: share of loss already in the opening gap"] = round(float((gap[worst10.index] / worst10).clip(0, 1).mean()), 3)
out["daily_loss"] = daily

# ---------- 2. Position weights reached naturally (80/20 and 70/30) ----------
pos = {}
for name, w in {"B 90/10": 0.9, "C 80/20": 0.8, "D 70/30": 0.7}.items():
    r = run(O, C, Params(core_w=w, trace=True))
    W = pd.DataFrame({d: ws for d, ws in r.weights}).T.fillna(0.0).loc["2017-03":]   # skip first deposit months
    single = W[[s for s in UNIVERSE if s in W]].max(axis=1)
    pos[name] = {"core_max": round(float(W[CORE].max()), 4), "core_p99": round(float(W[CORE].quantile(0.99)), 4),
                 "core_days_over_90pct": int((W[CORE] > 0.90).sum()),
                 "single_sleeve_etf_max": round(float(single.max()), 4),
                 "single_sleeve_etf_p99": round(float(single.quantile(0.99)), 4),
                 "days_any_sleeve_etf_over_15pct": int((single > 0.15).sum()),
                 "days_any_sleeve_etf_over_20pct": int((single > 0.20).sum()),
                 "bil_max": round(float(W.get(DEFENSIVE, pd.Series([0])).max()), 4), "days": int(len(W))}
out["position_weights"] = pos

# ---------- 3. Sleeve drawdowns (sleeve alone, time-weighted) ----------
sl = run(O, C, Params(core_w=0.0))
dd = 1 - sl.nav / sl.nav.cummax()
sd = {"max_dd": round(float(dd.max()), 4), "max_dd_date": str(dd.idxmax().date())}
for t in (0.10, 0.15, 0.20, 0.25):
    n, _ = episodes(sl.nav, t)
    sd[f"episodes_deeper_than_{int(t*100)}pct"] = n
    sd[f"share_of_days_below_{int(t*100)}pct"] = round(float((dd > t).mean()), 4)
sd["episode_depths_over_10pct"] = episodes(sl.nav, 0.10)[1]
vti_dd = 1 - run(O, C, Params(core_w=1.0)).nav
out["sleeve_drawdown"] = sd

# ---------- 4. Losing-exit streaks (80/20 sleeve exits: full sales of momentum ETFs) ----------
r = run(O, C, Params(core_w=0.8))
ex = [f for f in r.fills if f[1] == "sell" and f[2] in UNIVERSE]
wins = [f[4] > 0 for f in ex]
streaks, cur = [], 0
for wn in wins:
    if wn:
        if cur:
            streaks.append(cur)
        cur = 0
    else:
        cur += 1
if cur:
    streaks.append(cur)
out["exit_streaks"] = {"exits": len(ex), "win_rate": round(float(np.mean(wins)), 3),
                       "avg_win": round(float(np.mean([f[4] for f in ex if f[4] > 0])), 2),
                       "avg_loss": round(float(np.mean([f[4] for f in ex if f[4] <= 0])), 2),
                       "longest_losing_streak": max(streaks) if streaks else 0,
                       **{f"streaks_of_{k}_or_more": sum(s >= k for s in streaks) for k in (3, 4, 5, 6)},
                       "exits_per_year": round(len(ex) / ((r.nav.index[-1] - r.nav.index[0]).days / 365.25), 1)}

# ---------- 5. Total-wealth drawdown episodes (owner alert) ----------
wd = {}
for name, w in {"A 100% VTI": 1.0, "C 80/20": 0.8}.items():
    nav = run(O, C, Params(core_w=w)).nav
    wd[name] = {f"episodes_deeper_than_{int(t*100)}pct": episodes(nav, t)[0] for t in (0.10, 0.15, 0.20, 0.30)}
out["wealth_drawdown"] = wd

Path(Path(__file__).parent / "risk_evidence.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
