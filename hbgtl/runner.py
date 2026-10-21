"""One run of the paper-trading bot (called every ~30 minutes during market hours).

Order of work, every run:
  1. record deposits, open the day's ledger
  2. reconcile: confirm any pending order with the broker (accounting uses confirmed fills only)
  3. safety: any broker position we don't know about -> kill switch + alert
  4. manage the open position (exits)
  5. look for a new entry (Portfolio A only; Portfolio B can't afford one yet)
The risk engine has the final say on every entry. Nothing here can trade live.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import state as st
from .risk import AccountState, OptionQuote, RiskEngine, loss_alert_needed
from .selection import rank
from .strategy import evaluate, exit_reason, market_calm

MULT = 100


def _weekdays_between(a: date, b: date) -> int:
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _log(state: dict, now: datetime, msg: str) -> None:
    state["log"].append(f"{now:%Y-%m-%d %H:%M} {msg}")


def run(gw, cfg: dict, state: dict, now: datetime, mode: str = "trade", persist=lambda: None) -> list[str]:
    out: list[str] = []
    today = now.date()
    vp = cfg["vault"]["vault_pct"]
    fee = float(cfg["costs"]["fee_per_contract"])
    tcfg = cfg["trading"]
    d = st.day(state, today)
    alerts: list[str] = d.setdefault("alerts", [])

    for msg in st.apply_monthly_deposits(state, cfg, today):
        out.append("Deposit recorded: " + msg)
        if msg.startswith("B"):
            d["deposits_b"] = d.get("deposits_b", 0) + cfg["portfolios"]["B"]["monthly_deposit"]
    if d["start_equity"] is None:
        d["start_equity"] = {k: st.ledger(state, k, vp).trading_equity for k in state["portfolios"]}

    is_open, _ = gw.clock()
    if not is_open and mode == "trade":
        out.append("Market closed: no orders this run.")
        _log(state, now, "market closed")
        return out

    # ---------- 2. reconcile pending order ----------
    pend = state.get("pending")
    if pend and mode == "trade":
        o = gw.get_order(pend["client_order_id"])
        if o is None:
            out.append(f"Pending order {pend['client_order_id']} never reached the broker: cleared.")
            state["pending"] = None
        elif o["status"] == "filled":
            px = o["filled_avg_price"]
            if pend["side"] == "buy":
                state["position"] = {**pend["meta"], "qty": o["filled_qty"], "entry_price": px,
                                     "opened": today.isoformat()}
                out.append(f"Entry filled: {pend['symbol']} x{o['filled_qty']} @ ${px:.2f}")
            else:
                pos = state["position"]
                gross = (px - pos["entry_price"]) * MULT * pos["qty"]
                fees = 2 * fee * pos["qty"]
                net = round(gross - fees, 2)
                before = st.ledger(state, "A", vp)
                equity_before = before.trading_equity
                state["portfolios"]["A"]["events"].append(
                    {"kind": "trade", "ref": pend["client_order_id"], "net_pnl": net, "fees": fees,
                     "date": today.isoformat()})
                vault_added = st.ledger(state, "A", vp).vault_balance - before.vault_balance
                d["vault_added"] = round(d["vault_added"] + vault_added, 2)
                state["trades"].append({"date": today.isoformat(), "symbol": pos["symbol"], "underlying": pos["underlying"],
                                        "strategy": pos["strategy"], "qty": pos["qty"], "entry": pos["entry_price"],
                                        "exit": px, "net_pnl": net, "reason": pend["meta"].get("reason", ""),
                                        "opened": pos["opened"], "signal": pos.get("signal", ""),
                                        "strike": pos.get("strike"), "delta": pos.get("delta"),
                                        "expiration": pos.get("expiration"), "dte_at_entry": pos.get("dte"),
                                        "approved_max_loss": pos.get("approved_max_loss")})
                if net > 0:
                    d["wins"] += 1
                    state["consecutive_losses"] = 0
                else:
                    d["losses"] += 1
                    state["consecutive_losses"] += 1
                    alerts += loss_alert_needed(-net, pos["approved_max_loss"], equity_before,
                                                cfg["alerts"]["single_trade_loss_pct"])
                state["position"] = None
                out.append(f"Exit filled: {pos['symbol']} @ ${px:.2f}, net {net:+.2f}")
            state["pending"] = None
        elif o["status"] in ("canceled", "expired", "rejected", "done_for_day"):
            out.append(f"Order {pend['client_order_id']} ended unfilled ({o['status']}).")
            state["pending"] = None
        else:
            pend["checks"] = pend.get("checks", 0) + 1
            if pend["checks"] >= tcfg["cancel_unfilled_after_runs"]:
                gw.cancel(pend["client_order_id"])
                out.append(f"Order {pend['client_order_id']} unfilled after {pend['checks']} checks: canceled.")
                state["pending"] = None
            else:
                out.append(f"Order {pend['client_order_id']} still working ({o['status']}).")

    # ---------- 3. safety reconciliation ----------
    if mode == "trade":
        broker = gw.positions()
        known = {state["position"]["symbol"]} if state.get("position") else set()
        unknown = {s: q for s, q in broker.items() if s not in known and len(s) > 10}  # option symbols only
        if unknown:
            state["kill_switch"] = True
            d["violations"] += 1
            alerts.append(f"Unexpected broker position(s) {sorted(unknown)}: kill switch ON, trading paused")

    # ---------- 4. manage open position ----------
    pos = state.get("position")
    if pos and not state.get("pending") and mode == "trade":
        q = gw.option_quote(pos["symbol"])
        if q and q[0] > 0:
            bid, ask, _ = q
            mid = (bid + ask) / 2
            dte = (date.fromisoformat(pos["expiration"]) - today).days
            held = _weekdays_between(date.fromisoformat(pos["opened"]), today)
            pos["last_mid"] = round(mid, 2)
            why = exit_reason(pos["entry_price"], mid, held, dte, tcfg)
            if why:
                state["order_seq"] = state.get("order_seq", 0) + 1
                cid = f"A-sell-{pos['symbol']}-{today}-{state['order_seq']}"
                state["pending"] = {"client_order_id": cid, "side": "sell", "symbol": pos["symbol"],
                                    "meta": {"reason": why}}
                persist()                          # reserve before sending: a crash can't double-send
                gw.submit_limit(cid, pos["symbol"], "sell", pos["qty"], max(0.01, bid))
                out.append(f"Exit sent for {pos['symbol']}: {why}")
        else:
            out.append(f"No usable quote for {pos['symbol']}: holding, will retry.")

    # ---------- 5. new entry (Portfolio A) ----------
    hm = now.hour * 60 + now.minute
    start_m = int(tcfg["entry_start"][:2]) * 60 + int(tcfg["entry_start"][3:])
    end_m = int(tcfg["entry_end"][:2]) * 60 + int(tcfg["entry_end"][3:])
    entered_today = any(t["opened"] == today.isoformat() for t in state["trades"]) or \
        (state.get("position") or {}).get("opened") == today.isoformat()
    can_enter = (not state.get("position") and not state.get("pending") and not entered_today
                 and tcfg["enabled"] and not state.get("kill_switch"))
    if mode == "scan" or (can_enter and start_m <= hm <= end_m):
        out += _find_entry(gw, cfg, state, d, now, mode, persist)
    elif state.get("kill_switch"):
        out.append("Kill switch is ON: no new trades.")

    if True:  # Portfolio B: report its status every run
        b = st.ledger(state, "B", vp)
        d["b_note"] = (f"Founder's Account ${b.total_project_wealth:,.2f}: the 1% rule allows "
                       f"${b.trading_equity * cfg['risk']['max_risk_per_trade_pct'] / 100:,.2f} of risk per trade, "
                       "below any qualifying contract, so it holds cash by design.")
    _log(state, now, "; ".join(out) or "no action")
    return out


def _find_entry(gw, cfg, state, d, now, mode, persist) -> list[str]:
    out = []
    tcfg, vp = cfg["trading"], cfg["vault"]["vault_pct"]
    today = now.date()
    universe = tcfg["universe"]
    bars = gw.daily_bars(sorted(set(universe + ["SPY"])))
    calm, why = market_calm(bars["SPY"][0], tcfg["max_spy_realized_vol"])
    out.append(("Volatility filter OK: " if calm else "Volatility filter BLOCKS new trades: ") + why)
    d["market"] = {"calm": calm, "vol_note": why}
    signals = d.setdefault("signals", {})        # symbol -> {direction, reason, outcome}; once per symbol per day
    if not calm:
        return out
    a = st.ledger(state, "A", vp)
    acct = AccountState(equity=a.trading_equity, start_of_day_equity=d["start_equity"]["A"],
                        open_positions=1 if state.get("position") else 0,
                        consecutive_losses=state["consecutive_losses"], kill_switch=state.get("kill_switch", False))
    engine = RiskEngine(cfg, cfg["mode"])
    for sym in universe:
        closes, vols = bars.get(sym, ([], []))
        sig = evaluate(sym, closes, vols, tcfg)
        if not sig.direction:
            continue
        out.append(f"Signal {sym}: {sig.direction} ({sig.reason})")
        first_today = sym not in signals
        signals.setdefault(sym, {"direction": sig.direction, "reason": sig.reason, "price": closes[-1], "outcome": "checking"})
        cands = rank(gw.option_candidates(sym, sig.direction, closes[-1], today,
                                          tcfg["contracts"]["dte_min"], tcfg["contracts"]["dte_max"]),
                     tcfg, closes[-1])[: tcfg["contracts"]["max_checked"]]
        if not cands:
            if first_today:          # count each declined setup once a day, not once per 30-minute run
                d["rejected"] += 1
                d["rejections"]["no contract in delta/expiry range"] = d["rejections"].get("no contract in delta/expiry range", 0) + 1
            signals[sym]["outcome"] = "declined: no contract in the delta and expiration range"
            continue
        reasons = []
        for c in cands:
            qt = getattr(c, "quote_time", None) or now
            oq = OptionQuote(c.symbol, c.bid, c.ask, c.open_interest, c.dte, qt, c.strategy)
            # scan mode runs off-hours, so judge staleness against the quote's own time;
            # real trading always uses the actual clock.
            dec = engine.check(oq, acct, qt if mode == "scan" else now)
            if not dec.approved:
                reasons.append(dec.reasons[0].split(":")[0].split("(")[0].strip())
                continue
            if mode == "scan":
                out.append(f"[scan only] Would buy {dec.contracts} x {c.symbol} near ${c.mid:.2f}, max loss ${dec.max_loss:.2f}")
                return out
            state["order_seq"] = state.get("order_seq", 0) + 1
            cid = f"A-buy-{c.symbol}-{today}-{state['order_seq']}"
            limit = min(c.ask, round(c.mid + 0.01, 2))
            state["pending"] = {"client_order_id": cid, "side": "buy", "symbol": c.symbol, "meta": {
                "symbol": c.symbol, "underlying": sym, "strategy": c.strategy, "expiration": c.expiration.isoformat(),
                "approved_max_loss": dec.max_loss, "signal": sig.reason, "strike": c.strike, "delta": c.delta,
                "dte": c.dte, "underlying_price": closes[-1]}}
            signals[sym]["outcome"] = f"bought {dec.contracts} × {c.symbol}"
            persist()
            gw.submit_limit(cid, c.symbol, "buy", dec.contracts, limit)
            out.append(f"Entry sent: buy {dec.contracts} x {c.symbol} limit ${limit:.2f} (max loss ${dec.max_loss:.2f})")
            return out
        top = max(set(reasons), key=reasons.count) if reasons else "unknown"
        if first_today:
            d["rejected"] += 1
            d["rejections"][top] = d["rejections"].get(top, 0) + 1
        signals[sym]["outcome"] = f"declined by the risk engine: {top}"
        out.append(f"Risk engine declined all {len(cands)} {sym} contracts (mostly: {top}).")
    if len(out) == 1:
        out.append("No setups met the entry rules today: staying in cash.")
    return out
