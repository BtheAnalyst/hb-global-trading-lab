"""Profit compounding + Protected Profit Vault ledger.

Cash-flow-adjusted high-water mark (HWM):
  * A deposit raises trading equity AND the HWM by the same amount, so a deposit is never "profit".
  * Only realized net gains (after fees) that lift equity ABOVE the HWM are eligible.
  * Recovering a prior loss is not new profit.
  * Eligible profit is split: reinvest % stays in trading, vault % is swept to the vault.
  * Vault money is never available to the trading engine.
All amounts are kept in cents (int) to avoid floating-point drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _c(dollars: float) -> int:
    return int(round(dollars * 100))


def _d(cents: int) -> float:
    return cents / 100


@dataclass
class LedgerEvent:
    kind: str            # "deposit" | "trade"
    ref: str
    amount: float        # deposit amount or net trade P&L (after fees)
    fees: float
    eligible_profit: float
    to_vault: float
    trading_equity: float
    vault_balance: float
    high_water_mark: float


@dataclass
class ProfitVault:
    starting_capital: float
    vault_pct: float = 30.0
    _equity: int = field(init=False)
    _vault: int = field(default=0, init=False)
    _hwm: int = field(init=False)
    _contrib: int = field(init=False)
    _realized: int = field(default=0, init=False)
    _fees: int = field(default=0, init=False)
    events: list[LedgerEvent] = field(default_factory=list, init=False)

    def __post_init__(self):
        if not 0 <= self.vault_pct <= 100:
            raise ValueError("vault_pct must be 0-100")
        if self.starting_capital < 0:
            raise ValueError("starting_capital must be >= 0")
        self._equity = self._hwm = self._contrib = _c(self.starting_capital)

    # ----- cash flows -----
    def deposit(self, amount: float, ref: str = "monthly") -> LedgerEvent:
        if amount <= 0:
            raise ValueError("deposit must be positive")
        c = _c(amount)
        self._equity += c
        self._hwm += c
        self._contrib += c
        return self._log("deposit", ref, amount, 0.0, 0, 0)

    # ----- trades -----
    def record_closed_trade(self, trade_id: str, net_pnl: float, fees: float = 0.0) -> LedgerEvent:
        """net_pnl must already include fees and slippage (broker-confirmed fills)."""
        if net_pnl != net_pnl:  # NaN guard
            raise ValueError("net_pnl is NaN")
        pnl = _c(net_pnl)
        self._equity += pnl
        self._realized += pnl
        self._fees += _c(fees)
        eligible = max(0, self._equity - self._hwm)
        to_vault = round(eligible * self.vault_pct / 100)
        if eligible > 0:
            self._equity -= to_vault
            self._vault += to_vault
            self._hwm = self._equity  # swept profit can't be re-earned and counted twice
        return self._log("trade", trade_id, net_pnl, fees, eligible, to_vault)

    def _log(self, kind, ref, amount, fees, eligible, to_vault) -> LedgerEvent:
        e = LedgerEvent(kind, ref, amount, fees, _d(eligible), _d(to_vault),
                        self.trading_equity, self.vault_balance, self.high_water_mark)
        self.events.append(e)
        return e

    # ----- reporting -----
    @property
    def trading_equity(self) -> float:
        return _d(self._equity)

    @property
    def vault_balance(self) -> float:
        return _d(self._vault)

    @property
    def high_water_mark(self) -> float:
        return _d(self._hwm)

    @property
    def total_contributions(self) -> float:
        return _d(self._contrib)

    @property
    def realized_net_pnl(self) -> float:
        return _d(self._realized)

    @property
    def total_fees(self) -> float:
        return _d(self._fees)

    @property
    def total_project_wealth(self) -> float:
        return _d(self._equity + self._vault)

    @property
    def cumulative_net_gain(self) -> float:
        """Wealth minus everything the owner put in. Deposits are never counted as gains."""
        return _d(self._equity + self._vault - self._contrib)

    @property
    def loss_to_recover(self) -> float:
        return _d(max(0, self._hwm - self._equity))
