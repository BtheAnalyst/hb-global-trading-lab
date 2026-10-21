"""Performance measurements that don't rely on account balance alone."""
from __future__ import annotations

from datetime import date


def xirr(cash_flows: list[tuple[date, float]], final_value: float, final_date: date) -> float | None:
    """Money-weighted annual return. Deposits are negative (money in), final value positive.

    Adjusts for deposit timing, so $100 added in month 11 isn't credited with a year of growth.
    Returns None if it can't be solved.
    """
    flows = [(d, -abs(a)) for d, a in cash_flows] + [(final_date, final_value)]
    t0 = min(d for d, _ in flows)

    def npv(r: float) -> float:
        return sum(a / (1 + r) ** ((d - t0).days / 365) for d, a in flows)

    lo, hi = -0.9999, 100.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def max_drawdown(values: list[float]) -> float:
    """Largest peak-to-trough fall, as a fraction (0.25 = 25%). Feed it wealth NET of deposits
    (e.g. wealth - contributions + starting capital) so new money doesn't hide a drawdown."""
    peak, worst = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def trade_stats(pnls: list[float]) -> dict:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": n,
        "win_rate": len(wins) / n if n else 0.0,
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0),
        "expectancy": sum(pnls) / n if n else 0.0,
    }
