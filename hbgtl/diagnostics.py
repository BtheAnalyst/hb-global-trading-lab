"""Scan-mode diagnostics: explain, contract by contract, why the bot can or can't trade.

Read-only. Never sends orders, never changes risk limits, never touches saved state.
Every rule is judged by the same RiskEngine the live loop uses; this module only
reports what that engine (and the contract filters) decided.
"""
from __future__ import annotations

from datetime import datetime

from .risk import AccountState, OptionQuote, RiskEngine, max_affordable_premium
from .selection import Candidate, rank
from .strategy import evaluate


def _quote_age(c: Candidate, now: datetime) -> float | None:
    qt = getattr(c, "quote_time", None)
    if qt is None or qt.tzinfo is None:
        return None
    return (now - qt).total_seconds()


def contract_line(c: Candidate, engine: RiskEngine, acct: AccountState, now: datetime) -> str:
    """One contract, every reason the risk engine gives, judged against the REAL clock."""
    qt = getattr(c, "quote_time", None) or now
    oq = OptionQuote(c.symbol, c.bid, c.ask, c.open_interest, c.dte, qt, c.strategy)
    dec = engine.check(oq, acct, now)
    age = _quote_age(c, now)
    delta = f"{c.delta:.2f}" if c.delta is not None else "n/a"
    head = (f"{c.symbol} strike {c.strike:g} exp {c.expiration} ({c.dte}d) delta {delta} "
            f"bid/ask {c.bid:.2f}/{c.ask:.2f} spread {c.spread_pct:.1f}% OI {c.open_interest} "
            f"quote age {'n/a' if age is None else f'{age:.0f}s'} | 1 contract risks "
            f"${oq.max_loss_per_contract(engine.fee):,.2f}")
    verdict = "APPROVED" if dec.approved else "REJECTED: " + "; ".join(dec.reasons)
    return f"  {head} -> {verdict}"


def affordability(cands: list[Candidate], cfg: dict, engine: RiskEngine, acct: AccountState,
                  underlying_price: float) -> str:
    """Chain summary for one symbol: how many contracts survive each filter, cheapest that fits."""
    r = cfg["risk"]
    cap = max_affordable_premium(acct.equity, r["max_risk_per_trade_pct"], engine.fee)
    quoted = [c for c in cands if c.bid > 0 and c.ask > 0]
    in_band = rank(quoted, cfg["trading"], underlying_price)
    liquid = [c for c in in_band if c.open_interest >= r["min_open_interest"]
              and c.spread_pct <= r["max_spread_pct_of_mid"]]
    fits = [c for c in liquid if c.ask <= cap]
    liquid_any_delta = [c for c in quoted if c.open_interest >= r["min_open_interest"]
                        and c.spread_pct <= r["max_spread_pct_of_mid"] and c.ask <= cap]
    parts = [f"{len(cands)} contracts in 30-45d window", f"{len(quoted)} with a two-sided quote",
             f"{len(in_band)} in delta band", f"{len(liquid)} also liquid (OI>={r['min_open_interest']}, "
             f"spread<={r['max_spread_pct_of_mid']:g}%)", f"{len(fits)} also affordable (ask <= ${cap:.2f})"]
    line = ", ".join(parts)
    if liquid:
        cheapest = min(liquid, key=lambda c: c.ask)
        line += f"; cheapest liquid in-band ask ${cheapest.ask:.2f} ({cheapest.symbol})"
    if not fits and liquid_any_delta:
        c = min(liquid_any_delta, key=lambda c: abs(abs(c.delta or 0) - cfg['trading']['contracts']['delta_target']))
        line += (f"; outside the delta band, {len(liquid_any_delta)} liquid contract(s) would fit the budget "
                 f"(closest: {c.symbol} delta {c.delta if c.delta is None else round(c.delta, 2)} ask ${c.ask:.2f}) "
                 "- reported only, the delta band is NOT loosened")
    return line


def scan_report(gw, cfg: dict, bars: dict, acct: AccountState, now: datetime) -> list[str]:
    """Per-symbol signal status plus a per-contract and per-chain breakdown."""
    out: list[str] = []
    tcfg = cfg["trading"]
    engine = RiskEngine(cfg, cfg["mode"])
    today = now.date()
    cap = max_affordable_premium(acct.equity, cfg["risk"]["max_risk_per_trade_pct"], engine.fee)
    out.append(f"[diag] Portfolio A trading equity ${acct.equity:,.2f}; 1% risk budget allows an option "
               f"ask of at most ${cap:.2f} per share (${cap * 100:,.2f} per contract incl. fees)")
    for sym in tcfg["universe"]:
        closes, vols = bars.get(sym, ([], []))
        if not closes:
            out.append(f"[diag] {sym}: NO daily bars returned by the data feed")
            continue
        sig = evaluate(sym, closes, vols, tcfg)
        status = sig.direction or "no signal"
        out.append(f"[diag] {sym} ${closes[-1]:.2f} ({len(closes)} bars): {status} - {sig.reason}")
        direction = sig.direction or "long_call"   # chains are probed for every symbol, signal or not
        try:
            cands = gw.option_candidates(sym, direction, closes[-1], today,
                                         tcfg["contracts"]["dte_min"], tcfg["contracts"]["dte_max"])
        except Exception as e:  # report and keep scanning the rest of the universe
            out.append(f"[diag] {sym} {direction} chain: data error {type(e).__name__}: {e}")
            continue
        ages = [a for a in (_quote_age(c, now) for c in cands) if a is not None]
        age_note = (f"; quote ages {min(ages):.0f}s-{max(ages):.0f}s (limit {cfg['risk']['max_quote_age_seconds']}s)"
                    if ages else "")
        out.append(f"[diag] {sym} {direction} chain: " + affordability(cands, cfg, engine, acct, closes[-1]) + age_note)
        if sig.direction:
            for c in rank(cands, tcfg, closes[-1])[: tcfg["contracts"]["max_checked"]]:
                out.append(contract_line(c, engine, acct, now))
    return out
