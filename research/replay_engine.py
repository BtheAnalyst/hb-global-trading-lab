"""Replay the LIVE engine (hbgtl.equity_runner) day by day over history and compare with backtest_v2.

Each simulated morning the engine sees completed closes through yesterday and fills at today's open
(SimGateway, 0.05% slippage per side). parity=True uses the backtest's timing, so differences isolate
engine logic; parity=False shows the effect of the live timing (cash invested a day sooner).
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "research"))
import yaml  # noqa: E402

from backtest_v2 import Params, load, run  # noqa: E402
from hbgtl.equity_gateway import SimGateway  # noqa: E402
from hbgtl.equity_ledger import Ledger  # noqa: E402
from hbgtl.equity_risk import RiskConfig  # noqa: E402
from hbgtl.equity_runner import Engine, specs_from_cfg  # noqa: E402


def replay(cfg, O, C, start="2017-01-01", end=None, parity=True, safeguards=False, cost=0.0005, journal=None):
    specs = [s for s in specs_from_cfg(cfg) if s.mode == "shadow"]
    eng = Engine(specs, RiskConfig.from_cfg(cfg.get("risk")), journal=journal)
    idx = C.index
    j0 = int(np.searchsorted(idx, pd.Timestamp(start)))
    j1 = len(idx) if end is None else int(np.searchsorted(idx, pd.Timestamp(end), side="right"))
    state, gw = {}, SimGateway(cost=cost)
    values = {s.pid: [] for s in specs}
    alerts = []
    for j in range(j0, j1):
        gw.set_prices(O.iloc[j].to_dict())
        gw.orders = {}
        summ = eng.run_day(state, C, j - 1, str(idx[j].date()), {"shadow": gw}, parity=parity, safeguards=safeguards)
        alerts += [(str(idx[j].date()), a) for a in summ["alerts"]]
        pxc = C.iloc[j].to_dict()
        for s in specs:
            values[s.pid].append(Ledger.from_dict(state["portfolios"][s.pid]).total(pxc))
    return state, {k: pd.Series(v, index=idx[j0:j1]) for k, v in values.items()}, alerts


if __name__ == "__main__":
    cfg = yaml.safe_load(open(ROOT / "config" / "equity.yaml"))
    O, C = load()
    out = {}
    for parity in (True, False):
        state, vals, _ = replay(cfg, O, C, parity=parity)
        rows = {}
        for spec in specs_from_cfg(cfg):
            if spec.mode != "shadow":
                continue
            bt = run(O, C, Params(core_w=spec.rule.core_w, vault_pct=spec.vault_pct))
            led = Ledger.from_dict(state["portfolios"][spec.pid])
            eng_final = float(vals[spec.pid].iloc[-1])
            rows[spec.pid] = {"backtest_final": round(float(bt.value.iloc[-1]), 2), "engine_final": round(eng_final, 2),
                              "diff_pct": round(eng_final / float(bt.value.iloc[-1]) - 1, 5),
                              "backtest_trades": bt.trades, "engine_trades": led.trades,
                              "backtest_vault": round(bt.vault, 2), "engine_vault": round(led.vault, 2),
                              "contributed": led.contributed}
        out["parity_timing" if parity else "live_timing"] = rows
    state, vals, alerts = replay(cfg, O, C, parity=False, safeguards=True)
    out["live_timing_with_safeguards"] = {
        pid: round(float(v.iloc[-1]), 2) for pid, v in vals.items()}
    out["safeguard_alerts"] = alerts
    (ROOT / "research" / "replay_results.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
