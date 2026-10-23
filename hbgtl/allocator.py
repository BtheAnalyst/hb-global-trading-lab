"""Turn a strategy's target mix into orders. Shared by the backtest and the live paper bot.

Trades happen only on:
  * a sleeve signal change (an ETF leaves -> full sale; a slot fills -> excess T-bills sold),
  * new free cash to invest (deposits, settled sale proceeds) -> buys where holdings are short of target,
  * a large core/sleeve drift (core more than `drift_band` above target) -> trim the core.
Never on small daily drift. Never more than free settled cash.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AllocationRule:
    core: str = "VTI"
    core_w: float = 0.8            # 1.0 = 100% core (the benchmark); not hard-coded anywhere else
    defensive: str = "BIL"
    slots: int = 2
    drift_band: float = 0.10
    min_order: float = 1.0

    @property
    def sleeve_w(self) -> float:
        return 1.0 - self.core_w


@dataclass(frozen=True)
class Order:
    side: str        # "buy" | "sell"
    symbol: str
    dollars: float
    sleeve: bool     # True = Momentum Atelier order, False = core
    reason: str


def targets(rule: AllocationRule, equity: float, sleeve_held: list[str]) -> dict[str, float]:
    """Dollar targets for every symbol the strategy should hold."""
    t = {rule.core: rule.core_w * equity}
    if rule.sleeve_w > 0:
        per = rule.sleeve_w * equity / rule.slots
        for s in sleeve_held:
            t[s] = t.get(s, 0.0) + per
        t[rule.defensive] = t.get(rule.defensive, 0.0) + per * (rule.slots - len(sleeve_held))
    return t


def decide(rule: AllocationRule, hold_val: dict[str, float], equity: float, free_cash: float,
           prev_sleeve: list[str], new_sleeve: list[str]) -> list[Order]:
    """Orders for the next session. hold_val = current market value per symbol (0 if none)."""
    hv = lambda s: hold_val.get(s, 0.0)  # noqa: E731
    orders: list[Order] = []
    for s in prev_sleeve:                                     # exits: sell the whole position
        if s not in new_sleeve and hv(s) > rule.min_order:
            orders.append(Order("sell", s, hv(s), True, "left the top ranks or fell below its trend"))
    tgt = targets(rule, equity, new_sleeve)
    if rule.sleeve_w > 0:
        excess = hv(rule.defensive) - tgt.get(rule.defensive, 0.0)
        if excess > max(5.0, 0.01 * equity):
            orders.append(Order("sell", rule.defensive, excess, True, "a sleeve slot filled; T-bills no longer needed"))
        if equity > 0:
            core_dev = hv(rule.core) / equity - rule.core_w
            if core_dev > rule.drift_band:
                orders.append(Order("sell", rule.core, core_dev * equity, False,
                                    f"core drifted {core_dev:.0%} above its target"))
    short = sorted(((tgt[s] - hv(s), s) for s in tgt if tgt[s] - hv(s) > rule.min_order), reverse=True)
    cash = free_cash
    for gap, s in short:
        if cash < rule.min_order:
            break
        amt = min(gap, cash)
        orders.append(Order("buy", s, amt, s != rule.core, "invest cash toward target"))
        cash -= amt
    if cash >= rule.min_order and short:
        orders.append(Order("buy", rule.core, cash, False, "leftover cash to the core"))
    return orders
