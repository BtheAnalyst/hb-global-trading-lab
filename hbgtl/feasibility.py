"""Feasibility math + Monte Carlo for the $100 -> $10,000 challenge.

These are HYPOTHETICAL scenarios with assumed win rates and payoffs, not forecasts.
No strategy has been validated yet, so none of these numbers describe our system.
"""
from __future__ import annotations

import numpy as np

TRADING_DAYS_IN_100_CALENDAR_DAYS = 69


def required_daily_growth(multiple: float = 100, days: int = 100) -> float:
    return multiple ** (1 / days) - 1


def simulate(stake_pct: float, win_rate: float, win_return: float = 1.0, loss_return: float = -0.5,
             cost_per_trade_pct_of_premium: float = 0.03, start: float = 100.0,
             days: int = TRADING_DAYS_IN_100_CALENDAR_DAYS, n_paths: int = 100_000,
             min_trade_dollars: float = 5.0, seed: int = 7) -> dict:
    """One trade per trading day. Each trade puts `stake_pct` of equity into premium.

    A win returns `win_return` x premium (+100% default); a loss returns `loss_return`
    (-50% default, i.e. a stop at half the premium). Costs (spread + fees) are charged
    on every trade. A path is 'ruined' when equity falls below `min_trade_dollars`,
    roughly the cheapest liquid contract, so it can no longer trade.
    """
    rng = np.random.default_rng(seed)
    eq = np.full(n_paths, start)
    alive = np.ones(n_paths, bool)
    for _ in range(days):
        wins = rng.random(n_paths) < win_rate
        r = np.where(wins, win_return, loss_return) - cost_per_trade_pct_of_premium
        eq = np.where(alive, eq * (1 + stake_pct * r), eq)
        alive &= eq >= min_trade_dollars
    ev = win_rate * win_return + (1 - win_rate) * loss_return - cost_per_trade_pct_of_premium
    return {
        "stake_pct": stake_pct, "win_rate": win_rate, "edge_per_trade": round(ev, 3),
        "median_end": float(np.median(eq)),
        "p_finish_above_100": float(np.mean(eq > start)),
        "p_1000": float(np.mean(eq >= 1_000)),
        "p_10000": float(np.mean(eq >= 10_000)),
        "p_ruin": float(np.mean(~alive)),
    }


if __name__ == "__main__":
    print(f"Needed: {required_daily_growth():.2%}/calendar day, "
          f"{required_daily_growth(100, TRADING_DAYS_IN_100_CALENDAR_DAYS):.2%}/trading day")
    for wr in (0.33, 0.40, 0.45):
        for stake in (0.01, 0.10, 0.25, 0.50):
            print(simulate(stake, wr))
