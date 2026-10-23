"""Gate 2 item 7: verify the backtest's methodology. Writes research/methodology_checks.json.

  1. look-ahead test: picks made with data truncated at day i equal picks made with the full history,
  2. data completeness: gaps, duplicates, non-positive prices, suspicious one-day jumps, open/close sanity,
  3. adjusted prices: calendar-year total returns of VTI/SPY from the data (to compare with published figures),
  4. ETF availability: first date of data per ETF vs the first decision date,
  5. path sensitivity: how much the 80/20 vs 100% VTI gap moves with trivial changes (start day, deposit day).
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_v2 import ALL, DATA, UNIVERSE, Params, load, run  # noqa: E402
from hbgtl.signals import Momentum, MomentumParams  # noqa: E402

O, C = load()
out = {}

# 1. look-ahead
mp = MomentumParams(universe=tuple(UNIVERSE))
full = Momentum(C, mp)
rng = np.random.default_rng(7)
days = sorted(rng.choice(np.arange(260, len(C) - 1), size=150, replace=False))
mism = 0
for i in days:
    trunc = Momentum(C.iloc[: i + 1], mp)
    held = full.picks(i - 1, [])
    if trunc.picks(i, held) != full.picks(i, held) or trunc.ranked(i) != full.ranked(i):
        mism += 1
out["look_ahead"] = {"days_tested": len(days), "mismatches": mism,
                     "timing": "decide on the close of day t using closes through t; fill at the open of t+1"}

# 2. data completeness
raw = {s: pd.read_csv(DATA / f"{s}.csv", parse_dates=["date"]) for s in ALL}
cal = sorted(set().union(*[set(df["date"]) for df in raw.values()]))
comp = {}
for s, df in raw.items():
    d = df.set_index("date")
    r = d["close"].pct_change().abs()
    comp[s] = {"rows": len(d), "first": str(d.index.min().date()), "last": str(d.index.max().date()),
               "missing_vs_calendar": len(cal) - len(d), "duplicate_dates": int(d.index.duplicated().sum()),
               "non_positive": int((d[["open", "high", "low", "close"]] <= 0).sum().sum()),
               "open_outside_high_low": int(((d["open"] > d["high"] * 1.0001) | (d["open"] < d["low"] * 0.9999)).sum()),
               "days_moving_over_15pct": [str(x.date()) for x in r[r > 0.15].index]}
out["data_completeness"] = {"calendar_days": len(cal), "per_etf": comp}

# 3. adjusted-price total returns by calendar year (dividends included via adjustment)
yr = {}
for s in ("VTI", "SPY", "BIL", "TLT"):
    c = C[s]
    yr[s] = {str(y): round(float(c[c.index.year == y].iloc[-1] / c[c.index.year == y - 1].iloc[-1] - 1), 4)
             for y in range(2017, 2026)}
out["calendar_year_total_returns"] = yr

# 4. availability
out["availability"] = {s: comp[s]["first"] for s in ALL} | {"first_decision_date": "2017-01-03",
                                                           "lookback_needed_days": 252}

# 5. path sensitivity: start month (18 runs) and small cost changes
gaps = []
for m in pd.date_range("2017-01-01", "2018-06-01", freq="MS"):
    st = str(m.date())
    a = run(O, C, Params(core_w=1.0), start=st)
    c = run(O, C, Params(core_w=0.8), start=st)
    gaps.append({"start": st, "contributed": a.contributed, "vti": round(float(a.value.iloc[-1]), 2),
                 "c_80_20": round(float(c.value.iloc[-1]), 2),
                 "gap_pct": round(float(c.value.iloc[-1] / a.value.iloc[-1] - 1), 4)})
g = np.array([x["gap_pct"] for x in gaps])
costs = {f"{x:.2%}": round(float(run(O, C, Params(core_w=0.8, cost=x)).value.iloc[-1]), 2) for x in (0.0004, 0.0005, 0.0006)}
out["path_sensitivity"] = {"runs": gaps, "gap_min": float(g.min()), "gap_median": float(np.median(g)),
                           "gap_max": float(g.max()), "share_c_beats_vti": float((g > 0).mean()),
                           "c_80_20_final_by_cost": costs}
Path(Path(__file__).parent / "methodology_checks.json").write_text(json.dumps(out, indent=1))
print(json.dumps({k: v for k, v in out.items() if k != "data_completeness"}, indent=1))
print(json.dumps({s: {k: v for k, v in d.items() if k not in ("rows", "first", "last")} for s, d in comp.items()}))
