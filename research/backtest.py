"""Backtest long-only stock/ETF strategies for a $100 start + $100/month account.

Rules of the simulation (kept deliberately conservative):
  * Deposit $100 on the first trading day of each month (the first deposit is the $100 start).
  * Signals use the CLOSE; orders fill at the NEXT day's OPEN (no look-ahead).
  * Fractional shares; minimum order $1; cost = 0.05% slippage/spread per trade side, $0 commission.
  * Cash-account discipline: sale proceeds are usable only the next trading day (T+1).
  * Long only. No margin, no shorting, no leverage. Uninvested cash earns nothing.
Prices are dividend-adjusted daily bars (Alpaca SIP), 2016-01 to 2026-10.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
COST = 0.0005
DEPOSIT = 100.0


def load(symbols):
    o, c = {}, {}
    for s in symbols:
        df = pd.read_csv(DATA / f"{s}.csv", parse_dates=["date"]).set_index("date")
        o[s], c[s] = df["open"], df["close"]
    return pd.DataFrame(o).dropna(), pd.DataFrame(c).dropna()


def rsi2(close: pd.Series, n: int = 2) -> pd.Series:
    d = close.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + ru / rd.replace(0, np.nan))


# ---------------- strategies: return target weights (dict) on a rebalance day, else None ----------------

class Strategy:
    name = ""
    daily = False          # True: evaluated every day; False: at month end only

    def targets(self, i, C, held) -> dict | None:
        raise NotImplementedError


class BuyHold(Strategy):
    def __init__(self, sym):
        self.sym, self.name = sym, f"Buy & hold {sym} (benchmark)"

    def targets(self, i, C, held):
        return {self.sym: 1.0}


class TrendFilter(Strategy):
    """Hold the index while it closes above its 200-day average; otherwise park in T-bills (BIL)."""
    def __init__(self, sym="SPY", n=200):
        self.sym, self.n, self.name = sym, n, f"Trend filter {sym} > {n}-day avg, else BIL"

    def targets(self, i, C, held):
        px = C[self.sym]
        if i < self.n:
            return {"BIL": 1.0}
        return {self.sym: 1.0} if px.iloc[i] > px.iloc[i - self.n + 1:i + 1].mean() else {"BIL": 1.0}


class DualMomentum(Strategy):
    """Pick the stronger of US (SPY) vs international (VEA) over 12 months; if it lags T-bills, hold IEF."""
    name = "Dual momentum SPY/VEA, else IEF"

    def targets(self, i, C, held):
        lb = 252
        if i < lb:
            return {"BIL": 1.0}
        r = {s: C[s].iloc[i] / C[s].iloc[i - lb] - 1 for s in ("SPY", "VEA", "BIL")}
        best = max(("SPY", "VEA"), key=lambda s: r[s])
        return {best: 1.0} if r[best] > r["BIL"] else {"IEF": 1.0}


class RotateTop2(Strategy):
    """Each month, hold the 2 strongest ETFs by 6-month return that are above their 200-day average."""
    U = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "VNQ"]
    name = "Momentum rotation, top 2 of 8 ETFs (trend-filtered)"

    def targets(self, i, C, held):
        if i < 200:
            return {"BIL": 1.0}
        score = {}
        for s in self.U:
            px = C[s]
            if px.iloc[i] > px.iloc[i - 199:i + 1].mean():
                score[s] = px.iloc[i] / px.iloc[i - 126] - 1
        picks = sorted(score, key=score.get, reverse=True)[:2]
        w = {s: 0.5 for s in picks}
        if len(picks) < 2:
            w["BIL"] = w.get("BIL", 0) + 0.5 * (2 - len(picks))
        return w


class SwingPullback(Strategy):
    """Daily: buy an ETF in a long-term uptrend after a sharp 2-day dip (RSI(2) < 10); sell on the bounce
    (close above 5-day average), after 10 days, or on an 8% stop (checked at the close, so gaps can
    make the real loss bigger). Up to 3 positions of 1/3 each; idle cash stays in cash."""
    U = ["SPY", "QQQ", "IWM", "EFA", "GLD", "TLT"]
    name = "Swing pullback (RSI-2 dips in uptrending ETFs)"
    daily = True

    def __init__(self):
        self.entry = {}

    def prepare(self, C):
        self.r2 = {s: rsi2(C[s]) for s in self.U}
        self.s200 = {s: C[s].rolling(200).mean() for s in self.U}
        self.s5 = {s: C[s].rolling(5).mean() for s in self.U}

    def targets(self, i, C, held):
        w = {s: 1 / 3 for s in held if s in self.U}
        changed = False
        for s in list(w):
            px = C[s].iloc[i]
            e_px, e_i = self.entry.get(s, (px, i))
            if px > self.s5[s].iloc[i] or i - e_i >= 10 or px <= e_px * 0.92:
                del w[s]; self.entry.pop(s, None); changed = True
        for s in self.U:
            if len(w) >= 3:
                break
            if s not in w and C[s].iloc[i] > (self.s200[s].iloc[i] or math.inf) and self.r2[s].iloc[i] < 10:
                w[s] = 1 / 3; self.entry[s] = (C[s].iloc[i], i); changed = True
        return w if changed else None


# ---------------- engine ----------------

def simulate(strat: Strategy, O: pd.DataFrame, C: pd.DataFrame, start: int = 0, end: int | None = None) -> dict:
    end = len(C) if end is None else end
    dates = C.index
    if hasattr(strat, "prepare"):
        strat.prepare(C)
    if hasattr(strat, "entry"):
        strat.entry = {}
    sh = {s: 0.0 for s in C.columns}
    settled, unsettled = 0.0, 0.0
    contributed, trades, costs = 0.0, 0, 0.0
    target, pending = None, None
    nav, units, flows = [], 0.0, []
    last_month = None
    for i in range(start, end):
        d = dates[i]
        settled += unsettled; unsettled = 0.0                      # T+1: yesterday's sale proceeds settle
        # deposits
        if d.month != last_month:
            settled += DEPOSIT; contributed += DEPOSIT; flows.append((d, DEPOSIT)); last_month = d.month
            value_before = sum(sh[s] * O[s].iloc[i] for s in sh) + settled - DEPOSIT
            units += DEPOSIT / (value_before / units) if units > 0 and value_before > 0 else DEPOSIT
            if target is None and not strat.daily:
                pending = strat.targets(max(i - 1, 0), C, {s for s in sh if sh[s] > 0}) if i > 0 else None
        # execute yesterday's decision at today's open: sells first (proceeds settle tomorrow), then buys
        if pending is not None:
            target = pending; pending = None
        if target is not None:
            px = O.iloc[i]
            total = sum(sh[s] * px[s] for s in sh) + settled + unsettled
            for s in sh:
                want = target.get(s, 0.0) * total
                have = sh[s] * px[s]
                if have - want > 1.0:
                    sell = have - want if target.get(s, 0) > 0 else have
                    sh[s] -= sell / px[s]
                    if target.get(s, 0) == 0:
                        sh[s] = 0.0
                    unsettled += sell * (1 - COST); costs += sell * COST; trades += 1
            for s, wgt in target.items():
                want = wgt * total
                have = sh[s] * px[s]
                buy = min(want - have, settled)
                if buy > 1.0:
                    sh[s] += buy * (1 - COST) / px[s]; settled -= buy; costs += buy * COST; trades += 1
        # end of day: mark, then decide for tomorrow
        value = sum(sh[s] * C[s].iloc[i] for s in sh) + settled + unsettled
        nav.append((d, value / units if units else 1.0, value))
        nxt_month = dates[i + 1].month if i + 1 < len(dates) else None
        if strat.daily:
            pending = strat.targets(i, C, {s for s in sh if sh[s] > 1e-9})
        elif nxt_month != d.month:                                    # month end: rebalance next open
            pending = strat.targets(i, C, {s for s in sh if sh[s] > 1e-9})
        if pending is None and target is not None and not strat.daily:
            pass
        if pending is None and strat.daily and settled > 1 and target:   # invest new deposits into holdings
            pending = target
    df = pd.DataFrame(nav, columns=["date", "nav", "value"]).set_index("date")
    peak = df["nav"].cummax()
    mdd = float(((peak - df["nav"]) / peak).max())
    return {"final": float(df["value"].iloc[-1]), "contributed": contributed, "trades": trades, "costs": costs,
            "max_dd": mdd, "xirr": xirr(flows, float(df["value"].iloc[-1]), dates[end - 1]), "series": df}


def xirr(flows, final, end_date):
    fl = [(d, -a) for d, a in flows] + [(end_date, final)]
    t0 = fl[0][0]

    def npv(r):
        return sum(a / (1 + r) ** ((d - t0).days / 365) for d, a in fl)
    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        return float("nan")
    for _ in range(200):
        m = (lo + hi) / 2
        lo, hi = (lo, m) if npv(lo) * npv(m) <= 0 else (m, hi)
    return (lo + hi) / 2


def one_year_windows(strat_factory, O, C):
    """Every start month: a fresh $100 account, $100/month, held 12 months. Returns final values."""
    month_starts = [i for i in range(1, len(C)) if C.index[i].month != C.index[i - 1].month]
    res = []
    for k, s in enumerate(month_starts):
        if k + 12 >= len(month_starts):
            break
        e = month_starts[k + 12]
        r = simulate(strat_factory(), O, C, start=max(s - 260, 0) if False else s, end=e)
        res.append((C.index[s].date(), r["final"], r["contributed"]))
    return res


if __name__ == "__main__":
    syms = ["SPY", "VTI", "QQQ", "IWM", "EFA", "EEM", "VEA", "TLT", "IEF", "BIL", "GLD", "VNQ"]
    O, C = load(syms)
    print(f"Data: {C.index[0].date()} to {C.index[-1].date()} ({len(C)} trading days)\n")
    factories = [lambda: BuyHold("VTI"), lambda: BuyHold("SPY"), lambda: TrendFilter("SPY"),
                 lambda: DualMomentum(), lambda: RotateTop2(), lambda: SwingPullback()]
    rows = []
    for f in factories:
        s = f()
        r = simulate(s, O, C)
        rows.append((s.name, r))
        print(f"{s.name:58s} final ${r['final']:>9,.0f} on ${r['contributed']:,.0f} in | "
              f"money-weighted {r['xirr']:6.1%}/yr | max drop {r['max_dd']:5.1%} | trades {r['trades']:4d} | costs ${r['costs']:.2f}")
    print("\n12-month windows ($100 start + $100/month, 12 months, every start month; needs 1 year of warm-up data):")
    # warm-up: windows start after 2017 so 200/252-day signals exist; simulate on full history slices
    for f in factories:
        s = f()
        month_starts = [i for i in range(1, len(C)) if C.index[i].month != C.index[i - 1].month]
        finals = []
        for k, st in enumerate(month_starts):
            if st < 260 or k + 12 >= len(month_starts):
                continue
            e = month_starts[k + 12]
            # run the strategy's signals with full history up to st (they only look back), account starts at st
            r = simulate(f(), O, C, start=st, end=e)
            finals.append(r["final"])
        a = np.array(finals)
        print(f"{s.name:58s} median ${np.median(a):6,.0f} | worst ${a.min():6,.0f} | best ${a.max():6,.0f} | "
              f"below $1,200 in {np.mean(a < 1200):4.0%} of {len(a)} windows")


class CoreSatellite(Strategy):
    """Core: VTI (buy and hold). Satellite: a sleeve run by the rotation strategy. Rebalanced monthly."""
    def __init__(self, core_w=0.8):
        self.core_w, self.sat = core_w, RotateTop2()
        self.name = f"Core {core_w:.0%} VTI + {1 - core_w:.0%} momentum rotation"

    def targets(self, i, C, held):
        w = {"VTI": self.core_w}
        for s, x in (self.sat.targets(i, C, held) or {}).items():
            w[s] = w.get(s, 0) + (1 - self.core_w) * x
        return w
