"""The Daily Letter (markdown), posted after the close.

Progress first, but every figure is exact and losses, rejections and alerts are always shown.
"""
from __future__ import annotations

from datetime import date

from . import state as st


def _m(x: float) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def _s(x: float) -> str:
    return ("+" if x >= 0 else "") + _m(x)


def daily_letter(cfg: dict, state: dict, today: date) -> str:
    vp = cfg["vault"]["vault_pct"]
    d = st.day(state, today)
    a = st.ledger(state, "A", vp)
    b = st.ledger(state, "B", vp)
    pos = state.get("position")
    unreal = round((pos.get("last_mid", pos["entry_price"]) - pos["entry_price"]) * 100 * pos["qty"], 2) if pos else 0.0
    start_eq = (d.get("start_equity") or {}).get("A", a.trading_equity)
    day_trades = [t for t in state["trades"] if t["date"] == today.isoformat()]
    realized_today = round(sum(t["net_pnl"] for t in day_trades), 2)
    all_pnls = [t["net_pnl"] for t in state["trades"]]
    wins_all = sum(1 for p in all_pnls if p > 0)
    wealth_a = a.total_project_wealth + unreal
    peak = max([a.high_water_mark, a.trading_equity])
    nxt = next((m for m in cfg["milestones"] if m > wealth_a), cfg["milestones"][-1])

    hi = []
    if d["wins"]:
        hi.append(f"{d['wins']} profitable trade{'s' if d['wins'] > 1 else ''} closed.")
    if d["vault_added"] > 0:
        hi.append(f"A new account high sent {_m(d['vault_added'])} to the Profit Vault.")
    if day_trades and d["violations"] == 0:
        hi.append("Every trade followed the risk rules.")
    if pos:
        hi.append(f"Holding 1 position: {pos['qty']} × {pos['symbol']} ({pos['strategy'].replace('_', ' ')}).")
    if not day_trades and not pos:
        hi.append("No setup met every rule today, so the Atelier stayed in cash on purpose. "
                  "That protects capital; it is not a gain.")

    rej = ", ".join(f"{n} {r}" for r, n in sorted(d["rejections"].items(), key=lambda kv: -kv[1])) or "none"
    alerts = d.get("alerts", [])
    lines = [
        "# The Daily Letter",
        f"**HB Global · The Trading Atelier** · {today:%A, %B %-d, %Y} · **Paper mode**",
        "",
        "## Highlights",
        *[f"- {h}" for h in hi],
        "",
        "## Profit Vault",
        f"**{_m(a.vault_balance)}** protected" + (f" (+{_m(d['vault_added'])} today)" if d["vault_added"] else ""),
        "",
        "## Milestones",
        f"Atelier A combined wealth {_m(wealth_a)} · next milestone {_m(nxt)} ({min(100, 100 * wealth_a / nxt):.0f}%)",
        "",
        "## Insight",
        "Rules-only baseline (Model A). The eight-member AI Council is not active yet; it will be added "
        "and must beat this baseline to stay.",
        f"Trades to date: {len(all_pnls)}" + (f" · {100 * wins_all / len(all_pnls):.0f}% won" if all_pnls else "")
        + " · too few to judge an edge (needs 30+ by day 90).",
        "",
        "## Financial snapshot (Atelier A, $10,000 engineering account)",
        "| | |", "|---|---|",
        f"| Trading account (realized) | {_m(start_eq)} → {_m(a.trading_equity)} |",
        f"| Open position, unrealized | {_s(unreal)} |",
        f"| Combined wealth (incl. vault + unrealized) | {_m(wealth_a)} |",
        f"| Today's realized P&L | {_s(realized_today)} |",
        f"| Net gain since start | {_s(wealth_a - a.total_contributions)} |",
        f"| Trades closed today | {d['wins'] + d['losses']} ({d['wins']} won, {d['losses']} lost) |",
        f"| Drawdown from peak (realized) | {_m(max(0.0, peak - a.trading_equity))} |",
        f"| Fees to date (estimated) | {_m(a.total_fees)} |",
        f"| Setups declined by the risk engine | {d['rejected']} ({rej}) |",
        "",
        "## Risks and notes",
        *([f"- ⚠️ {x}" for x in alerts] or ["- No alerts. No rule violations."]),
        *[f"- Closed {t['symbol']}: {_s(t['net_pnl'])} ({t['reason']})" for t in day_trades],
        "",
        "## Founder's Account (Portfolio B)",
        d.get("b_note", f"{_m(b.total_project_wealth)}, holding cash by design."),
        f"Contributed {_m(b.total_contributions)} · trading gain {_s(b.cumulative_net_gain)}.",
        "",
        "## Tomorrow",
        "Kill switch is **ON**: no new trades until you approve a restart." if state.get("kill_switch")
        else "Ready. The Atelier scans every 30 minutes from 10:00 AM to 3:30 PM ET.",
        "",
        "_Paper trading only. Sample size is small; nothing here is a forecast or financial advice._",
    ]
    return "\n".join(lines)
