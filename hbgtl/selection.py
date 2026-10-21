"""Options Specialist (rules-only version): pick candidate contracts for a signal."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class Candidate:
    symbol: str          # OCC option symbol
    underlying: str
    strategy: str        # long_call | long_put
    strike: float
    expiration: date
    dte: int
    open_interest: int
    bid: float
    ask: float
    delta: float | None

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2, 2)

    @property
    def spread_pct(self) -> float:
        m = (self.bid + self.ask) / 2
        return 100 * (self.ask - self.bid) / m if m > 0 else float("inf")


def rank(cands: list[Candidate], cfg: dict, underlying_price: float) -> list[Candidate]:
    """Keep contracts in the delta band (or, without greeks, near the money), then
    prefer delta closest to the target and tighter spreads. Risk checks happen later."""
    c = cfg["contracts"]
    lo, hi, target = c["delta_min"], c["delta_max"], c["delta_target"]
    kept = []
    for x in cands:
        if x.bid <= 0 or x.ask <= 0:
            continue
        if x.delta is not None:
            d = abs(x.delta)
            if not lo <= d <= hi:
                continue
            score = abs(d - target)
        else:
            m = (x.strike - underlying_price) / underlying_price
            m = m if x.strategy == "long_call" else -m
            if not -0.02 <= m <= 0.05:         # at the money to 5% out of the money
                continue
            score = 0.5 + abs(m)
        kept.append((score, x.spread_pct, x))
    kept.sort(key=lambda t: (t[0], t[1]))
    return [x for _, _, x in kept]
