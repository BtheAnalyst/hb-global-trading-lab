"""Backtest engine v2: Core + Momentum Atelier, long-only stocks/ETFs, small account.

Corrections vs v1 (research/backtest.py):
  1. v1 re-traded toward target weights EVERY day after a rebalance (inflated trades/costs and
     silently became a daily-rebalanced strategy). v2 trades only on: a signal change, new cash to
     invest, or a large drift (core/sleeve more than 10 percentage points off target).
  2. Explicit T+1: sale proceeds become spendable the next trading day; buys use settled cash only.
  3. Profit Vault simulated on REALIZED net gains above a high-water mark of cumulative realized P&L,
     never on deposits or unrealized gains; vault cash is excluded from trading. No forced sales.
  4. Estimated tax reserve tracked (assumption: 24% of each calendar year's positive net realized
     gain), reported separately, not traded.
  5. Both money-weighted (XIRR) and time-weighted (unit NAV) returns; turnover reported.
  6. Accounts start 2017-01 so every 252-day lookback is built only from past data.
Timing: decisions on the CLOSE of day t, orders fill at the OPEN of day t+1. Fractional shares,
$1 minimum order, cost = slippage/spread per trade side (default 0.05%), $0 commission.
Uninvested cash earns nothing (conservative); the defensive asset is BIL (T-bills ETF).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "VNQ"]   # predeclared, unleveraged
CORE, DEFENSIVE = "VTI", "BIL"
ALL = sorted(set(UNIVERSE + [CORE, DEFENSIVE]))


def load(symbols=ALL):
    o, c = {}, {}
    for s in symbols:
        df = pd.read_csv(DATA / f"{s}.csv", parse_dates=["date"]).set_index("date")
        o[s], c[s] = df["open"], df["close"]
    O, C = pd.DataFrame(o).dropna(), pd.DataFrame(c).dropna()
    return O.loc[C.index], C


@dataclass
class Params:
    core_w: float = 0.8
    lookbacks: tuple = (63, 126, 252)     # blended 3/6/12-month momentum (predeclared default)
    sma: int = 200                        # trend filter
    exit_rank: int = 3                    # hold while ranked within top-3 (entry needs top-2)
    slots: int = 2
    cost: float = 0.0005
    drift_band: float = 0.10              # core/sleeve rebalance only past 10pp drift
    min_order: float = 1.0
    vault_pct: float = 0.0                # 0 = full reinvestment; 0.30 = 70/30 policy
    tax_rate: float = 0.24                # estimate only, reported
    deposit: float = 100.0
    extra_deposit_from: str | None = None  # e.g. "2019-01": optional raise scenario
    extra_deposit: float = 0.0


@dataclass
class Result:
    p: Params
    nav: pd.Series
    value: pd.Series
    contributed: float
    final_equity: float
    vault: float
    tax_reserve: float
    realized: float
    trades: int
    traded_dollars: float
    costs: float
    xirr: float
    twr_cagr: float
    max_dd: float
    sleeve_trades: int
    accessible_cash: float
    flows: list = field(default_factory=list)


class Momentum:
    def __init__(self, C: pd.DataFrame, p: Params):
        self.p = p
        self.sma = {s: C[s].rolling(p.sma).mean() for s in UNIVERSE}
        lb = p.lookbacks
        self.score = pd.DataFrame({s: sum(C[s] / C[s].shift(n) - 1 for n in lb) / len(lb) for s in UNIVERSE})
        self.C = C

    def picks(self, i: int, held: list[str]) -> list[str]:
        p, C = self.p, self.C
        row = self.score.iloc[i]
        elig = [s for s in UNIVERSE if not np.isnan(row[s]) and C[s].iloc[i] > self.sma[s].iloc[i]]
        ranked = sorted(elig, key=lambda s: row[s], reverse=True)
        keep = [s for s in held if s in ranked[: p.exit_rank]]
        for s in ranked:
            if len(keep) >= p.slots:
                break
            if s not in keep and s in ranked[: p.slots]:
                keep.append(s)
        return keep[: p.slots]


def run(O: pd.DataFrame, C: pd.DataFrame, p: Params, start: str = "2017-01-01", end: str | None = None) -> Result:
    idx = C.index
    i0 = int(np.searchsorted(idx, pd.Timestamp(start)))
    i1 = len(idx) if end is None else int(np.searchsorted(idx, pd.Timestamp(end), side="right"))
    mom = Momentum(C, p) if p.core_w < 1 else None
    sh = {s: 0.0 for s in ALL}
    basis = {s: 0.0 for s in ALL}               # average-cost basis in dollars
    settled = unsettled = vault = owed_vault = 0.0
    realized = hwm_r = 0.0
    yr_realized: dict[int, float] = {}
    contributed = costs = traded = 0.0
    trades = sleeve_trades = 0
    units, navs, vals, flows = 0.0, [], [], []
    sleeve_held: list[str] = []
    orders: list[tuple] = []                    # decided at close, filled next open
    last_month = None
    for i in range(i0, i1):
        d = idx[i]
        px_o = O.iloc[i]
        settled += unsettled; unsettled = 0.0
        # ---- deposit (first trading day of month) ----
        if d.month != last_month:
            dep = p.deposit + (p.extra_deposit if p.extra_deposit_from and d >= pd.Timestamp(p.extra_deposit_from) else 0.0)
            prev_val = sum(sh[s] * C[s].iloc[i - 1] for s in sh) + settled if i > i0 else 0.0
            units += dep / (prev_val / units) if units > 0 and prev_val > 0 else dep
            settled += dep; contributed += dep; flows.append((d, dep)); last_month = d.month
        # ---- pay any owed vault transfer from settled cash (never force a sale) ----
        if owed_vault > 0 and settled > 0:
            x = min(owed_vault, settled); settled -= x; vault += x; owed_vault -= x
        # ---- execute yesterday's orders at today's open: sells, then buys with settled cash ----
        for side, s, amt, tag in [o for o in orders if o[0] == "sell"] + [o for o in orders if o[0] == "buy"]:
            px = px_o[s]
            if side == "sell":
                amt = min(amt, sh[s] * px)
                if amt < p.min_order:
                    continue
                frac = amt / (sh[s] * px)
                cost_out = basis[s] * frac
                proceeds = amt * (1 - p.cost)
                pnl = proceeds - cost_out
                realized += pnl; yr_realized[d.year] = yr_realized.get(d.year, 0.0) + pnl
                sh[s] -= amt / px; basis[s] -= cost_out
                if sh[s] * px < 0.01:
                    sh[s] = 0.0; basis[s] = 0.0
                unsettled += proceeds; costs += amt * p.cost; traded += amt; trades += 1
                sleeve_trades += tag == "sleeve"
                if realized > hwm_r:                       # qualifying realized gain above prior high
                    gain = realized - hwm_r; hwm_r = realized
                    owed_vault += gain * p.vault_pct
            else:
                amt = min(amt, settled)
                if amt < p.min_order:
                    continue
                sh[s] += amt * (1 - p.cost) / px; basis[s] += amt
                settled -= amt; costs += amt * p.cost; traded += amt; trades += 1
                sleeve_trades += tag == "sleeve"
        orders = []
        # ---- mark at close ----
        px_c = C.iloc[i]
        hold_val = {s: sh[s] * px_c[s] for s in sh}
        equity = sum(hold_val.values()) + settled + unsettled          # trading capital (vault excluded)
        total = equity + vault + owed_vault * 0                         # owed vault is still inside equity
        navs.append(total / units if units else 1.0); vals.append(total)
        if i + 1 >= i1:
            break
        # ---- decide for tomorrow's open ----
        free_cash = max(0.0, settled - owed_vault)       # cash reserved for the vault isn't invested
        if mom is not None:
            new_held = mom.picks(i, [s for s in sleeve_held if hold_val[s] > 0.01] or sleeve_held)
            for s in sleeve_held:                         # exits: full sale of dropped ETFs
                if s not in new_held and hold_val[s] > p.min_order:
                    orders.append(("sell", s, hold_val[s], "sleeve"))
            sleeve_held = new_held
        sleeve_w = 1 - p.core_w
        target = {CORE: p.core_w * equity}
        if sleeve_w > 0:
            per_slot = sleeve_w * equity / p.slots
            for s in sleeve_held:
                target[s] = target.get(s, 0) + per_slot
            target[DEFENSIVE] = target.get(DEFENSIVE, 0) + per_slot * (p.slots - len(sleeve_held))
            # defensive ETF no longer needed (a slot was filled): sell the excess BIL
            if hold_val[DEFENSIVE] - target[DEFENSIVE] > max(5.0, 0.01 * equity):
                orders.append(("sell", DEFENSIVE, hold_val[DEFENSIVE] - target[DEFENSIVE], "sleeve"))
        # big drift between core and sleeve: trim the overweight side (rare)
        core_dev = hold_val[CORE] / equity - p.core_w if equity > 0 else 0
        if sleeve_w > 0 and core_dev > p.drift_band:
            orders.append(("sell", CORE, core_dev * equity, "core"))
        # buys: put free cash where holdings are below target, largest shortfall first
        short = sorted(((target[s] - hold_val[s], s) for s in target if target[s] - hold_val[s] > p.min_order), reverse=True)
        cash_left = free_cash
        for gap, s in short:
            if cash_left < p.min_order:
                break
            amt = min(gap, cash_left)
            orders.append(("buy", s, amt, "core" if s == CORE else "sleeve")); cash_left -= amt
        if cash_left >= p.min_order and short:            # leftover pennies: put into the core
            orders.append(("buy", CORE, cash_left, "core"))
    nav = pd.Series(navs, index=idx[i0:i0 + len(navs)])
    value = pd.Series(vals, index=nav.index)
    peak = nav.cummax()
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    tax_reserve = sum(max(0.0, v) for v in yr_realized.values()) * p.tax_rate
    final_equity = value.iloc[-1] - vault
    return Result(p, nav, value, contributed, final_equity, vault, tax_reserve, realized, trades, traded, costs,
                  xirr(flows, value.iloc[-1], nav.index[-1]), (nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else 0.0,
                  float(((peak - nav) / peak).max()), sleeve_trades, settled + unsettled, flows)


def xirr(flows, final, end_date):
    fl = [(d, -a) for d, a in flows] + [(end_date, final)]
    t0 = fl[0][0]

    def npv(r):
        return sum(a / (1 + r) ** ((d - t0).days / 365.25) for d, a in fl)
    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        return float("nan")
    for _ in range(200):
        m = (lo + hi) / 2
        lo, hi = (lo, m) if npv(lo) * npv(m) <= 0 else (m, hi)
    return (lo + hi) / 2


def regime_returns(nav: pd.Series) -> dict:
    """Time-weighted return of the unit NAV through known unfavorable stretches in the data."""
    windows = {"2018 Q4 sell-off": ("2018-09-20", "2018-12-24"), "2020 COVID crash": ("2020-02-19", "2020-03-23"),
               "2022 bear market": ("2022-01-03", "2022-10-12"), "2025 spring drop": ("2025-02-19", "2025-04-08")}
    out = {}
    for k, (a, b) in windows.items():
        s = nav.loc[a:b]
        if len(s) > 1:
            out[k] = s.iloc[-1] / s.iloc[0] - 1
    return out
