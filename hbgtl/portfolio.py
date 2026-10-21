"""Two simulated portfolios, kept strictly apart.

A = $10,000 engineering account (tests the software).
B = $100 start + $100/month (Haley's real funding plan; the feasibility account).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .vault import ProfitVault


@dataclass
class Portfolio:
    key: str
    name: str
    starting_capital: float
    monthly_deposit: float
    deposit_day: int = 1
    vault_pct: float = 30.0
    start_date: date = field(default_factory=date.today)
    ledger: ProfitVault = field(init=False)
    deposits_made: list[date] = field(default_factory=list, init=False)

    def __post_init__(self):
        self.ledger = ProfitVault(self.starting_capital, self.vault_pct)
        self.deposits_made.append(self.start_date)   # the starting capital = month one

    def apply_scheduled_deposits(self, today: date) -> list[date]:
        """Idempotent: running it twice on the same day never double-deposits."""
        if self.monthly_deposit <= 0:
            return []
        new = []
        y, m = self.start_date.year, self.start_date.month
        while True:
            m += 1
            if m > 12:
                y, m = y + 1, 1
            due = date(y, m, min(self.deposit_day, 28))
            if due > today:
                break
            if due not in self.deposits_made:
                self.ledger.deposit(self.monthly_deposit, ref=f"{self.key}-deposit-{due.isoformat()}")
                self.deposits_made.append(due)
                new.append(due)
        return new

    def cash_flows(self) -> list[tuple[date, float]]:
        amt = {self.start_date: self.starting_capital}
        return [(d, amt.get(d, self.monthly_deposit)) for d in self.deposits_made]


def load_portfolios(cfg: dict, start: date) -> dict[str, Portfolio]:
    vp = cfg["vault"]["vault_pct"]
    return {k: Portfolio(k, p["name"], p["starting_capital"], p.get("monthly_deposit", 0),
                         p.get("deposit_day", 1), vp, start)
            for k, p in cfg["portfolios"].items()}
