"""End-of-day report: progress first, but every number is accurate and losses are never hidden."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DayStats:
    date: str
    mode: str
    portfolio: str
    start_equity: float          # trading equity at the open
    end_equity: float            # trading equity at the close
    deposits_today: float
    vault_balance: float
    new_vault_allocation: float
    total_contributions: float
    wins: int
    losses: int
    rule_violations: int
    high_water_mark: float
    next_milestone: float
    council_note: str
    trades_rejected: int = 0
    system_status: str = "All systems normal"


def money(x: float) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def signed(x: float) -> str:
    return f"+{money(x)}" if x >= 0 else money(x)


def end_of_day_report(s: DayStats) -> str:
    trades = s.wins + s.losses
    # Deposits are removed so new money can never look like a trading gain.
    # Vault sweeps move money out of the trading account but are part of today's profit, so add them back.
    day_pnl = s.end_equity - s.start_equity - s.deposits_today + s.new_vault_allocation
    wealth = s.end_equity + s.vault_balance
    total_gain = wealth - s.total_contributions
    drawdown = max(0.0, s.high_water_mark - s.end_equity)
    base = s.start_equity + s.deposits_today

    highlights = []
    if s.wins:
        highlights.append(f"{s.wins} profitable trade{'s' if s.wins > 1 else ''} completed.")
    if s.new_vault_allocation > 0:
        highlights.append(f"Profit Vault received {money(s.new_vault_allocation)}.")
    if s.deposits_today > 0:
        highlights.append(f"Monthly contribution of {money(s.deposits_today)} received (not counted as profit).")
    if trades == 0:
        highlights.append("No trade met every rule today, so the system stayed in cash on purpose. "
                          "That protects capital but is not a gain.")
    if trades and s.rule_violations == 0:
        highlights.append("Every trade followed the risk rules.")

    risk_line = ("Within approved limits" if s.rule_violations == 0
                 else f"{s.rule_violations} RULE VIOLATION(S): trading paused, see alerts")

    lines = [
        f"HB GLOBAL TRADING LAB — DAILY REPORT ({s.portfolio})",
        f"{s.date} · {s.mode.upper()} MODE",
        "",
        "Today's highlights:",
        *[f"• {h}" for h in highlights],
        "",
        f"Profit Vault: {money(s.vault_balance)}",
        f"Next milestone: {money(s.next_milestone)} "
        f"({min(100, 100 * wealth / s.next_milestone):.0f}% of the way, deposits included)",
        "",
        "Financial snapshot:",
        f"Trading account: {money(s.start_equity)} → {money(s.end_equity)}",
        f"Combined project wealth: {money(wealth)}",
        f"Total contributed: {money(s.total_contributions)}",
        f"Today's trading P&L: {signed(day_pnl)} ({100 * day_pnl / base:+.2f}%)" if base else
        f"Today's trading P&L: {signed(day_pnl)}",
        f"Net gain vs. money contributed: {signed(total_gain)}",
        f"Trades: {trades} ({s.wins} won, {s.losses} lost"
        + (f", {100 * s.wins / trades:.0f}% win rate)" if trades else ")")
        + f" · {s.trades_rejected} rejected by risk rules",
        f"Current drawdown from peak: {money(drawdown)}",
        "",
        f"Council insight: {s.council_note}",
        f"Risk status: {risk_line}",
        f"System: {s.system_status}",
    ]
    return "\n".join(lines)
