"""Intraday sleeve engine: scan -> completed-candle signals -> risk checks -> Alpaca PAPER bracket order ->
monitor -> exit -> reconcile -> journal. One call to tick() per minute.

Safety rules enforced here (each refusal is journaled with a plain-English reason):
  * mode 'signal' never sends orders; mode 'paper' sends orders only for setups in `execute`.
  * capital = cash the founder allocated to the sleeve; buys use SETTLED cash only (T+1 after sales),
    never the paper account's buying power. Whole shares only (bracket orders need them).
  * 0.5% planned risk per trade, -1% daily loss stop, one position, max 3 entries/day, no averaging down,
    long only, no entries after 15:00 ET, flatten at 15:50 ET, nothing held overnight.
  * entries need fresh bars, a fresh quote with a tight spread, and price not run past the chase cap.
  * every entry is a bracket (stop + target at the broker). If the stop leg is not live after the fill,
    the position is closed immediately. Unknown broker state -> no new entries (kill switch after repeats).
  * duplicate protection: deterministic client order ids, state persisted BEFORE an order is sent.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Callable

import pandas as pd

from .candles import rvol, rvol_daily, session_frame, volume_profile
from .intraday_sim import simulate_trade
from .intraday_universe import asset_ok
from .setups import SETUP_IDS, Signal, evaluate, invalidated, reason_key

LIVE_STOP_STATES = {"new", "held", "accepted", "pending_new", "partially_filled"}


def hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m) - 570


def new_state() -> dict:
    return {"sleeve": {"allocated": 0.0, "settled": 0.0, "unsettled": [], "realized": 0.0, "allocations": []},
            "position": None, "day": {}, "order_ids": [], "trades": [], "shadow": {}, "kill_switch": False}


class IntradayEngine:
    def __init__(self, cfg: dict, gw, state: dict, journal: Callable[[dict], None], persist: Callable[[], None]):
        self.cfg, self.gw, self.s, self.j, self.persist = cfg, gw, state, journal, persist
        self.r = cfg["risk"]
        self.u = cfg["universe"]
        self.mode = cfg.get("mode", "signal")
        self.prefix = cfg.get("order_prefix", "id-")
        self.account = cfg.get("account", "FOUNDER")
        self.execute = set(cfg.get("execute") or []) if self.mode == "paper" else set()
        self.evaluate_ids = [x for x in cfg.get("evaluate", SETUP_IDS) if x in SETUP_IDS]
        self.raw: dict[str, pd.DataFrame] = {}
        self.frames: dict[str, pd.DataFrame] = {}
        self.profiles: dict[str, dict] = {}
        self.eligible: list[str] = []
        self.ctx: dict[str, dict] = {}            # gappers: {"prev_close", "adv20"} (stocks in play)

    # ------------------------------------------------------------------ helpers
    @property
    def day(self) -> dict:
        return self.s["day"]

    def note(self, event: str, **kw):
        rec = {"t": datetime.now().astimezone().isoformat(timespec="seconds"), "date": self.day.get("date"),
               "event": event, **kw}
        self.j(rec)
        return rec

    def equity(self) -> float:
        sl = self.s["sleeve"]
        return sl["settled"] + sum(a for _, a in sl["unsettled"])

    # ------------------------------------------------------------------ day start
    def start_day(self, date: str, pool: list[str], daily: dict, profiles_src: dict, assets: dict) -> dict:
        sl = self.s["sleeve"]
        moved = sum(a for d, a in sl["unsettled"] if d < date)          # T+1 settlement
        sl["unsettled"] = [[d, a] for d, a in sl["unsettled"] if d >= date]
        sl["settled"] += moved
        if self.day.get("date") != date:
            self.s["day"] = {"date": date, "start_equity": round(self.equity(), 4), "entries": 0, "realized": 0.0,
                             "errors": 0, "halted": None, "signals": 0, "rejections": {}, "setup_state": {},
                             "last_m": {}, "scanned": 0, "eligible": 0}
            self.s["shadow"] = {k: {"open": None, "entries": 0, "day_R": 0.0, "busy_until": -1, "trades": []}
                                for k in SETUP_IDS}
        elig, why = [], {}
        for sym in pool:
            a = assets.get(sym)
            reason = asset_ok(a) if a is not None else "asset not found"
            d = daily.get(sym)
            if not reason:
                if d is None or len(d) < 21:
                    reason = "not enough daily history"
                else:
                    pc, adv = float(d["close"].iloc[-1]), float(d["volume"].iloc[-20:].mean())
                    if not (self.u["price_min"] <= pc <= self.u["price_max"]):
                        reason = f"price ${pc:.2f} outside ${self.u['price_min']}-${self.u['price_max']}"
                    elif adv < self.u["adv_min"]:
                        reason = f"average volume {adv:,.0f} < {self.u['adv_min']:,}"
            if not reason and not profiles_src.get(sym):
                reason = "no intraday volume history (IEX)"
            if reason:
                why[sym] = reason
            else:
                elig.append(sym)
                self.profiles[sym] = profiles_src[sym]
        self.eligible = elig
        self.day.update(scanned=len(pool), eligible=len(elig), released=round(moved, 4))
        self.note("scan", scanned=len(pool), eligible=len(elig), excluded=dict(list(why.items())[:60]),
                  settled_cash=round(sl["settled"], 4), equity=round(self.equity(), 4), mode=self.mode,
                  execute=sorted(self.execute))
        return {"eligible": elig, "excluded": why}

    # ------------------------------------------------------------------ per-minute tick
    def add_gappers(self, found: dict[str, dict]) -> list[str]:
        """Add today's gap-up 'stocks in play' (from the market movers scan) to the watch list."""
        added = []
        for sym, c in found.items():
            if sym not in self.ctx:
                self.ctx[sym] = c
                if sym not in self.eligible:
                    self.eligible.append(sym)
                added.append(sym)
        if added:
            self.note("gappers_added", symbols={k: self.ctx[k] for k in added})
        return added

    def update_bars(self, new: dict[str, pd.DataFrame]) -> None:
        for sym, df in new.items():
            old = self.raw.get(sym)
            df = df.copy()
            df["t"] = pd.to_datetime(df["t"], utc=True)
            self.raw[sym] = (pd.concat([old, df]).drop_duplicates("t", keep="last").sort_values("t")
                             if old is not None else df)

    def tick(self, now: pd.Timestamp, clock_open: bool) -> dict:
        """now: tz-aware current time. Uses only bars whose minute has fully ended."""
        et = now.tz_convert("America/New_York")
        m_now = et.hour * 60 + et.minute - 570                      # current (forming) minute index
        out = {"m": m_now, "signals": [], "orders": []}
        if not clock_open:
            return out
        cutoff = now.floor("min")                                   # bars starting before this are complete
        for sym in list(self.raw):
            r = self.raw[sym]
            r = r[r["t"] < cutoff]
            r = r[r["t"].dt.tz_convert("America/New_York").dt.date == et.date()]
            if len(r):
                f = session_frame(r)
                self.frames[sym] = f[(f["m"] >= 0) & (f["m"] < 390)].reset_index(drop=True)
        self._manage_position(now, m_now)
        self._shadow_update(m_now)
        self._scan_signals(now, m_now, out)
        self.persist()
        return out

    # ------------------------------------------------------------------ signals
    def _scan_signals(self, now, m_now, out):
        fresh_m = m_now - 1                                         # the bar that just completed
        cands = []
        for sym in self.eligible:
            d = self.frames.get(sym)
            if d is None or d.empty:
                continue
            last = self.day["last_m"].get(sym, -1)
            idx = [i for i in range(len(d)) if int(d["m"].iloc[i]) > last]
            if not idx:
                continue
            self.day["last_m"][sym] = int(d["m"].iloc[-1])
            st = self.day["setup_state"].setdefault(sym, {})
            for i in idx:
                c = self.ctx.get(sym)
                if c and "gap" not in st and int(d["m"].iloc[0]) <= 2 and c.get("prev_close"):
                    st["gap"] = float(d["o"].iloc[0]) / c["prev_close"] - 1
                if sym in self.profiles:
                    rv = rvol(float(d["cumv"].iloc[i]), int(d["m"].iloc[i]), self.profiles.get(sym))
                else:   # no intraday history of its own (a gapper): RVOL vs average daily volume
                    rv = rvol_daily(float(d["cumv"].iloc[i]), int(d["m"].iloc[i]), (c or {}).get("adv20"))
                for setup in self.evaluate_ids:
                    res = evaluate(setup, d, i, sym, rv, (self.cfg.get("params") or {}).get(setup), st)
                    if res is None:
                        continue
                    stale = int(d["m"].iloc[i]) < fresh_m
                    if isinstance(res, str):
                        key = reason_key(setup, res)
                        self.day["rejections"][key] = self.day["rejections"].get(key, 0) + 1
                        self.note("candidate_rejected", setup=setup, symbol=sym, m=int(d["m"].iloc[i]), reason=res)
                        continue
                    self.day["signals"] += 1
                    rec = self.note("signal", setup=setup, symbol=sym, m=res.m, ref=res.ref, stop=round(res.stop, 4),
                                    target=round(res.target, 4), rvol=round(res.rvol or 0, 2), why=res.why,
                                    conditions=res.conditions, stale=stale)
                    out["signals"].append(rec)
                    if stale:
                        continue                                    # missed bar (restart/gap): never traded late
                    self._shadow_open(res)
                    if setup in self.execute:
                        cands.append(res)
        if cands and self.s["position"] is None:
            cands.sort(key=lambda s: -(s.rvol or 0))
            for sig in cands:
                if self._try_enter(sig, now, m_now, out):
                    break

    # ------------------------------------------------------------------ entry
    def _block_reason(self, sig: Signal, m_now: int) -> str | None:
        if self.s.get("kill_switch"):
            return "kill switch is ON"
        if self.day.get("halted"):
            return f"entries halted today: {self.day['halted']}"
        if self.s["position"] is not None:
            return "one position at a time"
        if self.day["entries"] >= self.r["max_entries_per_day"]:
            return "max entries for today reached"
        if m_now > hm(self.r["last_entry"]):
            return "no new entries after " + self.r["last_entry"]
        if self.day["realized"] <= -self.r["daily_loss_pct"] * self.day["start_equity"] and self.day["start_equity"] > 0:
            return "daily loss limit reached"
        return None

    def _try_enter(self, sig: Signal, now, m_now, out) -> bool:
        reason = self._block_reason(sig, m_now)
        q = None
        if not reason:
            try:
                q = self.gw.quote(sig.symbol)
            except Exception as e:  # noqa: BLE001
                reason = f"quote unavailable: {e}"
        if not reason:
            if q is None or q[0] <= 0 or q[1] <= 0:
                reason = "no valid quote"
            elif q[2] > self.r["max_quote_age_s"]:
                reason = f"stale quote ({q[2]:.0f}s old)"
            elif (q[1] - q[0]) / ((q[1] + q[0]) / 2) > ((self.cfg.get("params") or {}).get(sig.setup, {})
                                                         .get("max_spread_pct", self.r["max_spread_pct"])):
                reason = f"spread {(q[1] - q[0]) / ((q[1] + q[0]) / 2):.2%} too wide"
        limit = round(sig.ref + self.r["chase_r"] * sig.risk_ps, 2)
        if not reason and q[1] > limit:
            reason = f"price ran away: ask {q[1]:.2f} > limit {limit:.2f}"
        if not reason and q[1] <= sig.stop:
            reason = "ask already at or below the stop"
        qty = 0
        if not reason:
            eq = self.equity()
            risk_usd = self.r["risk_per_trade_pct"] * eq
            entry_est = min(q[1], limit)
            risk_ps = entry_est - sig.stop
            settled = self.s["sleeve"]["settled"]
            qty = min(math.floor(risk_usd / risk_ps) if risk_ps > 0 else 0, math.floor(settled / limit))
            if qty < 1:
                reason = (f"account too small: ${risk_usd:.2f} risk allows {math.floor(risk_usd / risk_ps) if risk_ps > 0 else 0} "
                          f"share(s) at ${risk_ps:.2f}/share risk; settled cash ${settled:.2f} buys "
                          f"{math.floor(settled / limit)} at ${limit:.2f}")
        if reason:
            key = reason_key(sig.setup, "entry refused: " + reason.split(":")[0])
            self.day["rejections"][key] = self.day["rejections"].get(key, 0) + 1
            self.note("entry_refused", setup=sig.setup, symbol=sig.symbol, reason=reason)
            return False
        target = round(min(q[1], limit) + 2.0 * (min(q[1], limit) - sig.stop), 2)
        cid = f"{self.prefix}{self.day['date']}-{sig.setup}-{sig.symbol}-{self.day['entries'] + 1}"
        if cid in self.s["order_ids"]:
            self.note("entry_refused", setup=sig.setup, symbol=sig.symbol, reason="duplicate order id")
            return False
        self.s["order_ids"].append(cid)
        self.s["position"] = {"cid": cid, "symbol": sig.symbol, "setup": sig.setup, "qty": qty, "limit": limit,
                              "stop": round(sig.stop, 2), "target": target, "status": "submitting",
                              "signal_m": sig.m, "sent_at": now.isoformat(), "why": sig.why,
                              "conditions": sig.conditions, "rvol": sig.rvol, "quote": list(q)}
        self.day["entries"] += 1
        self.persist()                                              # persist BEFORE sending
        try:
            o = self.gw.submit_bracket(cid, sig.symbol, qty, limit, sig.stop, target)
            self.s["position"].update(status="entering", order_id=o["id"])
            self.note("entry_sent", **{k: self.s["position"][k] for k in ("cid", "symbol", "setup", "qty", "limit",
                                                                           "stop", "target", "why")})
            out["orders"].append(cid)
        except Exception as e:  # noqa: BLE001
            self._error(f"entry order failed for {sig.symbol}: {e}")
            o = self.gw.order(cid)                                  # did it reach the broker anyway?
            if o is None:
                self.s["position"] = None
            else:
                self.s["position"].update(status="entering", order_id=o["id"])
        self.persist()
        return True

    # ------------------------------------------------------------------ position management
    def _manage_position(self, now, m_now):
        p = self.s["position"]
        if p is None:
            return
        o = self.gw.order(p["cid"])
        if o is None:
            if p["status"] == "submitting":                         # never reached the broker
                self.note("entry_lost", symbol=p["symbol"], reason="order not found at broker; nothing held")
                self.s["position"] = None
            else:
                self._error(f"cannot read order {p['cid']}")
            return
        if p["status"] in ("submitting", "entering"):
            if o["status"] == "filled":
                p.update(status="open", entry=o["filled_avg_price"], qty=o["filled_qty"], filled_at=now.isoformat())
                self.s["sleeve"]["settled"] -= p["entry"] * p["qty"]
                self.note("entry_filled", symbol=p["symbol"], setup=p["setup"], qty=p["qty"], entry=p["entry"],
                          stop=p["stop"], target=p["target"])
                if not self._stop_live(o):
                    self._exit(p, o, now, "protective stop not live at the broker: closed immediately")
                return
            age = (now - pd.Timestamp(p["sent_at"])).total_seconds()
            if o["status"] in ("canceled", "expired", "rejected") or age > self.r["entry_timeout_s"]:
                self.gw.cancel(o["id"])
                o2 = self.gw.order(p["cid"]) or o
                if o2["filled_qty"] > 0:                            # partial fill: unwind it, never hold a half bracket
                    p.update(status="open", entry=o2["filled_avg_price"], qty=o2["filled_qty"])
                    self.s["sleeve"]["settled"] -= p["entry"] * p["qty"]
                    self._exit(p, o2, now, "partial fill unwound")
                else:
                    self.note("entry_not_filled", symbol=p["symbol"], setup=p["setup"], status=o2["status"],
                              reason="limit not reached within the entry window" if age > self.r["entry_timeout_s"]
                              else f"broker {o2['status']}")
                    if o2["status"] == "rejected":
                        self._error(f"entry rejected for {p['symbol']}")
                    self.s["position"] = None
            return
        if p["status"] == "open":
            leg = next((x for x in o["legs"] if x["status"] == "filled"), None)
            if leg:                                                 # stop or target filled at the broker
                how = "stop" if leg["type"] in ("stop", "stop_limit") else "target"
                self._close(p, leg["filled_avg_price"], how, now)
                return
            if not self._stop_live(o):
                self._exit(p, o, now, "protective stop leg no longer live")
                return
            d = self.frames.get(p["symbol"])
            if m_now >= hm(self.r["flatten"]):
                self._exit(p, o, now, "end of day flatten (15:50 ET)")
            elif d is not None and len(d):
                why = invalidated(p["setup"], d, len(d) - 1, p["entry"])
                if why and int(d["m"].iloc[-1]) > p["signal_m"] + 1:
                    self._exit(p, o, now, why)
            return
        if p["status"] == "exiting":
            e = self.gw.order(p["exit_cid"])
            if e and e["status"] == "filled":
                self._close(p, e["filled_avg_price"], p["exit_reason"], now)
            elif e and e["status"] in ("canceled", "rejected", "expired"):
                self._error(f"exit order {e['status']} for {p['symbol']}")
                self._send_exit(p, now, retry=True)

    @staticmethod
    def _stop_live(o) -> bool:
        return any(x["type"] in ("stop", "stop_limit") and x["status"] in LIVE_STOP_STATES for x in o["legs"])

    def _exit(self, p, o, now, reason):
        for x in o.get("legs", []):
            if x["status"] not in ("filled", "canceled", "expired", "rejected"):
                self.gw.cancel(x["id"])
        if o["status"] not in ("filled", "canceled", "expired", "rejected"):
            self.gw.cancel(o["id"])
        o2 = self.gw.order(p["cid"]) or o
        leg = next((x for x in o2.get("legs", []) if x["status"] == "filled"), None)
        if leg:                                                     # a leg filled while we were cancelling
            how = "stop" if leg["type"] in ("stop", "stop_limit") else "target"
            self._close(p, leg["filled_avg_price"], how, now)
            return
        p["exit_reason"] = reason
        self._send_exit(p, now)

    def _send_exit(self, p, now, retry=False):
        p["exit_tries"] = p.get("exit_tries", 0) + 1
        p["exit_cid"] = f"{p['cid']}-x{p['exit_tries']}"
        p["status"] = "exiting"
        self.persist()
        try:
            qty = self.gw.position_qty(p["symbol"]) or p["qty"]
            self.gw.market_sell(p["exit_cid"], p["symbol"], min(qty, p["qty"]))
            self.note("exit_sent", symbol=p["symbol"], reason=p["exit_reason"], cid=p["exit_cid"])
        except Exception as e:  # noqa: BLE001
            self._error(f"exit order failed for {p['symbol']}: {e}")

    def _close(self, p, px, how, now):
        pnl = (px - p["entry"]) * p["qty"]
        sl = self.s["sleeve"]
        sl["unsettled"].append([self.day["date"], px * p["qty"]])   # sale proceeds settle T+1
        sl["realized"] += pnl
        self.day["realized"] += pnl
        risk = p["entry"] - p["stop"]
        tr = {"date": self.day["date"], "symbol": p["symbol"], "setup": p["setup"], "qty": p["qty"],
              "entry": p["entry"], "exit": px, "stop": p["stop"], "target": p["target"], "pnl": round(pnl, 4),
              "R": round((px - p["entry"]) / risk, 3) if risk > 0 else None, "how": how,
              "entered": p.get("filled_at"), "exited": now.isoformat(), "why_in": p["why"],
              "conditions": p.get("conditions"), "rvol": p.get("rvol"), "entry_quote": p.get("quote"),
              "slippage_entry": round(p["entry"] - p["limit"], 4)}
        self.s["trades"].append(tr)
        self.note("trade_closed", **tr)
        self.s["position"] = None
        if self.day["realized"] <= -self.r["daily_loss_pct"] * self.day["start_equity"] and self.day["start_equity"] > 0:
            self.day["halted"] = "daily loss limit reached"
            self.note("risk", reason="daily loss limit reached: no more entries today")
        self.persist()

    def _error(self, msg):
        self.day["errors"] = self.day.get("errors", 0) + 1
        self.note("error", message=msg)
        if self.day["errors"] >= self.r["consecutive_error_kill"]:
            self.s["kill_switch"] = True
            self.day["halted"] = "repeated broker errors: kill switch ON"
            self.note("risk", reason="kill switch ON after repeated broker errors (founder restart required)")

    # ------------------------------------------------------------------ shadow (virtual) book
    def _shadow_open(self, sig: Signal):
        sh = self.s["shadow"].get(sig.setup)
        if sh is None:
            return
        if sh["open"] or sig.m <= sh["busy_until"] or sh["entries"] >= self.r["max_entries_per_day"] or sh["day_R"] <= -2.0:
            return
        if sig.m > hm(self.r["last_entry"]):
            return
        sh["open"] = {"symbol": sig.symbol, "setup": sig.setup, "m": sig.m, "ref": sig.ref, "stop": sig.stop,
                      "target": sig.target, "rvol": sig.rvol, "why": sig.why}
        sh["entries"] += 1

    def _shadow_update(self, m_now):
        for setup, sh in self.s["shadow"].items():
            o = sh["open"]
            if not o:
                continue
            d = self.frames.get(o["symbol"])
            if d is None:
                continue
            sub = d[d["m"] >= o["m"]].reset_index(drop=True)
            sig = Signal(setup, o["symbol"], o["m"], o["ref"], o["stop"], o["target"], o["rvol"], o["why"])
            res = simulate_trade(sig, sub, 0.0010, self.r["chase_r"], session_over=m_now >= 390)
            if res is None:
                sh["open"] = None
                sh["entries"] -= 1
                self.note("shadow_no_fill", setup=setup, symbol=o["symbol"])
            elif not res.get("open"):
                sh["open"] = None
                sh["day_R"] += res["R"]
                sh["busy_until"] = res["exit_m"]
                sh["trades"].append(res)
                self.note("shadow_trade", **res)

    # ------------------------------------------------------------------ end of day
    def end_of_day_report(self) -> dict:
        sl = self.s["sleeve"]
        trades = [t for t in self.s["trades"] if t["date"] == self.day.get("date")]
        rep = {"date": self.day.get("date"), "account": self.account,
               "label": f"PAPER TRADING ({self.account} account) - simulated, not real money",
               "mode": self.mode, "execute": sorted(self.execute), "scanned": self.day.get("scanned"),
               "eligible": self.day.get("eligible"), "signals": self.day.get("signals"),
               "trades": trades, "realized_pnl": round(sum(t["pnl"] for t in trades), 4),
               "settled_cash": round(sl["settled"], 4), "unsettled_cash": round(sum(a for _, a in sl["unsettled"]), 4),
               "sleeve_equity": round(self.equity(), 4), "allocated": sl["allocated"],
               "shadow": {k: {"trades": len(v["trades"]), "R": round(sum(t["R"] for t in v["trades"]), 3),
                              "detail": v["trades"]} for k, v in self.s["shadow"].items()},
               "rejections": self.day.get("rejections"), "halted": self.day.get("halted"),
               "kill_switch": self.s.get("kill_switch"), "position_open": self.s["position"]}
        return rep


def profiles_from_bars(five_min: dict[str, pd.DataFrame], today: str) -> dict[str, dict]:
    """Build each symbol's prior-20-session cumulative volume profile from 5-minute bars (before today)."""
    out = {}
    for sym, df in five_min.items():
        df = df.copy()
        df["t"] = pd.to_datetime(df["t"], utc=True)
        et = df["t"].dt.tz_convert("America/New_York")
        df["date"] = et.dt.date.astype(str)
        df["m"] = et.dt.hour * 60 + et.dt.minute - 570
        df = df[(df["m"] >= 0) & (df["m"] < 390) & (df["date"] < today)]
        days = [g for _, g in df.groupby("date")][-20:]
        if len(days) >= 10:
            out[sym] = volume_profile(days)
    return out


def dumps(x) -> str:
    return json.dumps(x, default=str)
