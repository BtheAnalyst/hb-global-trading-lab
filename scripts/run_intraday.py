"""Run the intraday sleeve for one session (or part of one) on Alpaca PAPER.

  python scripts/run_intraday.py --until 12:45          # morning job
  python scripts/run_intraday.py --until 16:05 --final  # afternoon job: flatten check + daily report
  python scripts/run_intraday.py --allocate 99.80 --reason "founder approved ..."   # move Founder settled cash

State: state/intraday_state.json (+ journal state/intraday_journal.jsonl, report dashboard/intraday_daily.json).
The loop saves and pushes state every 10 minutes and after every order event, so a restarted job resumes.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hbgtl.intraday_engine import IntradayEngine, new_state, profiles_from_bars  # noqa: E402
from hbgtl.intraday_universe import RESEARCH_POOL  # noqa: E402

ET = "America/New_York"
CFG = yaml.safe_load((ROOT / "config" / "intraday.yaml").read_text())
EQUITY = ROOT / "state" / "equity_state.json"


def select_account(today: str | None = None) -> str:
    """account: auto -> FOUNDER once its sleeve holds money (after founder-approved allocation), else ENGINEERING.
    With auto_allocate_founder (founder approval Oct 28, 2026), the Founder's free SETTLED cash is moved into the
    Founder sleeve first, up to its cap."""
    want = CFG.get("account", "FOUNDER")
    if want != "auto":
        return want
    today = today or datetime.now(timezone.utc).astimezone().date().isoformat()
    fst = ROOT / "state" / "intraday_state_FOUNDER.json"
    fs = json.loads(fst.read_text()) if fst.exists() else None
    if CFG.get("auto_allocate_founder"):
        from hbgtl.equity_ledger import Ledger
        eq = json.loads(EQUITY.read_text())
        led = Ledger.from_dict(eq["portfolios"]["FOUNDER"])
        led.settle(today)                                       # T+1: yesterday's sale proceeds are now settled
        eq["portfolios"]["FOUNDER"] = led.to_dict()
        EQUITY.write_text(json.dumps(eq, indent=1))
        room = CFG["accounts"]["FOUNDER"].get("max_usd", 100.0) - (fs["sleeve"]["allocated"] if fs else 0.0)
        amt = round(min(led.free_cash(), room), 2)
        if amt >= 1.0 and not led.positions:                    # only once the ETFs are gone
            _set_paths("FOUNDER")
            allocate(amt, "auto: founder-approved funding of the intraday sleeve (Oct 28, 2026)")
            fs = json.loads(fst.read_text())
    if fs and fs["sleeve"]["settled"] + sum(a for _, a in fs["sleeve"]["unsettled"]) >= CFG.get("founder_min_usd", 20):
        return "FOUNDER"
    return "ENGINEERING"


def _set_paths(acct: str):
    global ACCOUNT, ACCT, STATE, JOURNAL, REPORT
    ACCOUNT, ACCT = acct, CFG["accounts"][acct]
    STATE = ROOT / "state" / f"intraday_state_{acct}.json"
    JOURNAL = ROOT / "state" / f"intraday_journal_{acct}.jsonl"
    REPORT = ROOT / "dashboard" / f"intraday_daily_{acct}.json"


ACCOUNT = ACCT = STATE = JOURNAL = REPORT = None
_set_paths(CFG.get("account") if CFG.get("account") != "auto" else "FOUNDER")
KILL_FILE = ROOT / "KILL_SWITCH"


def note(title, msg, level="notice"):
    print(f"::{level} title={title}::{' '.join(str(msg).split())[:3500]}", flush=True)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    st = new_state()
    st["account"] = ACCOUNT
    seed = float(ACCT.get("seed_usd", 0))
    if seed > 0:   # simulated starting capital of this account (engineering account: $10,000)
        st["sleeve"].update(allocated=seed, settled=seed)
        st["sleeve"]["allocations"].append({"at": datetime.now(timezone.utc).isoformat(), "amount": seed,
                                            "reason": f"{ACCOUNT} account simulated starting capital"})
    return st


def save(state):
    STATE.write_text(json.dumps(state, indent=1, default=str))


def push(msg):
    """Commit state files and push (rebasing on any other commits). Never fatal."""
    try:
        subprocess.run(["git", "add", str(STATE), str(JOURNAL), str(REPORT), str(EQUITY)], cwd=ROOT, check=False,
                       capture_output=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
            return
        subprocess.run(["git", "commit", "-qm", msg], cwd=ROOT, check=True, capture_output=True)
        for _ in range(3):
            subprocess.run(["git", "pull", "-q", "--rebase", "-X", "theirs"], cwd=ROOT, capture_output=True)
            if subprocess.run(["git", "push", "-q"], cwd=ROOT, capture_output=True).returncode == 0:
                return
            time.sleep(3)
        note("State push failed", "state saved locally; will retry", "warning")
    except Exception as e:  # noqa: BLE001
        note("State push failed", e, "warning")


def journal(rec):
    with JOURNAL.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def allocate(amount: float, reason: str):
    """Move founder-approved SETTLED cash from the Founder's Account books into the intraday sleeve."""
    eq = json.loads(EQUITY.read_text())
    f = eq["portfolios"]["FOUNDER"]
    free = f["settled"] - f.get("owed_vault", 0)
    if amount <= 0 or amount > free + 1e-6:
        sys.exit(f"refused: allocation ${amount:.2f} exceeds the Founder's free settled cash ${free:.2f}")
    _set_paths("FOUNDER")
    st = load_state()
    if st["sleeve"]["allocated"] + amount > ACCT.get("max_usd", 100.0) + 1e-6:
        sys.exit("refused: the intraday sleeve is capped at $100 of founder capital")
    f["settled"] -= amount
    st["sleeve"]["allocated"] += amount
    st["sleeve"]["settled"] += amount
    st["sleeve"]["allocations"].append({"at": datetime.now(timezone.utc).isoformat(), "amount": amount,
                                        "reason": reason})
    EQUITY.write_text(json.dumps(eq, indent=1))
    save(st)
    note("Allocated", f"${amount:.2f} of Founder settled cash moved to the intraday sleeve ({reason})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="16:05")
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--allocate", type=float)
    ap.add_argument("--reason", default="")
    a = ap.parse_args()
    if a.allocate:
        return allocate(a.allocate, a.reason)
    _set_paths(select_account(pd.Timestamp.now(tz=ET).date().isoformat()))
    note("Account", f"{ACCOUNT} (config: {CFG.get('account')})")

    cfg = {**CFG, "order_prefix": ACCT.get("prefix", "id-"), "account": ACCOUNT}
    from hbgtl.intraday_gateway import AlpacaIntradayGateway
    gw = AlpacaIntradayGateway(cfg["universe"].get("feed", "iex"))
    state = load_state()
    if KILL_FILE.exists():
        state["kill_switch"] = True
    last_push = [time.time()]

    def persist():
        save(state)

    eng = IntradayEngine(cfg, gw, state, journal, persist)
    clock = gw.clock()
    now = pd.Timestamp.now(tz="UTC")
    today = now.tz_convert(ET).date().isoformat()
    if not clock["is_open"] and pd.Timestamp(clock["next_open"]).tz_convert(ET).date().isoformat() != today:
        note("Market closed", "no regular session today: nothing to do")
        return 0
    acct = gw.account()
    if acct["trading_blocked"] or acct["account_blocked"]:
        note("Broker blocked", acct, "error")
        state["kill_switch"] = True

    # ---- pre-market scan: eligibility from consolidated daily bars, RVOL profiles from IEX 5-minute bars
    pool = sorted(set(RESEARCH_POOL))
    daily = gw.daily_sip(pool, days=45)
    daily = {k: v[pd.to_datetime(v["timestamp"], utc=True).dt.tz_convert(ET).dt.date.astype(str) < today]
             for k, v in daily.items()}
    five = gw.bars(pool, datetime.now(timezone.utc) - timedelta(days=35), minutes=5)
    profiles = profiles_from_bars(five, today)
    scan = eng.start_day(today, pool, daily, profiles, gw.assets(pool))
    note("Scan", f"account {ACCOUNT} | {len(scan['eligible'])} of {len(pool)} eligible | mode {cfg.get('mode')} | execute "
                 f"{cfg.get('execute') or 'none (signal-only)'} | sleeve settled ${state['sleeve']['settled']:.2f}")
    _recover(eng, gw, state)
    persist()
    push(f"Intraday scan {today}")

    open_utc = pd.Timestamp(f"{today} 09:30", tz=ET).tz_convert("UTC")
    stop_at = pd.Timestamp(f"{today} {a.until}", tz=ET).tz_convert("UTC")
    fetched_from = open_utc
    last_pull = 0.0
    while True:
        if time.time() - last_pull > 300:                       # pick up founder changes made on main mid-session
            last_pull = time.time()
            _refresh_controls(eng, state)
        now = pd.Timestamp.now(tz="UTC")
        if now >= stop_at:
            break
        nxt = now.floor("min") + pd.Timedelta(seconds=66)            # ~6s after the minute closes
        if now < open_utc:
            nxt = max(nxt, open_utc + pd.Timedelta(seconds=66))
        time.sleep(max(1.0, (nxt - pd.Timestamp.now(tz="UTC")).total_seconds()))
        try:
            syms = sorted(set(eng.eligible) | ({state["position"]["symbol"]} if state["position"] else set()))
            new = gw.bars(syms, fetched_from.to_pydatetime() - timedelta(minutes=2))
            eng.update_bars(new)
            fetched_from = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=3)
            newest = max((df["t"].max() for df in eng.raw.values() if len(df)), default=None)
            clk_open = gw.clock()["is_open"]
            if clk_open and newest is not None and (pd.Timestamp.now(tz="UTC") - newest).total_seconds() > 60 + cfg["risk"]["max_bar_age_s"]:
                eng.note("stale_data", newest=str(newest))       # signals on stale bars are never traded
            out = eng.tick(pd.Timestamp.now(tz="UTC"), clk_open)
            if out["orders"] or time.time() - last_push[0] > 600:
                push(f"Intraday state {pd.Timestamp.now(tz=ET):%Y-%m-%dT%H:%M}")
                last_push[0] = time.time()
        except Exception as e:  # noqa: BLE001 - one bad minute must not kill the session
            eng.note("loop_error", message=f"{type(e).__name__}: {e}")
            note("Loop error", e, "warning")
            time.sleep(10)
    if a.final:
        _final(eng, gw, state)
    persist()
    push(f"Intraday {'final' if a.final else 'session'} {today}")
    return 0


def _refresh_controls(eng, state):
    """git pull, then re-read mode/execute and the KILL_SWITCH file. Only these controls change mid-session;
    risk limits and setup rules are fixed for the session."""
    try:
        save(state)
        subprocess.run(["git", "stash", "-q"], cwd=ROOT, capture_output=True)
        subprocess.run(["git", "pull", "-q", "--rebase"], cwd=ROOT, capture_output=True)
        subprocess.run(["git", "stash", "pop", "-q"], cwd=ROOT, capture_output=True)
        cfg = yaml.safe_load((ROOT / "config" / "intraday.yaml").read_text())
        mode, ex = cfg.get("mode", "signal"), set(cfg.get("execute") or [])
        new_ex = ex if mode == "paper" else set()
        if mode != eng.mode or new_ex != eng.execute:
            eng.note("controls", mode=mode, execute=sorted(new_ex), before={"mode": eng.mode, "execute": sorted(eng.execute)})
            note("Controls changed", f"mode {mode}, execute {sorted(new_ex) or 'none'}")
            eng.mode, eng.execute = mode, new_ex
        if KILL_FILE.exists() and not state.get("kill_switch"):
            state["kill_switch"] = True
            eng.note("risk", reason="KILL_SWITCH file present: no new entries")
    except Exception as e:  # noqa: BLE001
        note("Control refresh failed", e, "warning")


def _recover(eng, gw, state):
    """After a restart: make the books and the broker agree before doing anything else."""
    p = state.get("position")
    broker = gw.positions()
    etf_syms = set(json.loads(EQUITY.read_text())["portfolios"]["FOUNDER"]["positions"])
    orphans = [s for s in broker if s not in etf_syms and (p is None or s != p["symbol"])]
    if orphans:                                                 # unknown shares: never trade on top of them
        eng.day["halted"] = f"unknown broker positions {orphans}: founder review"
        eng.note("risk", reason=f"unknown broker positions {orphans}: no new entries today")
        note("Unknown positions", orphans, "warning")
    if p:
        eng.note("recover", position=p)


def _final(eng, gw, state):
    """Afternoon job end: nothing may be held overnight. Then write the daily report."""
    p = state.get("position")
    if p and p.get("status") in ("open", "exiting", "entering", "submitting"):
        o = gw.order(p["cid"]) or {"legs": [], "status": "unknown", "id": ""}
        eng._exit(p, o, pd.Timestamp.now(tz="UTC"), "final flatten (job end)")
        for _ in range(20):
            time.sleep(3)
            eng._manage_position(pd.Timestamp.now(tz="UTC"), 391)
            if state["position"] is None:
                break
    if gw.position_qty(p["symbol"]) if p else 0:
        note("Overnight position", f"{p['symbol']} still held after final flatten", "error")
    rep = eng.end_of_day_report()
    REPORT.write_text(json.dumps(rep, indent=1, default=str))
    note("Intraday daily report", f"PAPER | signals {rep['signals']} | trades {len(rep['trades'])} | realized "
         f"${rep['realized_pnl']:.2f} | settled ${rep['settled_cash']:.2f} | shadow "
         + ", ".join(f"{k} {v['trades']} trades {v['R']:+.2f}R" for k, v in rep["shadow"].items()))


if __name__ == "__main__":
    sys.exit(main())
