"""365-day feasibility for Portfolio B ($100 start + $100/month).

Everything here is ILLUSTRATIVE MATH under stated assumptions. None of it is a forecast:
no strategy has validated trade data yet.
"""
from __future__ import annotations

import numpy as np

from .risk import max_affordable_premium

MONTHS = 12
DEPOSIT = 100.0
CHECKPOINT_MONTHS = {30: 1, 90: 3, 180: 6, 270: 9, 365: 12}

# Assumed NET monthly returns on trading equity (after fees and AI costs).
SCENARIOS = {
    "Bearish": -0.05,
    "Conservative": 0.0,
    "Moderate": 0.02,
    "Aggressive": 0.06,
    "Exceptional": 0.15,
}


def project(monthly_return: float, months: int = MONTHS, deposit: float = DEPOSIT,
            vault_pct: float = 30.0) -> list[dict]:
    """Deposit at the start of each month (month 1 deposit = the $100 start), returns during
    the month, then the 70/30 sweep on profit above the cash-flow-adjusted high-water mark."""
    equity = vault = hwm = contrib = 0.0
    rows = []
    for m in range(1, months + 1):
        equity += deposit
        hwm += deposit
        contrib += deposit
        equity *= 1 + monthly_return
        eligible = max(0.0, equity - hwm)
        sweep = eligible * vault_pct / 100
        if eligible > 0:
            equity -= sweep
            vault += sweep
            hwm = equity
        rows.append({"month": m, "contributions": contrib, "trading": equity, "vault": vault,
                     "wealth": equity + vault, "gain": equity + vault - contrib})
    return rows


def months_to(target: float, monthly_return: float, cap: int = 600) -> int | None:
    rows = project(monthly_return, months=cap)
    return next((r["month"] for r in rows if r["wealth"] >= target), None)


def required_monthly_return(target: float = 10_000, months: int = MONTHS) -> float:
    lo, hi = 0.0, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if project(mid, months, vault_pct=0)[-1]["wealth"] >= target:
            hi = mid
        else:
            lo = mid
    return hi


def affordability_by_month(risk_pct: float = 1.0, fee: float = 0.05) -> list[dict]:
    """Under the CURRENT risk rule, what's the priciest option Portfolio B could buy each month,
    assuming it never trades (so equity = contributions)?"""
    return [{"month": m, "equity": 100.0 * m, "max_premium": round(max_affordable_premium(100.0 * m, risk_pct, fee), 3)}
            for m in range(1, MONTHS + 1)]


def vault_split_mc(win_rate: float, vault_pcts=(10, 20, 30, 50), stake: float = 0.05,
                   trades: int = 250, n_paths: int = 20_000, seed: int = 11,
                   win_ret: float = 1.0, loss_ret: float = -0.5, cost: float = 0.03) -> list[dict]:
    """HYPOTHETICAL: one trade per trading day, `stake` of trading equity in premium,
    $100 deposited every ~21 trading days. Compares Profit Vault splits on the same random paths.
    Ignores contract affordability on purpose, so it isolates the effect of the split."""
    out = []
    for vp in vault_pcts:
        rng = np.random.default_rng(seed)
        eq = np.full(n_paths, DEPOSIT)
        hwm = eq.copy()
        vault = np.zeros(n_paths)
        contrib = np.full(n_paths, DEPOSIT)
        nav = np.ones(n_paths)          # time-weighted index: deposits don't move it
        peak = nav.copy()
        mdd = np.zeros(n_paths)
        for t in range(1, trades + 1):
            if t % 21 == 0 and t // 21 < MONTHS:
                eq += DEPOSIT; hwm += DEPOSIT; contrib += DEPOSIT
            r = np.where(rng.random(n_paths) < win_rate, win_ret, loss_ret) - cost
            pnl = eq * stake * r
            nav *= 1 + pnl / (eq + vault)
            eq = eq + pnl
            elig = np.maximum(0, eq - hwm)
            sweep = elig * vp / 100
            eq -= sweep; vault += sweep
            hwm = np.where(elig > 0, eq, hwm)
            peak = np.maximum(peak, nav)
            mdd = np.maximum(mdd, (peak - nav) / peak)
        wealth = eq + vault
        out.append({"vault_pct": vp, "median_wealth": float(np.median(wealth)),
                    "p5_wealth": float(np.percentile(wealth, 5)),
                    "p_below_contrib": float(np.mean(wealth < contrib)),
                    "median_max_dd": float(np.median(mdd)),
                    "median_vault": float(np.median(vault))})
    return out


def dca_benchmark(annual_return: float, months: int = MONTHS) -> float:
    """$100/month into a buy-and-hold index at an assumed annual return."""
    m = (1 + annual_return) ** (1 / 12) - 1
    return project(m, months, vault_pct=0)[-1]["wealth"]


if __name__ == "__main__":
    print("required monthly:", round(required_monthly_return(), 4))
    for name, r in SCENARIOS.items():
        rows = project(r)
        cp = {d: rows[m - 1] for d, m in CHECKPOINT_MONTHS.items()}
        print(name, r, {d: (round(v["wealth"]), round(v["vault"])) for d, v in cp.items()},
              "1k:", months_to(1000, r), "5k:", months_to(5000, r), "10k:", months_to(10000, r))
    for a in affordability_by_month():
        print(a)
    for wr in (0.33, 0.40):
        print("win", wr)
        for row in vault_split_mc(wr):
            print({k: round(v, 3) for k, v in row.items()})
    for ar in (0.0, 0.07, 0.10):
        print("dca", ar, round(dca_benchmark(ar), 2))
