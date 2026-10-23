"""Stock/ETF engine entry point (runs inside GitHub Actions, where Alpaca is reachable).

Modes
  check : read-only. Fetches bars, asset flags (tradable/fractionable) and quotes; runs the engine on a
          COPY of the books and prints what it would do. Saves nothing, sends nothing.
  shadow: runs the shadow portfolios for today (simulated fills at live quotes) and saves the books.
          Allowed only by a manual run (--manual) until Gate 5 sets execution_enabled: true.
Broker-mode portfolios stay disabled in config until Gate 3 approval. Keys come from GitHub Secrets.
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
from hbgtl.equity_gateway import AlpacaEquityGateway, SimGateway  # noqa: E402
from hbgtl.equity_risk import RiskConfig, asset_allowed  # noqa: E402
from hbgtl.equity_runner import Engine, specs_from_cfg  # noqa: E402

ET = ZoneInfo("America/New_York")
STATE = ROOT / "state" / "equity_state.json"
JOURNAL = ROOT / "state" / "equity_journal.jsonl"


def note(title: str, msg: str, level: str = "notice") -> None:
    print(f"::{level} title={title}::{msg}")


def fetch_closes(gw: AlpacaEquityGateway, symbols: list[str], days: int = 420) -> pd.DataFrame:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    start = datetime.now(timezone.utc) - timedelta(days=int(days * 1.5))
    end = datetime.now(timezone.utc) - timedelta(minutes=20)        # free plan: SIP needs a 15-minute delay
    bars = gw.data.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start,
                                                   end=end, feed=DataFeed.SIP, adjustment=Adjustment.ALL)).data
    today = datetime.now(ET).date()
    cols = {s: pd.Series({pd.Timestamp(b.timestamp.astimezone(ET).date()): float(b.close)
                          for b in bars.get(s, []) if b.timestamp.astimezone(ET).date() < today}) for s in symbols}
    return pd.DataFrame(cols).sort_index()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["check", "shadow"], default="check")
    ap.add_argument("--manual", action="store_true", help="set by the manual (workflow_dispatch) run")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(ROOT / "config" / "equity.yaml"))
    if a.mode == "shadow" and not (cfg.get("execution_enabled") or a.manual):
        note("Blocked", "execution_enabled is false (Gate 5 not approved) and this was not a manual run", "warning")
        return 0
    specs = [s for s in specs_from_cfg(cfg) if s.enabled]
    if any(s.mode == "broker" for s in specs):
        note("Blocked", "a broker-mode portfolio is enabled; that requires Gate 3 approval", "error")
        return 1
    alp = AlpacaEquityGateway()
    syms = sorted({s for sp in specs for s in (sp.rule.core, sp.rule.defensive, *sp.mom.universe)})
    today = datetime.now(ET).date()
    cal = alp.trading.get_calendar(__import__("alpaca.trading.requests", fromlist=["GetCalendarRequest"])
                                   .GetCalendarRequest(start=today - timedelta(days=10), end=today))
    sessions = [c.date for c in cal]
    if not sessions or sessions[-1] != today:
        note("Market closed", f"{today} is not a trading session: nothing to do")
        if a.mode == "shadow":
            return 0
    prev_session = [d for d in sessions if d < today][-1]
    closes = fetch_closes(alp, syms)
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
    work = state if a.mode == "shadow" else copy.deepcopy(state)
    events = []
    eng = Engine(specs, RiskConfig.from_cfg(cfg.get("risk")), journal=events.append)
    shadow = SimGateway(quotes=lambda s: quotes.get(s), cost=0.0, assets=assets)   # real quotes: spread is real cost
    summ = eng.run_day(work, closes, len(closes) - 1, str(today), {"shadow": shadow})
    lines = []
    for pid, r in summ["portfolios"].items():
        o = ", ".join(f"{x['side']} {x['symbol']} ${x['dollars']:.2f}" for x in r["orders"]) or "no orders"
        rf = ", ".join(f"{x['symbol']}: {x['reason']}" for x in r["refused"])
        lines.append(f"{pid}: {o}" + (f" | refused {rf}" if rf else "") + (f" | {'; '.join(r['notes'])}" if r["notes"] else ""))
    note("Engine pass" + (" (dry: nothing saved)" if a.mode == "check" else ""), " || ".join(lines)[:3800])
    if summ["alerts"]:
        note("Alerts", " || ".join(summ["alerts"])[:3800], "warning")
    if a.mode == "shadow":
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(work, indent=1, default=str))
        with JOURNAL.open("a") as f:
            for e in events:
                f.write(json.dumps(e, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
