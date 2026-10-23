"""Gate 1 research suite: the four mandated portfolios plus robustness checks. Writes research/gate1_results.json."""
import itertools
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_v2 import Params, load, regime_returns, run  # noqa: E402

O, C = load()
out = {"data": f"{C.index[0].date()} to {C.index[-1].date()}", "accounts_start": "2017-01-03"}
PORTS = {"A 100% VTI": 1.0, "B 90/10": 0.9, "C 80/20": 0.8, "D 70/30": 0.7}


def summarize(r):
    yrs = (r.nav.index[-1] - r.nav.index[0]).days / 365.25
    avg_eq = float(r.value.mean())
    return {"contributed": round(r.contributed, 2), "final_total": round(r.value.iloc[-1], 2),
            "trading_equity": round(r.final_equity, 2), "vault": round(r.vault, 2),
            "net_profit": round(r.value.iloc[-1] - r.contributed, 2), "xirr": round(r.xirr, 4),
            "twr_cagr": round(r.twr_cagr, 4), "max_dd": round(r.max_dd, 4), "trades": r.trades,
            "sleeve_trades": r.sleeve_trades, "turnover_per_yr": round(r.traded_dollars / avg_eq / yrs, 3),
            "costs": round(r.costs, 2), "realized": round(r.realized, 2), "tax_reserve_est": round(r.tax_reserve, 2),
            "accessible_cash": round(r.accessible_cash, 2),
            "regimes": {k: round(v, 4) for k, v in regime_returns(r.nav).items()}}


# 1. Mandated comparison, predeclared parameters, full reinvestment
res = {k: run(O, C, Params(core_w=w)) for k, w in PORTS.items()}
out["main"] = {k: summarize(r) for k, r in res.items()}
vti = res["A 100% VTI"]
for k, r in res.items():
    out["main"][k]["vs_vti_final"] = round(r.value.iloc[-1] - vti.value.iloc[-1], 2)

# 2. Profit Vault policies (70/30 vs full reinvestment)
out["vault"] = {k: summarize(run(O, C, Params(core_w=w, vault_pct=0.30))) for k, w in PORTS.items()}

# 3. Cost sensitivity
out["costs"] = {f"{c:.2%}": {k: round(run(O, C, Params(core_w=w, cost=c)).value.iloc[-1], 2) for k, w in PORTS.items()}
                for c in (0.0005, 0.0010, 0.0020)}

# 4. Does the sleeve add value? Sleeve alone (0% core) vs VTI, time-weighted
sl = run(O, C, Params(core_w=0.0))
out["sleeve_alone"] = summarize(sl)

# 5. Parameter sensitivity (80/20) and walk-forward: pick on 2017-2021, test 2022-2026
grid = list(itertools.product([(63, 126, 252), (126,), (252,)], [150, 200, 250], [2, 3, 4]))
ins, oos = {}, {}
for lb, sma, ex in grid:
    p = Params(core_w=0.0, lookbacks=lb, sma=sma, exit_rank=ex)      # judge the sleeve itself
    key = f"lb={'+'.join(map(str, lb))} sma={sma} exit_top{ex}"
    ins[key] = run(O, C, p, start="2017-01-01", end="2021-12-31").twr_cagr
    oos[key] = run(O, C, p, start="2022-01-01").twr_cagr
best = max(ins, key=ins.get)
default = "lb=63+126+252 sma=200 exit_top3"
vti_ins = run(O, C, Params(core_w=1.0), start="2017-01-01", end="2021-12-31").twr_cagr
vti_oos = run(O, C, Params(core_w=1.0), start="2022-01-01").twr_cagr
out["walk_forward"] = {
    "in_sample": "2017-2021", "out_of_sample": "2022-01 to 2026-10",
    "best_in_sample": best, "best_in_sample_twr": round(ins[best], 4), "best_oos_twr": round(oos[best], 4),
    "default_in_sample_twr": round(ins[default], 4), "default_oos_twr": round(oos[default], 4),
    "vti_in_sample_twr": round(vti_ins, 4), "vti_oos_twr": round(vti_oos, 4),
    "grid_oos_median": round(float(np.median(list(oos.values()))), 4),
    "grid_oos_min": round(min(oos.values()), 4), "grid_oos_max": round(max(oos.values()), 4),
    "grid_share_beating_vti_oos": round(float(np.mean([v > vti_oos for v in oos.values()])), 3),
    "grid_share_beating_vti_ins": round(float(np.mean([v > vti_ins for v in ins.values()])), 3),
}

# 6. One-year windows for the $100 founder's account (every start month 2017-01 .. 2025-10)
months = pd.date_range("2017-01-01", "2025-10-01", freq="MS")
win = {}
for k, w in PORTS.items():
    finals = [run(O, C, Params(core_w=w), start=str(m.date()), end=str((m + pd.DateOffset(months=12) - pd.DateOffset(days=1)).date())).value.iloc[-1]
              for m in months]
    a = np.array(finals)
    win[k] = {"windows": len(a), "median": round(float(np.median(a)), 2), "p10": round(float(np.percentile(a, 10)), 2),
              "worst": round(float(a.min()), 2), "best": round(float(a.max()), 2),
              "share_below_1200": round(float(np.mean(a < 1200)), 3)}
out["one_year_windows"] = win

Path(Path(__file__).parent / "gate1_results.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
