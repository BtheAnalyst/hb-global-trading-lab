"""Render the intraday daily report (markdown) for the founder from dashboard/intraday_daily_<ACCOUNT>.json
and the account's journal. Output: intraday_report.md (posted as a GitHub issue by the workflow)."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config" / "intraday.yaml").read_text())
ACC = CFG.get("account", "FOUNDER")
if ACC == "auto":   # run_intraday picks FOUNDER or ENGINEERING per day: report the account with the newest daily report
    found = {p.stem.removeprefix("intraday_daily_"): json.loads(p.read_text())
             for p in (ROOT / "dashboard").glob("intraday_daily_*.json")
             if p.stem.removeprefix("intraday_daily_") in CFG.get("accounts", {})}
    if not found:
        sys.exit("no intraday daily report found")
    ACC = max(found, key=lambda a: (found[a].get("date", ""), a == "FOUNDER"))
rep = json.loads((ROOT / "dashboard" / f"intraday_daily_{ACC}.json").read_text())
st = json.loads((ROOT / "state" / f"intraday_state_{ACC}.json").read_text())
day = rep["date"]
jr = [json.loads(x) for x in (ROOT / "state" / f"intraday_journal_{ACC}.jsonl").read_text().splitlines() if x.strip()]
jr = [r for r in jr if r.get("date") == day]
sig = [r for r in jr if r["event"] == "signal" and not r.get("stale")]
risk = [r for r in jr if r["event"] in ("risk", "error", "stale_data", "loop_error")]
refused = Counter(r["reason"].split(":")[0] for r in jr if r["event"] == "entry_refused")
fe = json.loads((ROOT / "state" / "equity_state.json").read_text())["portfolios"]["FOUNDER"]

L = [f"**PAPER TRADING ONLY. Simulated results, not real money.** Account: **{ACC}**"
     + (" ($10,000 engineering account, used until the Founder's Account is rectified)" if ACC == "ENGINEERING" else ""),
     "", f"Mode: `{rep['mode']}` · setups allowed to place paper orders: {', '.join(rep['execute']) or 'none (signal-only)'}", "",
     "| | |", "|---|---|",
     f"| Stocks scanned / eligible | {rep['scanned']} / {rep['eligible']} |",
     f"| Setups detected (fresh signals) | {len(sig)} ({', '.join(f'{k} {v}' for k, v in Counter(r['setup'] for r in sig).items()) or 'none'}) |",
     f"| Paper trades entered / exited | {len(rep['trades'])} / {len(rep['trades'])} |",
     f"| Realized P&L today | ${rep['realized_pnl']:,.2f} |",
     f"| Settled cash / unsettled (T+1) | ${rep['settled_cash']:,.2f} / ${rep['unsettled_cash']:,.2f} |",
     f"| Sleeve equity | ${rep['sleeve_equity']:,.2f} (started with ${rep['allocated']:,.2f}) |",
     f"| Position held overnight | {'YES - check' if rep['position_open'] else 'none'} |",
     f"| Halted / kill switch | {rep['halted'] or 'no'} / {'ON' if rep['kill_switch'] else 'off'} |", ""]
if rep["trades"]:
    L += ["**Trades**", "", "| Symbol | Setup | Qty | Entry | Exit | P&L | R | Exit reason |", "|---|---|---|---|---|---|---|---|"]
    L += [f"| {t['symbol']} | {t['setup']} | {t['qty']:g} | {t['entry']:.2f} | {t['exit']:.2f} | ${t['pnl']:.2f} | "
          f"{t['R']} | {t['how']} |" for t in rep["trades"]]
    L.append("")
allt = st["trades"]
if allt:
    wins = [t for t in allt if t["pnl"] > 0]
    L += [f"**All paper trades to date ({ACC})**: {len(allt)} trades, win rate {len(wins) / len(allt):.0%}, "
          f"total ${sum(t['pnl'] for t in allt):,.2f}, average {sum(t['R'] or 0 for t in allt) / len(allt):+.2f}R", ""]
L += ["**Forward test (shadow book, every setup, virtual fills, 10 bps costs)**", "",
      "| Setup | Trades today | Result today |", "|---|---|---|"]
L += [f"| {k} | {v['trades']} | {v['R']:+.2f}R |" for k, v in rep["shadow"].items()]
L += ["", "**Rejected candidates (why qualifying setups were not traded)**", ""]
rj = sorted((rep.get("rejections") or {}).items(), key=lambda x: -x[1])[:12]
L += [f"- {k}: {v}" for k, v in rj] or ["- none"]
if refused:
    L += [f"- entry refused: {k} ({v})" for k, v in refused.most_common(8)]
L += ["", "**Risk events**", ""] + ([f"- {r['event']}: {r.get('reason') or r.get('message') or r.get('newest')}" for r in risk[:15]] or ["- none"])
L += ["", f"**Founder's Account ($100)**: ETF strategy frozen; holdings {', '.join(f'{s} {v['qty']:.6f}' for s, v in fe['positions'].items()) or 'none'}; "
      f"settled cash ${fe['settled']:.2f}. Intraday trading from it needs your approval to sell the ETFs (cash settles T+1)."]
Path("intraday_report.md").write_text("\n".join(L))
print("\n".join(L))
sys.exit(0)
