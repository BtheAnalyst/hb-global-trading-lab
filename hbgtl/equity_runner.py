"""Stock/ETF engine: one decision pass per portfolio per trading day.

Runs any number of independent paper portfolios (strategy x vault policy x contribution schedule) side by
side. Each portfolio is either
  * "shadow": fills simulated against quotes (no orders leave the bot), or
  * "broker": orders sent to the Alpaca PAPER account (only one broker portfolio, reconciled every pass).
Scheduled execution stays disabled until Gate 5; this module only runs when called.

Order of a pass (live):  settle T+1 -> credit the month's contribution (on the books only) -> pay vault owed
  -> mark at the last completed close -> safeguards -> signals -> allocator -> risk checks -> persist -> send.
parity=True reproduces research/backtest_v2.py timing exactly (decide on the prior close's books, before
that morning's settlement and deposit), so the live code can be checked against the backtest.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .allocator import AllocationRule, decide
from .equity_ledger import Ledger
from .equity_risk import RiskConfig, asset_allowed, cap_problem, quote_problem, reconcile
from .signals import Momentum, MomentumParams

ROOT = Path(__file__).resolve().parent.parent
KILL_FILE = ROOT / "KILL_SWITCH"


@dataclass
class PortfolioSpec:
    pid: str
    rule: AllocationRule
    mom: MomentumParams
    vault_pct: float = 0.0
    start: float = 100.0
    monthly: float = 100.0
    increases: tuple = ()          # (("2027-06", 150.0), ...): monthly amount from that month on
    mode: str = "shadow"           # shadow | broker
    enabled: bool = True
    tax_rate: float = 0.24
    label: str = ""

    def contribution(self, month: str, first: bool) -> float:
        if first:
            return self.start
        amt = self.monthly
        for frm, m in self.increases:
            if month >= frm:
                amt = m
        return amt


def specs_from_cfg(cfg: dict) -> list[PortfolioSpec]:
    """Build portfolio specs. Only the ONE portfolio named in `broker_portfolio` may use mode: broker."""
    d = cfg.get("defaults", {})
    out = []
    selected = cfg.get("broker_portfolio")
    for pid, p in cfg["portfolios"].items():
        q = {**d, **(p or {})}
        mom = MomentumParams(universe=tuple(q.get("universe", MomentumParams().universe)),
                             lookbacks=tuple(q.get("lookbacks", (63, 126, 252))), sma=q.get("sma", 200),
                             exit_rank=q.get("exit_rank", 3), slots=q.get("slots", 2))
        rule = AllocationRule(core=q.get("core", "VTI"), core_w=float(q["core_weight"]),
                              defensive=q.get("defensive", "BIL"), slots=mom.slots,
                              drift_band=q.get("drift_band", 0.10), min_order=q.get("min_order", 1.0))
        out.append(PortfolioSpec(pid, rule, mom, float(q.get("vault_pct", 0.0)), float(q.get("start", 100)),
                                 float(q.get("monthly", 100)),
                                 tuple((str(i["from"]), float(i["monthly"])) for i in q.get("increases", []) or []),
                                 q.get("mode", "shadow"), bool(q.get("enabled", True)),
                                 float(q.get("tax_rate", 0.24)), q.get("label", pid)))
        if out[-1].mode == "broker" and pid != selected:
            raise ValueError(f"{pid} is set to broker mode but broker_portfolio is {selected!r}: refusing to start")
    if sum(s.mode == "broker" for s in out) > 1:
        raise ValueError("more than one broker-mode portfolio configured: only one may send paper orders")
    return out


class Engine:
    """Holds the shared signal caches; call run_day once per trading day."""

    def __init__(self, specs: list[PortfolioSpec], rc: RiskConfig, journal: Callable[[dict], None] | None = None,
                 save: Callable[[], None] | None = None):
        if sum(s.mode == "broker" and s.enabled for s in specs) > 1:
            raise ValueError("only one portfolio may send orders to the paper broker")
        self.specs = specs
        self.rc = rc
        self.journal = journal or (lambda e: None)
        self.save = save or (lambda: None)
        self._mom: dict = {}

    def momentum(self, closes: pd.DataFrame, p: MomentumParams) -> Momentum:
        key = (id(closes), p)
        if key not in self._mom:
            self._mom = {k: v for k, v in self._mom.items() if k[0] == id(closes)}
            self._mom[key] = Momentum(closes, p)
        return self._mom[key]

    # ------------------------------------------------------------------
    def run_day(self, state: dict, closes: pd.DataFrame, i: int, today: str, gateways: dict,
                parity: bool = False, safeguards: bool = True) -> dict:
        """closes: completed daily closes (dividend-adjusted); row i is the last completed session.
        gateways: {"shadow": SimGateway, "broker": AlpacaEquityGateway | None}."""
        books = state.setdefault("portfolios", {})
        summary = {"date": today, "portfolios": {}, "alerts": []}
        kill = bool(state.get("kill_switch")) or KILL_FILE.exists()
        broker = gateways.get("broker")
        bspec = next((s for s in self.specs if s.mode == "broker" and s.enabled), None)
        if broker is not None and bspec is not None:
            led = Ledger.from_dict(books[bspec.pid]) if bspec.pid in books else Ledger(bspec.pid)
            pxc = {s: float(v) for s, v in closes.iloc[i].items() if not np.isnan(v)}
            rec = {"orders": [], "notes": [], "alerts": [], "refused": []}
            self._recover(led, broker, bspec, pxc, rec, today)        # adopt fills from any earlier/failed run
            books[bspec.pid] = led.to_dict()
            summary["recovery"] = rec
            summary["alerts"] += [f"{bspec.label}: {a}" for a in rec["alerts"]]
            kill = self._reconcile(state, broker, pxc, summary) or kill
        for spec in self.specs:
            if not spec.enabled:
                continue
            led = Ledger.from_dict(books[spec.pid]) if spec.pid in books else Ledger(spec.pid)
            gw = broker if spec.mode == "broker" else gateways["shadow"]
            if spec.mode == "broker" and broker is None:
                continue

            def persist(led=led, pid=spec.pid):
                books[pid] = led.to_dict()
                self.save()
            res = self._run_portfolio(spec, led, closes, i, today, gw, kill, parity, safeguards, persist)
            books[spec.pid] = led.to_dict()
            summary["portfolios"][spec.pid] = res
            summary["alerts"] += [f"{spec.label}: {a}" for a in res["alerts"]]
        state["last_run"] = today
        self.save()
        return summary

    # ------------------------------------------------------------------
    def _run_portfolio(self, spec, led: Ledger, closes, i, today, gw, kill, parity, safeguards,
                       persist=None) -> dict:
        persist = persist or self.save
        out = {"orders": [], "refused": [], "alerts": [], "notes": []}
        if led.last_pass == today:
            out["notes"].append("already ran today: no second decision pass")
            return out
        px = {s: float(v) for s, v in closes.iloc[i].items() if not np.isnan(v)}
        sleeve_syms = set(spec.mom.universe) | {spec.rule.defensive}
        rule, rc = spec.rule, self.rc

        def snapshot():
            hv = {s: v for s, v in led.values(px).items()}
            return hv, led.equity(px), led.free_cash()

        if parity:
            hold_val, equity, free_cash = snapshot()
        prev_total = led.total(px) if led.units > 0 else None
        led.settle(today)
        month = today[:7]
        if led.month_credited != month:
            amt = spec.contribution(month, first=led.contributed == 0)
            led.deposit(today, amt, prev_total)
            out["notes"].append(f"credited the ${amt:,.2f} monthly contribution (on the books; no real transfer)")
        led.pay_vault()
        if not parity:
            hold_val, equity, free_cash = snapshot()

        # ---- marks and highs at the last completed close ----
        nav = led.nav(px)
        close_day = str(closes.index[i].date())
        led.last_close_nav[close_day] = nav
        led.last_close_nav = dict(sorted(led.last_close_nav.items())[-10:])
        led.nav_hwm = max(led.nav_hwm, nav)
        led.mark_sleeve(px, sleeve_syms)

        # ---- data completeness: hold if any universe close is missing ----
        missing = [s for s in [rule.core, rule.defensive, *spec.mom.universe] if s not in px]
        data_ok = not missing
        if missing:
            out["alerts"].append(f"missing prices for {missing}: holding, no sleeve changes today")

        # ---- safeguards ----
        block_entries = False
        if safeguards:
            navs = list(led.last_close_nav.values())
            if (rule.sleeve_w > 0 and len(navs) >= 2 and navs[-2] > 0
                    and navs[-1] / navs[-2] - 1 <= -rc.daily_loss_pct):
                block_entries = True
                out["alerts"].append(f"daily safeguard: down {navs[-1] / navs[-2] - 1:.1%} at the last close, "
                                     "so no new sleeve entries today (nothing sold because of it)")
            sdd = led.sleeve_drawdown()
            if rule.sleeve_w > 0 and sdd >= rc.sleeve_breaker_dd and not led.flags.get("breaker"):
                led.flags["breaker"] = {"since": today, "dd": round(sdd, 4)}
                out["alerts"].append(f"sleeve circuit breaker: sleeve is {sdd:.0%} below its high; "
                                     "moving the sleeve to T-bills until the owner reviews it")
            if (rc.losing_exit_alert and led.losing_exits >= rc.losing_exit_alert
                    and led.flags.get("streak_alerted") != led.losing_exits):
                led.flags["streak_alerted"] = led.losing_exits
                out["alerts"].append(f"{led.losing_exits} losing sleeve exits in a row: owner review suggested")
            elif led.losing_exits < rc.losing_exit_alert:
                led.flags.pop("streak_alerted", None)
            if rc.losing_exit_pause and led.losing_exits >= rc.losing_exit_pause:
                block_entries = True
            # Total-account drawdown on the unit NAV: contributions buy units at the prior value, so a deposit
            # is never counted as a gain or as recovering a loss.
            dd = 1 - nav / led.nav_hwm if led.nav_hwm else 0
            if dd >= rc.wealth_alert_dd and not led.flags.get("wealth_alert"):
                led.flags["wealth_alert"] = today
                out["alerts"].append(f"total wealth is {dd:.0%} below its contribution-adjusted high")
            elif dd < rc.wealth_alert_dd / 2:
                led.flags.pop("wealth_alert", None)
            if (rc.wealth_pause_dd and dd >= rc.wealth_pause_dd and not led.flags.get("wealth_pause")
                    and not led.flags.get("wealth_pause_resumed")):
                led.flags["wealth_pause"] = {"since": today, "dd": round(dd, 4)}
                out["alerts"].append(f"account drawdown {dd:.0%} reached the {rc.wealth_pause_dd:.0%} limit: ALL new "
                                     "purchases paused until the founder approves a restart. Nothing is sold because of it")
            if led.flags.get("wealth_pause_resumed") and dd < rc.wealth_pause_dd / 2:
                led.flags.pop("wealth_pause_resumed", None)     # re-armed once the account recovers to within 10%

        # ---- signals ----
        prev = list(led.sleeve_held)
        new = prev
        if rule.sleeve_w > 0 and data_ok:
            mom = self.momentum(closes, spec.mom)
            held = [s for s in prev if hold_val.get(s, 0) > 0.01] or prev
            new = mom.picks(i, held)
            if led.flags.get("breaker"):
                new = []
            elif block_entries:
                new = [s for s in new if s in prev]
        led.sleeve_held = list(new)
        orders = decide(rule, hold_val, equity, free_cash, prev, new)
        if block_entries:
            orders = [o for o in orders if not (o.side == "buy" and o.sleeve and o.symbol not in prev
                                                and o.symbol != rule.defensive)]
        if new != prev:
            out["notes"].append(f"sleeve {prev or ['T-bills']} -> {new or ['T-bills']}")

        if kill or led.flags.get("kill"):
            out["alerts"].append("kill switch is ON: no orders sent")
            if orders:
                out["refused"] += [{"symbol": o.symbol, "side": o.side, "reason": "kill switch"} for o in orders]
            led.last_pass = today
            return out

        # ---- risk checks and execution: sells first, then buys with settled cash ----
        allow = set(spec.mom.universe) | {rule.core, rule.defensive}
        assets = gw.assets(sorted({o.symbol for o in orders}))
        eq_now = led.equity(px)
        budget = led.free_cash()          # Founder/portfolio capital limit: the books, never the broker's buying power
        spent = 0.0
        broker_qty = gw.positions() if gw.is_broker else None
        for o in sorted(orders, key=lambda o: o.side != "sell"):
            reason = asset_allowed(o.symbol, allow, assets.get(o.symbol))
            q = None if reason else gw.quote(o.symbol)
            if not reason:
                reason = quote_problem(o.symbol, *(q or (0, 0, None)), px.get(o.symbol, 0.0), rc,
                                       check_age=gw.is_broker)
            cid = f"hb-{spec.pid}-{today}-{o.symbol}-{o.side}"
            if not reason and cid in led.order_ids:
                reason = "duplicate order for today refused"
            if not reason and o.side == "buy" and led.flags.get("wealth_pause"):
                reason = "account drawdown pause: new purchases need the founder's approval to restart"
            if not reason and o.side == "sell":
                held_q = led.qty(o.symbol)
                if broker_qty is not None:
                    held_q = min(held_q, broker_qty.get(o.symbol, 0.0))   # never sell more than truly held
                want_q = min(held_q, o.dollars / q[0]) if q[0] > 0 else 0
                if o.dollars >= hold_val.get(o.symbol, 0) - 0.01:
                    want_q = held_q                                   # full exit: sell every share
                qty, limit = gw.sell_terms(want_q, q[0], q[1])
                if qty <= 0 or qty * q[0] < rc.min_order:
                    reason = f"sell below the ${rc.min_order:.0f} minimum"
            elif not reason:
                amt = min(o.dollars, led.settled, budget - spent)
                fractionable = assets.get(o.symbol, {}).get("fractionable", True)
                qty, limit = gw.buy_terms(amt, q[0], q[1], fractionable)
                if qty > 0 and qty * limit > budget - spent + 0.005:
                    reason = "capital limit: order would exceed this portfolio's own settled cash"
                elif amt < rc.min_order or qty <= 0:
                    reason = ("not enough settled cash" if amt < rc.min_order
                              else f"{o.symbol} is not fractionable and ${amt:.2f} is less than one share")
                else:
                    reason = cap_problem(o.symbol, led.values(px).get(o.symbol, 0) + amt, eq_now, rule, rc)
            if reason:
                out["refused"].append({"symbol": o.symbol, "side": o.side, "dollars": round(o.dollars, 2),
                                       "reason": reason})
                self.journal({"t": today, "pid": spec.pid, "event": "refused", "symbol": o.symbol,
                              "side": o.side, "reason": reason})
                continue
            led.order_ids.append(cid)
            led.pending.append({"cid": cid, "symbol": o.symbol, "side": o.side, "qty": qty, "limit": limit,
                                "sleeve": o.sleeve, "reason": o.reason, "day": today, "status": "submitting",
                                "ref_px": q[1] if o.side == "buy" else q[0], "exit": o.reason.startswith("left")})
            persist()                                                 # persist BEFORE sending
            try:
                r = gw.submit(cid, o.symbol, o.side, qty, limit)
            except Exception as e:  # noqa: BLE001
                led.pending[-1]["status"] = "error"
                out["alerts"].append(f"order error {o.symbol}: {e}")
                led.flags["order_errors"] = led.flags.get("order_errors", 0) + 1
                if led.flags["order_errors"] >= 2:
                    led.flags["kill"] = today
                continue
            if o.side == "buy":
                spent += float(r.get("filled_qty") or 0) * float(r.get("filled_avg_price") or 0)
            self._apply_result(led, led.pending[-1], r, spec, px, sleeve_syms, out, today)
            if r["status"] in ("rejected", "canceled", "expired") and float(r.get("filled_qty") or 0) == 0:
                out["alerts"].append(f"order {o.side} {o.symbol} was {r['status']} by the broker")
                led.flags["order_errors"] = led.flags.get("order_errors", 0) + 1
                if led.flags["order_errors"] >= 2:
                    led.flags["kill"] = today
                    out["alerts"].append("two order failures: this portfolio's kill switch is ON")
            persist()
        led.pending = [p for p in led.pending if p["status"] in ("submitting", "new", "accepted",
                                                                 "partially_filled", "pending_new")]
        led.last_pass = today
        self.journal({"t": today, "pid": spec.pid, "event": "pass", "sleeve": new, "orders": len(out["orders"]),
                      "refused": len(out["refused"]), "alerts": out["alerts"]})
        return out

    def _apply_result(self, led, pend, r, spec, px, sleeve_syms, out, today):
        pend["status"] = r["status"]
        total_q, avg = float(r.get("filled_qty") or 0), r.get("filled_avg_price")
        done_q, done_cash = pend.get("applied_qty", 0.0), pend.get("applied_cash", 0.0)
        fq = total_q - done_q                         # only the part not yet booked (partial fills)
        if fq <= 1e-12 or not avg:
            return
        fp = (total_q * float(avg) - done_cash) / fq   # price of the newly filled part
        pend["applied_qty"], pend["applied_cash"] = total_q, total_q * float(avg)
        led.flags.pop("order_errors", None)           # a successful fill resets the failure count
        cost_est = abs(fp - pend["ref_px"]) * fq          # slippage/spread vs the quote used to size the order
        if pend["side"] == "buy":
            led.apply_buy(today, pend["symbol"], fq * fp, fq, cost_est, pend["sleeve"])
        else:
            pnl = led.apply_sell(today, pend["symbol"], fq * fp, fq, cost_est, pend["sleeve"], spec.vault_pct,
                                 pend["exit"])
            pend["pnl"] = round(pnl, 2)
        out["orders"].append({k: pend[k] for k in ("symbol", "side", "reason")} |
                             {"qty": round(fq, 9), "price": round(fp, 4), "dollars": round(fq * fp, 2),
                              **({"pnl": pend["pnl"]} if "pnl" in pend else {})})
        self.journal({"t": today, "pid": spec.pid, "event": "fill", "symbol": pend["symbol"], "side": pend["side"],
                      "qty": fq, "price": fp, "reason": pend["reason"], "pnl": pend.get("pnl")})

    def _recover(self, led, gw, spec, px, out, today):
        """Before deciding: finish orders from earlier passes and adopt any order the broker has under this
        portfolio's id prefix that the books never recorded (e.g. the run died after sending)."""
        sleeve_syms = set(spec.mom.universe) | {spec.rule.defensive}
        for p in list(led.pending):
            r = gw.get_order(p["cid"])
            if r is None:
                p["status"] = "never_sent"
                out["notes"].append(f"order {p['cid']} never reached the broker; dropped")
            else:
                self._apply_result(led, p, r, spec, px, sleeve_syms, out, p["day"])
        known = set(led.order_ids)
        for r in gw.orders_since(f"hb-{spec.pid}-"):
            if r["cid"] in known:
                continue
            day = r["cid"].split("-")[-5:-2]
            p = {"cid": r["cid"], "symbol": r["symbol"], "side": r["side"], "qty": r.get("qty"), "limit": None,
                 "sleeve": r["symbol"] != spec.rule.core, "reason": "recovered after a failed run",
                 "day": "-".join(day) if len(day) == 3 else today, "status": r["status"],
                 "ref_px": r.get("filled_avg_price") or 0.0,
                 "exit": r["side"] == "sell" and r["symbol"] in spec.mom.universe}
            led.order_ids.append(r["cid"])
            led.pending.append(p)
            out["alerts"].append(f"recovered order {r['cid']} ({r['status']}) that the books had not recorded")
            self._apply_result(led, p, r, spec, px, sleeve_syms, out, p["day"])
        led.pending = [p for p in led.pending if p["status"] in ("new", "accepted", "partially_filled", "pending_new")]

    def _reconcile(self, state, broker, px, summary) -> bool:
        books = state.get("portfolios", {})
        ledger_qty: dict = {}
        for spec in self.specs:
            if spec.mode == "broker" and spec.pid in books:
                for s, p in books[spec.pid]["positions"].items():
                    ledger_qty[s] = ledger_qty.get(s, 0.0) + p["qty"]
        problems = reconcile(broker.positions(), ledger_qty, px, self.rc.recon_tolerance)
        summary["reconciliation"] = problems or "clean"
        if problems:
            state["kill_switch"] = True
            summary["alerts"].append("reconciliation failed, kill switch ON: " + "; ".join(problems))
        return bool(problems)


def owner_resume(state: dict, pid: str, reason: str, px: dict, sleeve_syms: set, today: str) -> str:
    """Owner-approved restart after a breaker or kill switch. Resets the sleeve high so it can't re-trip at once."""
    led = Ledger.from_dict(state["portfolios"][pid])
    if led.flags.pop("wealth_pause", None):
        led.flags["wealth_pause_resumed"] = today   # no re-trip in this same episode; re-arms after recovery
    led.flags.pop("breaker", None); led.flags.pop("kill", None); led.flags.pop("order_errors", None)
    led.sleeve_hwm = led.sleeve_nav_v
    led.flags.setdefault("resumes", []).append({"date": today, "reason": reason})
    state["portfolios"][pid] = led.to_dict()
    return f"{pid} resumed on {today}: {reason}"
