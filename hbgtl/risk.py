"""Deterministic risk engine.

This is the Chief Risk Officer in code. Nothing (AI council, vote, growth target,
notification preference) can override a rejection from `RiskEngine.check`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

CONTRACT_MULTIPLIER = 100  # one standard US equity option = 100 shares


class LiveTradingBlocked(Exception):
    pass


def assert_paper_or_approved(mode: str) -> None:
    """Live mode needs BOTH the config flag and an environment approval set by the owner."""
    if mode == "paper":
        return
    if mode == "live" and os.environ.get("HBGTL_LIVE_APPROVED") == "yes":
        return
    raise LiveTradingBlocked(f"mode={mode!r} is not permitted without explicit owner approval")


@dataclass
class OptionQuote:
    symbol: str
    bid: float
    ask: float
    open_interest: int
    days_to_expiration: int
    quote_time: datetime
    strategy: str = "long_call"

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return 100 * (self.ask - self.bid) / self.mid if self.mid > 0 else float("inf")

    def max_loss_per_contract(self, fee_per_contract: float = 0.0) -> float:
        """Long option worst case = whole premium paid at the ask + round-trip fees."""
        return self.ask * CONTRACT_MULTIPLIER + 2 * fee_per_contract


@dataclass
class AccountState:
    equity: float                      # trading equity only (never includes the Profit Vault)
    start_of_day_equity: float
    deposits_today: float = 0.0        # excluded from daily P&L so a deposit can't mask a loss
    open_positions: int = 0
    consecutive_losses: int = 0
    kill_switch: bool = False
    trading_day: date = field(default_factory=date.today)

    @property
    def daily_pnl(self) -> float:
        return self.equity - self.start_of_day_equity - self.deposits_today

    @property
    def daily_pnl_pct(self) -> float:
        base = self.start_of_day_equity + self.deposits_today
        return 100 * self.daily_pnl / base if base > 0 else 0.0


@dataclass
class RiskDecision:
    approved: bool
    contracts: int
    max_loss: float
    reasons: list[str]


class RiskEngine:
    def __init__(self, cfg: dict, mode: str):
        self.r = cfg["risk"]
        self.fee = float(cfg.get("costs", {}).get("fee_per_contract", 0.0))
        assert_paper_or_approved(mode)

    def check(self, quote: OptionQuote, acct: AccountState, now: datetime | None = None) -> RiskDecision:
        now = now or datetime.now(timezone.utc)
        reasons: list[str] = []

        if acct.kill_switch:
            reasons.append("kill switch is on")
        if quote.strategy not in self.r["allowed_strategies"]:
            reasons.append(f"strategy {quote.strategy} not allowed")
        if acct.daily_pnl_pct <= -self.r["daily_loss_limit_pct"]:
            reasons.append(f"daily loss limit hit ({acct.daily_pnl_pct:.2f}%)")
        if acct.consecutive_losses >= self.r["max_consecutive_losses"]:
            reasons.append(f"{acct.consecutive_losses} losses in a row: paused for owner review")
        if acct.open_positions >= self.r["max_open_positions"]:
            reasons.append("max open positions reached")
        if quote.quote_time.tzinfo is None:
            reasons.append("quote time has no timezone (treated as untrusted data)")
        else:
            age = (now - quote.quote_time).total_seconds()
            if age > self.r["max_quote_age_seconds"] or age < -5:
                reasons.append(f"stale or future-dated quote ({age:.0f}s)")
        if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            reasons.append("invalid quote")
        elif quote.spread_pct > self.r["max_spread_pct_of_mid"]:
            reasons.append(f"spread too wide ({quote.spread_pct:.1f}% of mid)")
        if quote.open_interest < self.r["min_open_interest"]:
            reasons.append(f"open interest too low ({quote.open_interest})")
        if quote.days_to_expiration < self.r["min_days_to_expiration"]:
            reasons.append(f"too close to expiration ({quote.days_to_expiration}d)")

        risk_budget = max(0.0, acct.equity) * self.r["max_risk_per_trade_pct"] / 100
        per_contract = quote.max_loss_per_contract(self.fee)
        contracts = int(risk_budget // per_contract) if per_contract > 0 else 0
        if contracts < 1:
            reasons.append(
                f"unaffordable: 1 contract risks ${per_contract:,.2f}, budget is ${risk_budget:,.2f}"
            )

        approved = not reasons
        return RiskDecision(approved, contracts if approved else 0,
                            round(contracts * per_contract, 2) if approved else 0.0, reasons)


def max_affordable_premium(equity: float, risk_pct: float, fee_per_contract: float = 0.0) -> float:
    """Highest option price (per share) one contract can have and stay within the risk cap."""
    return max(0.0, (equity * risk_pct / 100 - 2 * fee_per_contract) / CONTRACT_MULTIPLIER)


def loss_alert_needed(trade_loss: float, approved_max_loss: float, equity_before: float,
                      alert_pct: float) -> list[str]:
    """Urgent-alert rules for a single closed trade. Ordinary losses return []."""
    alerts = []
    if trade_loss > approved_max_loss + 0.01:
        alerts.append("loss exceeded the approved maximum: possible execution error")
    if equity_before > 0 and 100 * trade_loss / equity_before >= alert_pct:
        alerts.append(f"single trade lost {100 * trade_loss / equity_before:.2f}% of equity")
    return alerts
