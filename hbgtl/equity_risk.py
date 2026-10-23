"""Pre-trade checks, safeguards and reconciliation for the stock/ETF engine.

Thresholds come from config (see docs/RISK_THRESHOLDS.md for the evidence behind each default).
Every refusal returns a plain-English reason that goes into the journal and the letter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .gateway import is_option_symbol

LEVERAGED_OR_INVERSE = {  # well-known tickers; name keywords below catch the rest
    "TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SPXU", "SSO", "SDS", "SH", "PSQ", "QLD", "QID", "UDOW", "SDOW",
    "TNA", "TZA", "SOXL", "SOXS", "LABU", "LABD", "TMF", "TMV", "TBT", "UVXY", "SVXY", "VXX", "NUGT", "DUST",
    "JNUG", "JDST", "FAS", "FAZ", "ERX", "ERY", "YINN", "YANG", "BOIL", "KOLD", "UCO", "SCO", "AGQ", "ZSL",
}
_LEV_NAME = re.compile(r"\b(2x|3x|-1x|-2x|-3x|ultra|ultrapro|inverse|bear|leveraged|daily target)\b", re.I)


@dataclass
class RiskConfig:
    core_cap_margin: float = 0.05        # core hard cap = core target + 5 points
    etf_cap_mult: float = 1.5            # sleeve ETF hard cap = 1.5 x slot target
    defensive_cap_margin: float = 0.05   # BIL cap = whole sleeve weight + 5 points
    daily_loss_pct: float = 0.05         # block next-session sleeve entries
    sleeve_breaker_dd: float = 0.25      # sleeve to BIL, owner resume
    losing_exit_alert: int = 6           # alert only
    losing_exit_pause: int = 0           # 0 = off (Council may enable)
    wealth_alert_dd: float = 0.20        # alert only
    max_spread_pct: float = 0.01
    max_quote_age_s: int = 60
    max_move_pct: float = 0.25           # vs last close: bad-data guard
    recon_tolerance: float = 0.50        # dollars
    min_order: float = 1.0

    @classmethod
    def from_cfg(cls, d: dict | None) -> "RiskConfig":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def asset_allowed(sym: str, allowlist: set, asset: dict | None) -> str | None:
    """None if allowed, else the reason."""
    if is_option_symbol(sym):
        return f"{sym} is an option: options are disabled by mandate"
    if sym not in allowlist:
        return f"{sym} is not on the approved ETF list"
    if sym in LEVERAGED_OR_INVERSE:
        return f"{sym} is a leveraged or inverse fund"
    if asset is not None:
        if _LEV_NAME.search(asset.get("name", "") or ""):
            return f"{sym} looks leveraged or inverse by name ({asset.get('name')})"
        if not asset.get("tradable", True):
            return f"{sym} is not tradable at the broker right now"
        if asset.get("status", "active") != "active":
            return f"{sym} is not active at the broker"
    return None


def quote_problem(sym: str, bid: float, ask: float, last_close: float, age_s: float | None, rc: RiskConfig,
                  check_age: bool) -> str | None:
    if not (bid and ask) or bid <= 0 or ask <= 0 or ask < bid:
        return f"{sym}: no valid quote"
    mid = (bid + ask) / 2
    if (ask - bid) / mid > rc.max_spread_pct:
        return f"{sym}: spread {(ask - bid) / mid:.2%} is wider than {rc.max_spread_pct:.0%}"
    if last_close and abs(mid / last_close - 1) > rc.max_move_pct:
        return f"{sym}: price {mid:.2f} is {mid / last_close - 1:+.0%} from the last close: treated as bad data"
    if check_age and age_s is not None and age_s > rc.max_quote_age_s:
        return f"{sym}: quote is {age_s:.0f}s old"
    return None


def cap_for(sym: str, rule, rc: RiskConfig) -> float:
    if sym == rule.core:
        return min(1.0, rule.core_w + rc.core_cap_margin)
    if sym == rule.defensive:
        return min(1.0, rule.sleeve_w + rc.defensive_cap_margin)
    return rule.sleeve_w / rule.slots * rc.etf_cap_mult


def cap_problem(sym: str, post_value: float, equity: float, rule, rc: RiskConfig) -> str | None:
    if equity <= 0:
        return None
    w, cap = post_value / equity, cap_for(sym, rule, rc)
    if w > cap + 1e-9:
        return f"{sym} would be {w:.1%} of the portfolio, above its {cap:.1%} limit"
    return None


def reconcile(broker_qty: dict, ledger_qty: dict, prices: dict, tol: float) -> list[str]:
    """Compare broker positions with the sum of broker-mode ledgers. Returns problems (empty = clean)."""
    problems = []
    for s in sorted(set(broker_qty) | set(ledger_qty)):
        b, l = broker_qty.get(s, 0.0), ledger_qty.get(s, 0.0)
        if s not in ledger_qty and b:
            problems.append(f"unknown position at broker: {s} qty {b}")
            continue
        diff = abs(b - l) * prices.get(s, 0.0)
        if diff > tol:
            problems.append(f"{s}: broker {b} vs books {l} (${diff:.2f} apart)")
    return problems
