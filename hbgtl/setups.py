"""Long-only intraday setups. Each one is a pure function of COMPLETED bars 0..i of one session.

A signal decided at the close of bar i is entered at the open of bar i+1 (backtest) or with a marketable
limit capped near the signal close (live). Every setup returns the same fields so results are measured
separately per strategy id.

  ORB15_V1        Setup A  opening-range (first 15 min) breakout
  PULLBACK_VWAP_V1 Setup B trend pullback to VWAP with bullish confirmation
  MOMO_CONT_V1    Setup C  momentum, tight consolidation, continuation breakout

Parameters are fixed in advance (config/intraday.yaml); they were NOT fitted to the backtest data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .candles import five_minute

DEFAULTS = {
    "ORB15_V1": {"start_m": 15, "end_m": 120, "min_rvol": 2.0, "vol_mult": 1.5, "min_or_pct": 0.004,
                 "max_or_pct": 0.04, "min_risk_pct": 0.0025, "max_risk_pct": 0.015, "target_r": 2.0},
    "PULLBACK_VWAP_V1": {"start_m": 30, "end_m": 300, "min_rvol": 1.5, "touch_pct": 0.0015, "lookback": 30,
                         "above_frac": 0.8, "min_risk_pct": 0.0025, "max_risk_pct": 0.015, "target_r": 2.0},
    "MOMO_CONT_V1": {"start_m": 30, "end_m": 270, "min_rvol": 1.5, "min_move_pct": 0.015, "cons_bars": 10,
                     "max_cons_frac": 0.4, "vol_mult": 1.5, "min_risk_pct": 0.0025, "max_risk_pct": 0.015,
                     "target_r": 2.0},
    # Setup D (Ross Cameron-style gap-and-go, first pullback). Universe: gappers >= 10% at the open.
    "GAPGO_PB_V1": {"start_m": 2, "end_m": 60, "min_gap": 0.10, "min_rvol": 5.0, "min_push": 0.03,
                    "max_retrace": 0.5, "pb_max": 5, "min_risk_pct": 0.004, "max_risk_pct": 0.04, "target_r": 2.0},
}
SETUP_IDS = tuple(DEFAULTS)
LAST_ENTRY_M = 330      # no new entries after 15:00 ET
FLATTEN_M = 380         # flatten intraday positions at 15:50 ET


@dataclass
class Signal:
    setup: str
    symbol: str
    m: int                  # minute index of the signal bar (completed)
    ref: float              # signal-bar close (entry reference)
    stop: float
    target: float
    rvol: float | None
    why: str
    conditions: dict = field(default_factory=dict)

    @property
    def risk_ps(self) -> float:
        return self.ref - self.stop


def reason_key(setup: str, reason: str) -> str:
    """Group rejection reasons without their numbers (for counts in reports)."""
    import re
    return f"{setup}: " + re.sub(r"\s+", " ", re.sub(r"[-+]?\$?\d[\d.,]*%?x?", "#", reason.split("(")[0])).strip()


def _risk_ok(ref, stop, p):
    r = (ref - stop) / ref
    if r < p["min_risk_pct"]:
        return f"stop too tight ({r:.2%} < {p['min_risk_pct']:.2%}): noise would stop it out"
    if r > p["max_risk_pct"]:
        return f"stop too wide ({r:.2%} > {p['max_risk_pct']:.2%})"
    return None


def orb15(d: pd.DataFrame, i: int, sym: str, rv: float | None, p: dict, state: dict) -> Signal | str | None:
    row = d.iloc[i]
    m = int(row["m"])
    if m < p["start_m"] or m > min(p["end_m"], LAST_ENTRY_M):
        return None
    orb = d[d["m"] < 15]
    if len(orb) < 12:                           # data-quality: most opening minutes must exist
        return "opening range incomplete (missing minutes)"
    orh, orl = float(orb["h"].max()), float(orb["l"].min())
    if state.get("orb_broken"):                 # first breakout only
        return None
    if not (row["c"] > orh):
        return None
    state["orb_broken"] = True
    orw = (orh - orl) / orh
    why = []
    if not (p["min_or_pct"] <= orw <= p["max_or_pct"]):
        return f"opening range {orw:.2%} outside {p['min_or_pct']:.1%}-{p['max_or_pct']:.0%}"
    if rv is None or rv < p["min_rvol"]:
        return f"relative volume {rv if rv is None else round(rv, 2)} below {p['min_rvol']}"
    if not (row["v"] >= p["vol_mult"] * (row["vavg"] or np.inf)):
        return "breakout bar volume not confirmed"
    if not (row["c"] > row["vwap"]):
        return "breakout below VWAP"
    if not (row["close_pos"] >= 0.5):
        return "breakout bar closed weak (upper wick)"
    stop = (orh + orl) / 2
    stop = max(stop, row["c"] - 1.5 * (orh - orl))
    bad = _risk_ok(row["c"], stop, p)
    if bad:
        return bad
    why = f"close {row['c']:.2f} > OR high {orh:.2f}, RVOL {rv:.1f}, bar vol {row['v'] / row['vavg']:.1f}x, above VWAP"
    return Signal("ORB15_V1", sym, m, float(row["c"]), float(stop), float(row["c"] + p["target_r"] * (row["c"] - stop)),
                  rv, why, {"or_high": orh, "or_low": orl, "or_pct": round(orw, 4), "vwap": float(row["vwap"])})


def pullback_vwap(d, i, sym, rv, p, state):
    row = d.iloc[i]
    m = int(row["m"])
    if m < p["start_m"] or m > min(p["end_m"], LAST_ENTRY_M) or i < p["lookback"]:
        return None
    w = d.iloc[i - p["lookback"]: i + 1]
    if (w["c"] > w["vwap"]).mean() < p["above_frac"] or not (row["ema20"] > row["vwap"]):
        return None                                                # no established uptrend
    five = five_minute(d, i)                                       # completed 5-minute candles only
    if len(five) < 6:
        return None
    hl = five["l"].iloc[-3:].min() > five["l"].iloc[-6:-3].min()    # higher low
    hh = five["h"].iloc[-4:].max() >= five["h"].iloc[-6:].max()      # recent high is the session-window high
    if not (hl and hh):
        return None
    recent = d.iloc[i - 3: i + 1]
    touched = (recent["l"] <= recent["vwap"] * (1 + p["touch_pct"])).any()
    if not touched:
        return None
    if (recent["c"] < recent["vwap"] * (1 - p["touch_pct"])).any():
        return None                                                # closed through VWAP: trend weakening
    confirm = bool(row["bull_engulf"] or row["hammer"] or (row["bull"] and row["close_above_prev_high"]))
    if not confirm or state.get("pb_last", -99) >= i - 10:
        return None
    state["pb_last"] = i
    if rv is None or rv < p["min_rvol"]:
        return f"relative volume {rv if rv is None else round(rv, 2)} below {p['min_rvol']}"
    stop = float(recent["l"].min()) - 0.01
    bad = _risk_ok(row["c"], stop, p)
    if bad:
        return bad
    pat = "bullish engulfing" if row["bull_engulf"] else "hammer" if row["hammer"] else "close above prior high"
    why = f"uptrend above VWAP, pullback touched VWAP {row['vwap']:.2f}, {pat}, RVOL {rv:.1f}"
    return Signal("PULLBACK_VWAP_V1", sym, m, float(row["c"]), stop,
                  float(row["c"] + p["target_r"] * (row["c"] - stop)), rv, why,
                  {"vwap": float(row["vwap"]), "pattern": pat})


def momo_cont(d, i, sym, rv, p, state):
    row = d.iloc[i]
    m = int(row["m"])
    n = p["cons_bars"]
    if m < p["start_m"] or m > min(p["end_m"], LAST_ENTRY_M) or i < n + 15:
        return None
    day_open = float(d["o"].iloc[0])
    cons = d.iloc[i - n: i]                                       # consolidation = the n bars BEFORE the trigger
    ch, cl = float(cons["h"].max()), float(cons["l"].min())
    move = ch / day_open - 1
    if move < p["min_move_pct"] or not (row["c"] > ch):
        return None
    if state.get("mc_last", -99) >= i - n:
        return None
    hod_before = float(d["h"].iloc[: i - n].max())
    height = max(hod_before, ch) - day_open
    if (ch - cl) > p["max_cons_frac"] * height or cl < float(cons["vwap"].iloc[-1]):
        return None                                               # not a tight base above VWAP
    state["mc_last"] = i
    if rv is None or rv < p["min_rvol"]:
        return f"relative volume {rv if rv is None else round(rv, 2)} below {p['min_rvol']}"
    if not (row["v"] >= p["vol_mult"] * cons["v"].mean()):
        return "continuation breakout volume not confirmed"
    if row["close_pos"] < 0.5:
        return "breakout bar closed weak"
    stop = cl - 0.01
    bad = _risk_ok(row["c"], stop, p)
    if bad:
        return bad
    why = f"up {move:.1%} from open, {n}-bar base {cl:.2f}-{ch:.2f} above VWAP, breakout close {row['c']:.2f}, RVOL {rv:.1f}"
    return Signal("MOMO_CONT_V1", sym, m, float(row["c"]), stop, float(row["c"] + p["target_r"] * (row["c"] - stop)),
                  rv, why, {"base_high": ch, "base_low": cl, "move": round(move, 4)})


def gapgo_pb(d, i, sym, rv, p, state):
    """First pullback after an opening push on a stock that gapped up >= 10% (news/float not checked: the data
    feed has neither, so relative volume >= 5 stands in for 'in play')."""
    row = d.iloc[i]
    m = int(row["m"])
    if m < p["start_m"] or m > min(p["end_m"], LAST_ENTRY_M) or i < 3 or state.get("gg_done"):
        return None
    if (state.get("gap") or 0) < p["min_gap"]:
        return None
    day_open = float(d["o"].iloc[0])
    prior = d.iloc[:i]
    hod_i = int(prior["h"].values.argmax())
    hod = float(prior["h"].iloc[hod_i])
    if hod < day_open * (1 + p["min_push"]):
        return None                                               # no opening push yet
    pb = d.iloc[hod_i + 1: i]                                     # bars after the high, before the trigger
    if not (1 <= len(pb) <= p["pb_max"]) or not (~pb["bull"]).any():
        return None
    pb_low = float(min(pb["l"].min(), d["l"].iloc[i]))
    if pb_low < hod - p["max_retrace"] * (hod - day_open) or pb_low <= float(row["vwap"]):
        return None                                               # pulled back too deep / lost VWAP
    if not (row["bull"] and row["c"] > d["h"].iloc[i - 1]):
        return None                                               # first candle to make a new high
    state["gg_done"] = True
    if rv is None or rv < p["min_rvol"]:
        return f"relative volume {rv if rv is None else round(rv, 1)} below {p['min_rvol']}"
    stop = pb_low - 0.01
    bad = _risk_ok(row["c"], stop, p)
    if bad:
        return bad
    why = (f"gap {state['gap']:.0%}, push to {hod:.2f}, {len(pb)}-bar pullback held {pb_low:.2f} above VWAP, "
           f"new-high candle {row['c']:.2f}, RVOL {rv:.1f}")
    return Signal("GAPGO_PB_V1", sym, m, float(row["c"]), stop, float(row["c"] + p["target_r"] * (row["c"] - stop)),
                  rv, why, {"gap": round(state["gap"], 4), "hod": hod, "pb_low": pb_low, "vwap": float(row["vwap"])})


FUNCS = {"GAPGO_PB_V1": gapgo_pb, "ORB15_V1": orb15, "PULLBACK_VWAP_V1": pullback_vwap, "MOMO_CONT_V1": momo_cont}


def evaluate(setup: str, d, i, sym, rv, params: dict | None, state: dict):
    p = {**DEFAULTS[setup], **(params or {})}
    sub = state.setdefault(setup, {})
    if "gap" in state:                       # symbol-level context (today's opening gap) shared by all setups
        sub["gap"] = state["gap"]
    return FUNCS[setup](d, i, sym, rv, p, sub)


def invalidated(setup: str, d, i, entry: float) -> str | None:
    """Exit-early rule checked on each completed bar while in a trade (stop/target are bracket legs)."""
    row = d.iloc[i]
    if int(row["m"]) >= FLATTEN_M:
        return "end of day flatten (15:50 ET)"
    if (setup.startswith("ORB15") or setup in ("MOMO_CONT_V1", "GAPGO_PB_V1")) and row["c"] < row["vwap"]:
        return "closed below VWAP: momentum failed"
    if setup == "PULLBACK_VWAP_V1" and row["bear_engulf"] and row["c"] < row["vwap"]:
        return "bearish engulfing below VWAP: trend weakening"
    return None
