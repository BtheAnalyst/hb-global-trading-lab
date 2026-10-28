"""Stock/ETF engine entry point (runs inside GitHub Actions, where Alpaca is reachable).

Modes (every mode except check needs --manual until Gate 5 sets execution_enabled: true)
  check : read-only. Fetches bars, asset flags and quotes; runs the engine on a COPY of the books and
          prints what it would do. Saves nothing, sends nothing, any time of day.
  shadow: the 8 research portfolios only (simulated fills at live quotes); saves the books.
  paper : shadow + the ONE selected broker portfolio (Founder's Account), whose orders go to the Alpaca
          PAPER account. Sized only from the Founder's own books, never the paper account's buying power.
  mark  : after the close, marks every portfolio at today's closing prices, records history, writes the
          dashboard tables and the Daily Letter. Reporting only: no orders, so it may run unattended.
  harden: sets broker-side backstops on the paper account (no shorting, margin multiplier 1, no options).
  resume: owner-approved restart of a portfolio after a breaker or kill switch (--pid, --reason).
Keys come from GitHub Secrets and are never printed.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hbgtl.equity_gateway import AlpacaEquityGateway, DryBroker, SimGateway  # noqa: E402
from hbgtl.equity_risk import RiskConfig, asset_allowed  # noqa: E402
from hbgtl.equity_runner import Engine, specs_from_cfg  # noqa: E402

ET = ZoneInfo("America/New_York")
STATE = ROOT / "state" / "equity_state.json"
JOURNAL = ROOT / "state" / "equity_journal.jsonl"


RUN_LOG: list[dict] = []
LAST_RUN = ROOT / "state" / "last_run.json"


def write_last_run(mode: str, code: int) -> None:
    """Machine-readable result of this run, committed with the books, read by the alert task."""
    try:
        LAST_RUN.parent.mkdir(exist_ok=True)
        LAST_RUN.write_text(json.dumps({"at": datetime.now(ET).isoformat(timespec="seconds"), "mode": mode,
                                        "exit_code": code, "problem": code != 0 or any(
                                            n["level"] in ("warning", "error") for n in RUN_LOG),
                                        "notes": RUN_LOG}, indent=1, default=str))
    except Exception:  # noqa: BLE001
        pass


def note(title: str, msg: str, level: str = "notice") -> None:
    RUN_LOG.append({"level": level, "title": title, "message": msg})
    print(f"::{level} title={title}::{msg}")


def fetch_closes(gw: AlpacaEquityGateway, symbols: list[str], days: int = 420, include_today: bool = False) -> pd.DataFrame:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    start = datetime.now(timezone.utc) - timedelta(days=int(days * 1.5))
    end = datetime.now(timezone.utc) - timedelta(minutes=20)        # free plan: SIP needs a 15-minute delay
    bars = gw.data.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start,
                                                   end=end, feed=DataFeed.SIP, adjustment=Adjustment.ALL)).data
    today = datetime.now(ET).date()
    cols = {s: pd.Series({pd.Timestamp(b.timestamp.astimezone(ET).date()): float(b.close)
                          for b in bars.get(s, []) if include_today or b.timestamp.astimezone(ET).date() < today})
            for s in symbols}
    return pd.DataFrame(cols).sort_index()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["check", "shadow", "paper", "mark", "harden", "resume"], default="check")
    ap.add_argument("--manual", action="store_true", help="set by the manual (workflow_dispatch) run")
    ap.add_argument("--pid", default="")
    ap.add_argument("--reason", default="")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(ROOT / "config" / "equity.yaml"))
    if a.mode not in ("check", "mark") and not (cfg.get("execution_enabled") or a.manual):
        note("Blocked", "execution_enabled is false (Gate 5 not approved) and this was not a manual run", "warning")
        return 0
    specs = [s for s in specs_from_cfg(cfg) if s.enabled]           # refuses >1 or unselected broker portfolio
    if a.mode == "shadow":
        specs = [s for s in specs if s.mode == "shadow"]
    alp = AlpacaEquityGateway()
    if a.mode == "mark":
        return mark(alp, specs)
    if a.mode == "harden":
        note("Paper account backstops", str(alp.harden()))
        note("Paper account", str(alp.account()))
        return 0
    if a.mode == "resume":
        from hbgtl.equity_runner import owner_resume
        if not (a.pid and a.reason):
            note("Resume refused", "needs --pid and --reason", "error"); return 1
        state = json.loads(STATE.read_text())
        today_s = str(datetime.now(ET).date())
        if a.pid == "GLOBAL":                      # clears the account-wide kill switch (after a clean reconciliation)
            state["kill_switch"] = False
            state.setdefault("resumes", []).append({"date": today_s, "reason": a.reason})
            msg = f"global kill switch cleared on {today_s}: {a.reason}"
        else:
            spec = next(s for s in specs if s.pid == a.pid)
            closes_r = fetch_closes(alp, sorted({spec.rule.core, spec.rule.defensive, *spec.mom.universe}), days=30)
            msg = owner_resume(state, a.pid, a.reason, closes_r.iloc[-1].to_dict(),
                               set(spec.mom.universe) | {spec.rule.defensive}, today_s)
        STATE.write_text(json.dumps(state, indent=1, default=str))
        note("Owner resume", msg)
        return 0
    syms = sorted({s for sp in specs for s in (sp.rule.core, sp.rule.defensive, *sp.mom.universe)})
    today = datetime.now(ET).date()
    cal = alp.trading.get_calendar(__import__("alpaca.trading.requests", fromlist=["GetCalendarRequest"])
                                   .GetCalendarRequest(start=today - timedelta(days=10), end=today))
    sessions = [c.date for c in cal]
    if not sessions or sessions[-1] != today:
        note("Market closed", f"{today} is not a trading session: nothing to do")
        if a.mode != "check":
            return 0
    if a.mode in ("shadow", "paper"):
        clock = alp.trading.get_clock()
        now = datetime.now(ET)
        if not clock.is_open:
            note("Market closed", "the market is not open right now: no orders, nothing changed"); return 0
        if (now.hour, now.minute) < (9, 45):
            note("Too early", "decisions wait until 9:45 ET so opening-auction prices settle"); return 0
    prev_session = [d for d in sessions if d < today][-1]
    try:
        closes = fetch_closes(alp, syms)
    except Exception as e:  # noqa: BLE001
        note("Data outage", f"could not fetch prices ({type(e).__name__}): no decisions, nothing changed", "error")
        return 1
    last = closes.index[-1].date()
    stale = last != prev_session
    assets = alp.assets(syms)
    problems = {s: asset_allowed(s, set(syms), assets[s]) for s in syms}
    nonfrac = [s for s in syms if not assets[s]["fractionable"]]
    note("Data", f"{len(closes)} completed daily bars per ETF through {last} (previous session {prev_session}); "
                 f"missing values in last row: {int(closes.iloc[-1].isna().sum())}; "
                 f"ETFs not fractionable: {nonfrac or 'none'}; refused by allowlist checks: "
                 f"{ {k: v for k, v in problems.items() if v} or 'none'}")
    if stale:
        note("Stale data", f"last completed bar is {last}, expected {prev_session}: no decisions today", "warning")
        return 0
    quotes = {}
    for s in syms:
        q = alp.quote(s)
        quotes[s] = q
    note("Quotes", "; ".join(f"{s} {q[0]:.2f}/{q[1]:.2f} ({(q[1]-q[0])/((q[0]+q[1])/2):.2%})"
                             if q and q[0] > 0 else f"{s} no quote" for s, q in quotes.items()))
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    work = state if a.mode != "check" else copy.deepcopy(state)
    events = []

    def save():                                   # persist-before-send: write the books to disk at once
        if a.mode != "check":
            STATE.parent.mkdir(exist_ok=True)
            STATE.write_text(json.dumps(work, indent=1, default=str))
    eng = Engine(specs, RiskConfig.from_cfg(cfg.get("risk")), journal=events.append, save=save)
    shadow = SimGateway(quotes=lambda s: quotes.get(s), cost=0.0, assets=assets)   # real quotes: spread is real cost
    dry = DryBroker(alp) if a.mode == "check" else None
    gws = {"shadow": shadow, "broker": alp if a.mode == "paper" else dry}
    if a.mode in ("paper", "check"):
        acct = alp.account()
        note("Paper account (not the Founder's capital)", f"Alpaca paper cash ${acct['cash']:,.2f}, buying power "
             f"${acct['buying_power']:,.2f}, status {acct['status']}. The bot sizes orders only from the Founder's "
             "own books; this balance is never used.")
        if acct["trading_blocked"] or acct["account_blocked"]:
            note("Blocked", "the paper account reports trading blocked", "error"); return 1
    work["marks"] = {"date": str(closes.index[-1].date()), "px": {k: float(v) for k, v in closes.iloc[-1].items()}}
    try:
        summ = eng.run_day(work, closes, len(closes) - 1, str(today), gws)
        work["last_alerts"] = summ["alerts"]
    finally:
        save()
        if a.mode != "check":
            with JOURNAL.open("a") as f:
                for e in events:
                    f.write(json.dumps(e, default=str) + "\n")
    lines = []
    for pid, r in summ["portfolios"].items():
        o = ", ".join(f"{x['side']} {x['symbol']} ${x['dollars']:.2f}" for x in r["orders"]) or "no orders"
        rf = ", ".join(f"{x['symbol']}: {x['reason']}" for x in r["refused"])
        lines.append(f"{pid}: {o}" + (f" | refused {rf}" if rf else "") + (f" | {'; '.join(r['notes'])}" if r["notes"] else ""))
    note("Engine pass" + (" (dry: nothing saved)" if a.mode == "check" else ""), " || ".join(lines)[:3800])
    if summ["alerts"]:
        note("Alerts", " || ".join(summ["alerts"])[:3800], "warning")
    for pid, r in summ["portfolios"].items():
        spec = next((sp for sp in specs if sp.pid == pid), None)
        if spec is not None and spec.mode == "broker" and r["refused"] and a.mode == "paper":
            frozen = [x for x in r["refused"] if x["reason"].startswith("ETF allocation strategy frozen")]
            other = [x for x in r["refused"] if x not in frozen]
            if frozen:   # expected while the ETF strategy is stopped: a notice, not an alert
                note("Founder ETF orders frozen", "; ".join(f"{x['side']} {x['symbol']}" for x in frozen))
            if other:
                note("Founder order refused", "; ".join(f"{x['side']} {x['symbol']}: {x['reason']}" for x in other),
                     "warning")
    if "reconciliation" in summ:
        bspec = next((sp for sp in specs if sp.mode == "broker"), None)
        fb = work.get("portfolios", {}).get(bspec.pid, {}) if bspec else {}
        note("Reconciliation", f"{summ['reconciliation']} | broker positions {alp.positions() or 'none'} | Founder books: "
             f"positions {fb.get('positions') or 'none'}, settled cash ${fb.get('settled', 0):,.2f}, "
             f"contributed ${fb.get('contributed', 0):,.2f}")
    if dry is not None:
        note("Founder orders that WOULD be sent (dry run, nothing sent)",
             "; ".join(f"{o['side']} {o['symbol']} qty {o['qty']} limit ${o['limit']} (~${o['dollars']})"
                       for o in dry.would_send) or "none")
    return 0


def mark(alp, specs) -> int:
    """Reporting only. Marks the books at the latest closes (today's once the session is over)."""
    from hbgtl.equity_report import daily_letter, export_dashboard, record_history
    now = datetime.now(ET)
    syms = sorted({s for sp in specs for s in (sp.rule.core, sp.rule.defensive, *sp.mom.universe)})
    after_close = (now.hour, now.minute) >= (16, 10)
    try:
        closes = fetch_closes(alp, syms, days=30, include_today=after_close)
    except Exception as e:  # noqa: BLE001
        note("Data outage", f"could not fetch closing prices ({type(e).__name__}); report not refreshed", "error")
        return 1
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    as_of = str(closes.index[-1].date())
    px = {k: float(v) for k, v in closes.iloc[-1].items() if v == v}
    state["marks"] = {"date": as_of, "px": px}
    if state.get("portfolios"):
        record_history(state, px, as_of)
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, default=str))
    export_dashboard(state, JOURNAL, ROOT / "dashboard")
    subject, html, md = daily_letter({**state, "_journal": str(JOURNAL)}, str(now.date()), f"{now:%a %b} {now.day}")
    (ROOT / "dashboard" / "letter_email.html").write_text(html)
    (ROOT / "dashboard" / "letter_subject.txt").write_text(subject)
    (ROOT / "letter.md").write_text(md)
    note("Marked", f"{len(state.get('portfolios', {}))} portfolios at the {as_of} close; letter: {subject}")
    return 0


if __name__ == "__main__":
    _mode = next((sys.argv[i + 1] for i, x in enumerate(sys.argv) if x == "--mode" and i + 1 < len(sys.argv)), "check")
    try:
        _code = main() or 0
        if _mode in ("paper", "shadow"):
            write_last_run(_mode, _code)
        sys.exit(_code)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - logs are not visible from here; surface the error as an annotation
        import traceback
        tb = traceback.format_exc().strip().splitlines()
        note("Run failed", f"{type(e).__name__}: {' '.join(str(e).split())[:300]} | at {' / '.join(tb[-4:-1])[:600]}",
             "error")
        if _mode in ("paper", "shadow"):
            write_last_run(_mode, 1)
        sys.exit(1)
