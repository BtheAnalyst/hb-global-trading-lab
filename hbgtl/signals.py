"""Momentum Atelier signals: shared by the backtest and the live paper bot (one source of truth).

Decisions use completed daily closes only. Pure functions over a DataFrame of closes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "GLD", "VNQ"]   # predeclared, unleveraged


@dataclass(frozen=True)
class MomentumParams:
    universe: tuple = tuple(DEFAULT_UNIVERSE)
    lookbacks: tuple = (63, 126, 252)   # blended 3/6/12-month total return
    sma: int = 200                      # trend filter: must close above its 200-day average
    exit_rank: int = 3                  # keep holding while ranked within top-3
    slots: int = 2                      # enter only from the top-2

    @property
    def warmup(self) -> int:
        return max(max(self.lookbacks), self.sma) + 1


class Momentum:
    """Precomputes scores and trend filters for every day in `closes` (columns = symbols)."""

    def __init__(self, closes: pd.DataFrame, p: MomentumParams = MomentumParams()):
        self.p = p
        u = [s for s in p.universe if s in closes.columns]
        self.universe = u
        self.closes = closes[u]
        self.sma = self.closes.rolling(p.sma).mean()
        self.score = sum(self.closes / self.closes.shift(n) - 1 for n in p.lookbacks) / len(p.lookbacks)

    def ranked(self, i: int) -> list[str]:
        """Eligible ETFs on day i (close above SMA, score known), strongest first."""
        row, px, sma = self.score.iloc[i], self.closes.iloc[i], self.sma.iloc[i]
        elig = [s for s in self.universe if not np.isnan(row[s]) and not np.isnan(sma[s]) and px[s] > sma[s]]
        return sorted(elig, key=lambda s: row[s], reverse=True)

    def picks(self, i: int, held: list[str]) -> list[str]:
        """Holdings for the next session: keep held ETFs still eligible and in the top `exit_rank`,
        fill empty slots from the top `slots`. Never more than `slots` names."""
        ranked = self.ranked(i)
        keep = [s for s in held if s in ranked[: self.p.exit_rank]]
        for s in ranked:
            if len(keep) >= self.p.slots:
                break
            if s not in keep and s in ranked[: self.p.slots]:
                keep.append(s)
        return keep[: self.p.slots]

    def explain(self, i: int) -> dict:
        """Human-readable reasoning for the letters and journal."""
        row, px, sma = self.score.iloc[i], self.closes.iloc[i], self.sma.iloc[i]
        return {s: {"score": None if np.isnan(row[s]) else round(float(row[s]), 4),
                    "close": round(float(px[s]), 4),
                    "sma": None if np.isnan(sma[s]) else round(float(sma[s]), 4),
                    "above_trend": bool(not np.isnan(sma[s]) and px[s] > sma[s])}
                for s in self.universe}
