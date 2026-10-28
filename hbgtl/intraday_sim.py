"""Trade simulation shared by the backtest and the live shadow book (identical rules in both places)."""
from __future__ import annotations

import pandas as pd

from .setups import FLATTEN_M, Signal, invalidated


def simulate_trade(s: Signal, d: pd.DataFrame, slip: float, chase_r: float = 0.25, session_over: bool = True):
    """d = completed bars from the signal bar (row 0) onward. Next-bar entry with a chase cap, stop first on
    ambiguous bars, 2R target needs a trade-through, invalidation exits at the next bar's open.
    Returns None (no fill), {"open": True, ...} (still running, live only) or a closed-trade dict."""
    if len(d) < 2:
        return None if session_over else {"open": True, "filled": False}
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
    target = entry + 2.0 * risk
    for j in range(1, len(d)):
        r = d.iloc[j]
        if r["l"] <= s.stop:
            px = min(r["o"], s.stop) * (1 - slip) if j > 1 else s.stop * (1 - slip)
            return _res(s, entry, px, risk, "stop", int(r["m"]))
        if r["h"] >= target + 0.01:
            return _res(s, entry, target, risk, "target", int(r["m"]))
        why = invalidated(s.setup, d, j, entry)
        if why:
            if j + 1 < len(d):
                return _res(s, entry, d.iloc[j + 1]["o"] * (1 - slip), risk, why.split(":")[0], int(r["m"]) + 1)
            if session_over or int(r["m"]) >= FLATTEN_M:
                return _res(s, entry, r["c"] * (1 - slip), risk, why.split(":")[0], int(r["m"]))
            return {"open": True, "filled": True, "entry": entry, "risk": risk}
    if not session_over:
        return {"open": True, "filled": True, "entry": entry, "risk": risk}
    last = d.iloc[-1]
    return _res(s, entry, last["c"] * (1 - slip), risk, "session end", int(last["m"]))


def _res(s, entry, exit_px, risk, how, exit_m):
    return {"setup": s.setup, "symbol": s.symbol, "m": s.m, "entry": round(entry, 4), "exit": round(exit_px, 4),
            "risk": round(risk, 4), "R": round((exit_px - entry) / risk, 4), "how": how, "mins": exit_m - s.m,
            "rvol": s.rvol, "stop": s.stop, "exit_m": exit_m}
