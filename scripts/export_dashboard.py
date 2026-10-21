"""Export the bot's saved state as small JSON tables for The Trading Atelier dashboard.

Writes dashboard/{summary,daily,trades,vault}.json. Reads state/state.json only.
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from hbgtl import state as st  # noqa: E402
from hbgtl.metrics import trade_stats  # noqa: E402
from hbgtl.vault import ProfitVault  # noqa: E402

cfg = yaml.safe_load(open("config/settings.yaml"))
vp = cfg["vault"]["vault_pct"]
state = st.load(cfg, date.today())
out = Path("dashboard")
out.mkdir(exist_ok=True)
names = {"A": "Atelier A", "B": "Founder's Account"}

# ---- daily wealth per portfolio (replayed from the ledger events) ----
dates = sorted(set([state["start_date"], *state["days"].keys(),
                    *[e["date"] for p in state["portfolios"].values() for e in p["events"]]]))
daily = []
for key, p in state["portfolios"].items():
    for d in dates:
        v = ProfitVault(p["starting_capital"], vp)
        for e in p["events"]:
            if e["date"] > d:
                continue
            if e["kind"] == "deposit":
                v.deposit(e["amount"], e["ref"])
            else:
                v.record_closed_trade(e["ref"], e["net_pnl"], e.get("fees", 0.0))
        daily.append({"date": d, "portfolio": key, "trading": v.trading_equity, "vault": v.vault_balance,
                      "wealth": v.total_project_wealth, "contributed": v.total_contributions})

# ---- summary per portfolio ----
summary = []
for key in state["portfolios"]:
    v = st.ledger(state, key, vp)
    pnls = [t["net_pnl"] for t in state["trades"]] if key == "A" else []
    ts = trade_stats(pnls)
    pos = state.get("position") if key == "A" else None
    unreal = round((pos.get("last_mid", pos["entry_price"]) - pos["entry_price"]) * 100 * pos["qty"], 2) if pos else 0.0
    wealth = round(v.total_project_wealth + unreal, 2)
    series = [r["wealth"] - r["contributed"] for r in daily if r["portfolio"] == key]
    peak, mdd, base = float("-inf"), 0.0, v.total_contributions
    for x in series:
        peak = max(peak, x)
        mdd = max(mdd, (peak - x) / base if base else 0)
    nxt = next((m for m in cfg["milestones"] if m > wealth), cfg["milestones"][-1])
    last_day = st.day(state, date.fromisoformat(dates[-1]))
    summary.append({
        "portfolio": key, "name": names.get(key, key),
        "wealth": wealth, "trading": v.trading_equity, "vault": v.vault_balance,
        "contributed": v.total_contributions, "net_gain": round(wealth - v.total_contributions, 2),
        "realized_pnl": v.realized_net_pnl, "unrealized_pnl": unreal, "fees": v.total_fees,
        "trades": ts["trades"], "wins": sum(1 for x in pnls if x > 0), "losses": sum(1 for x in pnls if x <= 0),
        "win_rate": round(ts["win_rate"], 4),
        "profit_factor": (None if ts["profit_factor"] in (float("inf"), 0.0) and not pnls else
                          (None if ts["profit_factor"] == float("inf") else round(ts["profit_factor"], 3))),
        "max_drawdown": round(mdd, 4), "next_milestone": nxt,
        "risk_per_trade": round(v.trading_equity * cfg["risk"]["max_risk_per_trade_pct"] / 100, 2),
        "open_position": pos["symbol"] if pos else "",
        "kill_switch": bool(state.get("kill_switch")),
        "declined_today": last_day["rejected"] if key == "A" else 0,
        "as_of": dates[-1],
    })

trades = [{"date": t["date"], "contract": t["symbol"], "underlying": t["underlying"],
           "type": "Call" if t["strategy"] == "long_call" else "Put", "qty": t["qty"],
           "entry": t["entry"], "exit": t["exit"], "net_pnl": t["net_pnl"], "reason": t["reason"]}
          for t in reversed(state["trades"])]

vault_rows, bal = [], 0.0
v = ProfitVault(state["portfolios"]["A"]["starting_capital"], vp)
for e in state["portfolios"]["A"]["events"]:
    ev = v.deposit(e["amount"], e["ref"]) if e["kind"] == "deposit" else v.record_closed_trade(e["ref"], e["net_pnl"], e.get("fees", 0.0))
    if ev.to_vault > 0:
        vault_rows.append({"date": e["date"], "added": ev.to_vault, "balance": ev.vault_balance})

# The dashboard can't read an empty file, so an empty table carries one marker row
# (placeholder: true) that the page hides. It holds no invented numbers.
as_of = dates[-1]
if not trades:
    trades = [{"date": as_of, "contract": "none yet", "type": "", "qty": 0, "entry": 0, "exit": 0,
               "net_pnl": 0, "reason": "No closed trades yet", "placeholder": True}]
if not vault_rows:
    vault_rows = [{"date": as_of, "added": 0, "balance": 0, "placeholder": True}]

for name, rows in {"summary": summary, "daily": daily, "trades": trades, "vault": vault_rows}.items():
    (out / f"{name}.json").write_text(json.dumps(rows, indent=1))
    print(f"dashboard/{name}.json: {len(rows)} rows")
