"""Per-portfolio books for the stock/ETF engine. Plain JSON-serializable state, dollars as floats
rounded only for display. One Ledger per portfolio; all portfolios share nothing but prices.

Tracks separately (Gate 2 directive item 6):
  contributions | trading equity | settled vs unsettled cash (T+1) | positions + average-cost basis |
  realized P&L and its high-water mark | Profit Vault (protected cash) and vault owed |
  prior losses still to recover | estimated tax reserve | contribution-adjusted wealth high (unit NAV) |
  sleeve unit NAV and its high (for the circuit breaker) | losing-exit streak.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Ledger:
    pid: str
    positions: dict = field(default_factory=dict)        # sym -> {"qty": float, "basis": float}
    settled: float = 0.0
    unsettled: list = field(default_factory=list)        # [[trade_date_iso, amount], ...]
    contributed: float = 0.0
    deposits: list = field(default_factory=list)         # [[date_iso, amount], ...]
    realized: float = 0.0
    hwm_realized: float = 0.0
    vault: float = 0.0                                   # protected cash, never traded
    owed_vault: float = 0.0                              # earned by the vault, paid from settled cash
    yr_realized: dict = field(default_factory=dict)      # "2026" -> net realized
    costs: float = 0.0
    traded: float = 0.0
    trades: int = 0
    units: float = 0.0                                   # unit NAV: contribution-adjusted (time-weighted)
    nav_hwm: float = 0.0
    sleeve_nav_v: float = 1.0                            # sleeve time-weighted index (flows removed)
    sleeve_hwm: float = 1.0
    sleeve_last_value: float = 0.0
    sleeve_flow: float = 0.0                             # net dollars moved into the sleeve since the last mark
    sleeve_held: list = field(default_factory=list)
    losing_exits: int = 0
    flags: dict = field(default_factory=dict)            # breaker / kill / daily_block_until / alerts
    order_ids: list = field(default_factory=list)        # every client_order_id ever sent (dup guard)
    pending: list = field(default_factory=list)          # broker orders sent but not final
    last_pass: str | None = None                         # ISO date of the last decision pass
    last_close_nav: dict = field(default_factory=dict)   # date_iso -> unit NAV at that close (last 10)
    month_credited: str | None = None                    # "YYYY-MM" of the last credited deposit

    # ---------- serialization ----------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Ledger":
        return cls(**d)

    # ---------- cash ----------
    def qty(self, s: str) -> float:
        return self.positions.get(s, {}).get("qty", 0.0)

    def settle(self, today: str) -> float:
        """T+1: proceeds from any earlier trade date become settled (runs happen only on trading days)."""
        moved = sum(a for d, a in self.unsettled if d < today)
        self.unsettled = [[d, a] for d, a in self.unsettled if d >= today]
        self.settled += moved
        return moved

    def unsettled_total(self) -> float:
        return sum(a for _, a in self.unsettled)

    def deposit(self, day: str, amount: float, prev_value: float | None) -> None:
        """Credit a contribution. Units are issued at the prior value so deposits never count as gains."""
        if amount <= 0:
            return
        nav = prev_value / self.units if self.units > 0 and prev_value and prev_value > 0 else 1.0
        self.units += amount / nav
        self.settled += amount
        self.contributed += amount
        self.deposits.append([day, amount])
        self.month_credited = day[:7]

    def pay_vault(self) -> float:
        """Move vault money owed from settled cash only; never forces a sale."""
        x = min(self.owed_vault, max(0.0, self.settled))
        if x > 0:
            self.settled -= x; self.vault += x; self.owed_vault -= x
        return x

    def free_cash(self) -> float:
        return max(0.0, self.settled - self.owed_vault)

    # ---------- fills ----------
    def apply_buy(self, day: str, s: str, dollars: float, qty: float, cost: float, sleeve: bool,
                  sleeve_nav: float | None = None) -> None:
        p = self.positions.setdefault(s, {"qty": 0.0, "basis": 0.0})
        p["qty"] += qty; p["basis"] += dollars
        self.settled -= dollars
        self.costs += cost; self.traded += dollars; self.trades += 1
        if sleeve:
            self.sleeve_flow += dollars

    def apply_sell(self, day: str, s: str, proceeds: float, qty: float, cost: float, sleeve: bool,
                   vault_pct: float, is_exit: bool) -> float:
        """Returns realized P&L of this sale. proceeds = cash received (qty x fill price, net of fees);
        cost = estimated slippage/spread inside that price (reported, already reflected in proceeds)."""
        p = self.positions[s]
        frac = min(1.0, qty / p["qty"]) if p["qty"] > 0 else 1.0
        cost_out = p["basis"] * frac
        pnl = proceeds - cost_out
        p["qty"] -= qty; p["basis"] -= cost_out
        if p["qty"] < 1e-9 or frac >= 0.999999:
            self.positions.pop(s, None)
        self.unsettled.append([day, proceeds])
        self.realized += pnl
        y = day[:4]
        self.yr_realized[y] = self.yr_realized.get(y, 0.0) + pnl
        self.costs += cost; self.traded += proceeds + cost; self.trades += 1
        if self.realized > self.hwm_realized:                 # only gains above the prior high feed the vault
            self.owed_vault += (self.realized - self.hwm_realized) * vault_pct
            self.hwm_realized = self.realized
        if sleeve:
            self.sleeve_flow -= proceeds
        if is_exit:
            self.losing_exits = self.losing_exits + 1 if pnl <= 0 else 0
        return pnl

    # ---------- marks ----------
    def values(self, px: dict) -> dict:
        return {s: p["qty"] * px[s] for s, p in self.positions.items()}

    def equity(self, px: dict) -> float:
        """Trading equity: holdings + settled + unsettled cash (vault already removed; owed vault is inside)."""
        return sum(self.values(px).values()) + self.settled + self.unsettled_total()

    def total(self, px: dict) -> float:
        return self.equity(px) + self.vault

    def nav(self, px: dict) -> float:
        return self.total(px) / self.units if self.units > 0 else 1.0

    def sleeve_value(self, px: dict, sleeve_syms: set) -> float:
        return sum(v for s, v in self.values(px).items() if s in sleeve_syms)

    def mark_sleeve(self, px: dict, sleeve_syms: set) -> float:
        """Advance the sleeve's time-weighted index to this close: return = (value - net inflows) / last value."""
        v = self.sleeve_value(px, sleeve_syms)
        if self.sleeve_last_value > 1e-9:
            self.sleeve_nav_v *= (v - self.sleeve_flow) / self.sleeve_last_value
        self.sleeve_last_value, self.sleeve_flow = v, 0.0
        self.sleeve_hwm = max(self.sleeve_hwm, self.sleeve_nav_v)
        return self.sleeve_nav_v

    def sleeve_drawdown(self) -> float:
        return 1 - self.sleeve_nav_v / self.sleeve_hwm if self.sleeve_hwm > 0 else 0.0

    def report(self, px: dict, tax_rate: float = 0.24) -> dict:
        eq, tot = self.equity(px), self.total(px)
        return {"pid": self.pid, "contributed": round(self.contributed, 2), "trading_equity": round(eq, 2),
                "protected_vault": round(self.vault, 2), "vault_owed": round(self.owed_vault, 2),
                "total_wealth": round(tot, 2), "net_gain": round(tot - self.contributed, 2),
                "realized": round(self.realized, 2), "unrealized": round(
                    sum(v - self.positions[s]["basis"] for s, v in self.values(px).items()), 2),
                "prior_losses_to_recover": round(max(0.0, self.hwm_realized - self.realized), 2),
                "tax_reserve_est": round(sum(max(0.0, v) for v in self.yr_realized.values()) * tax_rate, 2),
                "settled_cash": round(self.settled, 2), "unsettled_cash": round(self.unsettled_total(), 2),
                "nav": round(self.nav(px), 6), "nav_high": round(self.nav_hwm, 6),
                "drawdown_from_high": round(1 - self.nav(px) / self.nav_hwm, 4) if self.nav_hwm else 0.0,
                "costs": round(self.costs, 2), "trades": self.trades,
                "holdings": {s: {"qty": round(p["qty"], 9), "value": round(p["qty"] * px[s], 2),
                                 "basis": round(p["basis"], 2)} for s, p in sorted(self.positions.items())},
                "sleeve": list(self.sleeve_held), "sleeve_drawdown": round(self.sleeve_drawdown(), 4),
                "losing_exit_streak": self.losing_exits, "flags": dict(self.flags)}
