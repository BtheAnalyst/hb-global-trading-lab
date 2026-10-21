"""The Daily Letter as a polished HTML email (Atelier palette, email-safe inline styles).

Same numbers as letter.py; only the presentation differs. Progress first, but losses,
declined setups and alerts always appear.
"""
from __future__ import annotations

from datetime import date
from html import escape

from . import state as st
from .explain import explain_trade, market_read, terms_for_today

MOSS_DEEP, MOSS, SAGE, SAGE_TINT = "#2F3A22", "#4A5A32", "#B9C8A4", "#E8ECDD"
IVORY, PANEL, LINE, INK, MUTED = "#FAF6EE", "#FFFDF8", "#D5DCC6", "#2B1D17", "#6B5646"
GOLD, OXBLOOD, CREAM = "#C8A96A", "#5E1A22", "#F4ECDD"
SERIF = "Georgia, 'Times New Roman', serif"
SANS = "-apple-system, 'Helvetica Neue', Arial, sans-serif"


def _m(x: float) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def _s(x: float) -> str:
    return ("+" if x >= 0 else "") + _m(x)


def _label(t: str, color: str = MUTED) -> str:
    return (f'<div style="font:600 10px {SANS};letter-spacing:2px;text-transform:uppercase;'
            f'color:{color};margin:0 0 6px">{escape(t)}</div>')


def _tile(k: str, v: str, note: str, vcolor: str = INK) -> str:
    return (f'<td width="50%" style="padding:6px" valign="top"><div style="background:{SAGE_TINT};border-radius:14px;padding:14px">'
            f'{_label(k)}<div style="font:400 24px {SERIF};color:{vcolor}">{escape(v)}</div>'
            f'<div style="font:12px {SANS};color:{MUTED};margin-top:2px">{escape(note)}</div></div></td>')


def _row(k: str, v: str, color: str = INK, last: bool = False) -> str:
    b = "" if last else f"border-bottom:1px solid {SAGE_TINT};"
    return (f'<tr><td style="padding:9px 0;{b}font:13px {SANS};color:{INK}">{escape(k)}</td>'
            f'<td align="right" style="padding:9px 0;{b}font:13px {SANS};color:{color};white-space:nowrap">{escape(v)}</td></tr>')


def _why_block(paras: list[str]) -> str:
    return "".join(f'<p style="margin:0 0 10px;font:14px/1.6 {SANS};color:{INK}">{escape(x)}</p>' for x in paras)


def _terms_block(terms) -> str:
    rows = "".join(f'<div style="margin:0 0 10px"><div style="font:400 16px {SERIF};color:{MOSS}">{escape(k)}</div>'
                   f'<div style="font:13px/1.5 {SANS};color:{INK}">{escape(v)}</div></div>' for k, v in terms)
    return (f'<div style="background:{SAGE_TINT};border-radius:14px;padding:14px 16px">'
            f'{_label("Speak the language", MOSS)}{rows}</div>')


def daily_email(cfg: dict, state: dict, today: date) -> tuple[str, str]:
    """Returns (subject, html)."""
    vp = cfg["vault"]["vault_pct"]
    d = st.day(state, today)
    a, b = st.ledger(state, "A", vp), st.ledger(state, "B", vp)
    pos = state.get("position")
    unreal = round((pos.get("last_mid", pos["entry_price"]) - pos["entry_price"]) * 100 * pos["qty"], 2) if pos else 0.0
    start_eq = (d.get("start_equity") or {}).get("A", a.trading_equity)
    day_trades = [t for t in state["trades"] if t["date"] == today.isoformat()]
    realized_today = round(sum(t["net_pnl"] for t in day_trades), 2)
    pnls = [t["net_pnl"] for t in state["trades"]]
    wealth = a.total_project_wealth + unreal
    nxt = next((m for m in cfg["milestones"] if m > wealth), cfg["milestones"][-1])
    pct = min(100, 100 * wealth / nxt)
    alerts = d.get("alerts", [])

    if d["wins"] and not d["losses"]:
        headline = "A disciplined, profitable day."
    elif day_trades and realized_today >= 0:
        headline = "A positive day, rules intact."
    elif day_trades:
        headline = "A measured day: a loss, kept within its limits."
    elif pos:
        headline = "A position is working; patience is the plan."
    else:
        headline = "Discipline held: the Atelier stayed in cash."

    hi = []
    if d["wins"]:
        hi.append(f"{d['wins']} profitable trade{'s' if d['wins'] > 1 else ''} closed.")
    if d["vault_added"] > 0:
        hi.append(f"A new account high sent {_m(d['vault_added'])} to the Profit Vault.")
    if day_trades and d["violations"] == 0:
        hi.append("Every trade followed the risk rules.")
    if pos:
        hi.append(f"Holding {pos['qty']} × {pos['symbol']}.")
    if not day_trades and not pos:
        hi.append("No setup met every rule today, so capital stayed protected. That is discipline, not a gain.")
    hi_html = "".join(f'<li style="margin:0 0 6px">{escape(h)}</li>' for h in hi)

    pnl_color = MOSS if realized_today > 0 else OXBLOOD if realized_today < 0 else INK
    rej = ", ".join(f"{n} {r}" for r, n in sorted(d["rejections"].items(), key=lambda kv: -kv[1])) or "none"
    notes = [f'<div style="font:13px {SANS};color:{OXBLOOD};margin:0 0 6px">⚠ {escape(x)}</div>' for x in alerts]
    notes += [f'<div style="font:13px {SANS};color:{INK};margin:0 0 6px">Closed {escape(t["symbol"])}: '
              f'<span style="color:{MOSS if t["net_pnl"] > 0 else OXBLOOD}">{escape(_s(t["net_pnl"]))}</span> '
              f'<span style="color:{MUTED}">({escape(t["reason"])})</span></div>' for t in day_trades]
    if not notes:
        notes = [f'<div style="font:13px {SANS};color:{MUTED}">No alerts. No rule violations.</div>']
    why = [explain_trade(t) for t in day_trades]
    if pos:
        why.append("Still holding: " + explain_trade(pos))
    why += market_read(d)
    if not why:
        why = ["The bot hasn't scanned yet today."]
    terms = terms_for_today(d, day_trades, bool(pos), today)
    tomorrow = ("Kill switch is ON: no new trades until you approve a restart." if state.get("kill_switch")
                else "Ready. The Atelier scans every 30 minutes from 10:00 AM to 3:30 PM ET.")

    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:{IVORY}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{IVORY}"><tr><td align="center" style="padding:20px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:{PANEL};border:1px solid {LINE};border-radius:20px;overflow:hidden">
<tr><td style="background:{MOSS_DEEP};padding:28px 26px 26px;color:{CREAM}">
  <table role="presentation" width="100%"><tr>
    <td><div style="font:400 20px {SERIF};color:{CREAM}">HB Global</div>
        <div style="font:600 10px {SANS};letter-spacing:3px;text-transform:uppercase;color:{GOLD}">The Trading Atelier</div></td>
    <td align="right"><span style="font:600 10px {SANS};letter-spacing:2px;text-transform:uppercase;color:{SAGE};border:1px solid #7D8F5C;border-radius:999px;padding:5px 10px">Paper</span></td>
  </tr></table>
  <div style="font:400 30px {SERIF};color:{CREAM};margin:22px 0 4px">The Daily Letter</div>
  <div style="font:13px {SANS};color:#BFAF95">{today:%A, %B %-d, %Y}</div>
  <div style="font:italic 20px {SERIF};color:{SAGE};margin:18px 0 0">{escape(headline)}</div>
</td></tr>
<tr><td style="padding:22px 26px 6px">
  {_label("Highlights", MOSS)}
  <ul style="margin:0;padding-left:18px;font:14px/1.55 {SANS};color:{INK}">{hi_html}</ul>
</td></tr>
<tr><td style="padding:12px 20px 4px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
  {_tile("Combined wealth", _m(wealth), "Atelier A, vault included")}
  {_tile("Profit Vault", _m(a.vault_balance), ("+" + _m(d["vault_added"]) + " today") if d["vault_added"] else "protected", MOSS)}
</tr><tr>
  {_tile("Today's realized P&L", _s(realized_today), f"{d['wins']} won · {d['losses']} lost", pnl_color)}
  {_tile("Trades to date", str(len(pnls)), "needs 30+ to judge an edge")}
</tr></table></td></tr>
<tr><td style="padding:14px 26px 4px">
  {_label("Milestone", MOSS)}
  <div style="font:400 18px {SERIF};color:{INK};margin:0 0 8px">Next: {_m(nxt)} <span style="font:13px {SANS};color:{MUTED}">· {pct:.0f}% of the way</span></div>
  <div style="height:6px;background:{SAGE_TINT};border-radius:6px"><div style="height:6px;width:{pct:.1f}%;background:{MOSS};border-radius:6px"></div></div>
</td></tr>
<tr><td style="padding:20px 26px 4px">
  {_label("Why the bot did what it did", MOSS)}
  {_why_block(why)}
</td></tr>
<tr><td style="padding:14px 26px 4px">
  {_label("Financial snapshot")}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    {_row("Trading account (realized)", f"{_m(start_eq)} → {_m(a.trading_equity)}")}
    {_row("Open position, unrealized", _s(unreal), MOSS if unreal > 0 else OXBLOOD if unreal < 0 else INK)}
    {_row("Net gain since start", _s(wealth - a.total_contributions))}
    {_row("Drawdown from peak", _m(max(0.0, a.high_water_mark - a.trading_equity)))}
    {_row("Fees to date (estimated)", _m(a.total_fees))}
    {_row("Setups declined by the risk engine", f"{d['rejected']} ({rej})", INK, last=True)}
  </table>
</td></tr>
<tr><td style="padding:18px 26px 4px">
  {_label("Risks and notes", OXBLOOD if alerts else MUTED)}
  {''.join(notes)}
</td></tr>
<tr><td style="padding:18px 26px 4px">
  <div style="background:{SAGE_TINT};border-radius:14px;padding:14px 16px">
    {_label("Founder's Account", MOSS)}
    <div style="font:13px/1.5 {SANS};color:{INK}">{escape(d.get("b_note", f"{_m(b.total_project_wealth)}, holding cash by design."))}</div>
    <div style="font:12px {SANS};color:{MUTED};margin-top:6px">Contributed {_m(b.total_contributions)} · trading gain {_s(b.cumulative_net_gain)}</div>
  </div>
</td></tr>
<tr><td style="padding:18px 26px 4px">{_terms_block(terms)}</td></tr>
<tr><td style="padding:20px 26px 26px">
  <div style="border-top:1px solid {GOLD};padding-top:14px;text-align:center">
    <div style="font:400 17px {SERIF};color:{INK}">Tomorrow</div>
    <div style="font:12px {SANS};color:{MUTED};margin-top:4px">{escape(tomorrow)}</div>
    <div style="font:11px {SANS};color:{MUTED};margin-top:14px">Paper trading only. Small sample; not a forecast or financial advice.</div>
  </div>
</td></tr>
</table></td></tr></table></body></html>"""
    subject = f"The Daily Letter · {today:%a %b %-d} · {headline.rstrip('.')}"
    return subject, html


def midday_email(cfg: dict, state: dict, now) -> tuple[str, str] | None:
    """A short Midday Check-in from the bot's saved state. Returns None when the bot
    has not run during market hours today (weekend, holiday), so nothing is sent."""
    today = now.date()
    stamp = today.isoformat()
    runs = [e for e in state.get("log", []) if e.startswith(stamp)]
    active = [e for e in runs if "market closed" not in e]
    if not active:
        return None
    vp = cfg["vault"]["vault_pct"]
    d = st.day(state, today)
    a = st.ledger(state, "A", vp)
    pos = state.get("position")
    unreal = round((pos.get("last_mid", pos["entry_price"]) - pos["entry_price"]) * 100 * pos["qty"], 2) if pos else 0.0
    day_trades = [t for t in state["trades"] if t["date"] == stamp]
    realized = round(sum(t["net_pnl"] for t in day_trades), 2)
    pend = state.get("pending")

    if pos and unreal >= 0:
        headline = "A position is open and working."
    elif pos:
        headline = "A position is open, inside its limits."
    elif day_trades:
        headline = "A trade has already closed today."
    else:
        headline = "Watching patiently: no trade has met every rule yet."

    items = []
    if pos:
        items.append(f"Holding {pos['qty']} × {pos['symbol']} bought at ${pos['entry_price']:.2f}; "
                     f"last mark ${pos.get('last_mid', pos['entry_price']):.2f} ({_s(unreal)} unrealized).")
    for t in day_trades:
        items.append(f"Closed {t['symbol']}: {_s(t['net_pnl'])} ({t['reason']}).")
    if pend:
        items.append(f"An order is working: {pend['side']} {pend['symbol']}.")
    if d["rejected"]:
        items.append(f"The risk engine declined {d['rejected']} setup{'s' if d['rejected'] > 1 else ''} so far.")
    if not items:
        items.append("No setup has met every rule so far today. The Atelier keeps scanning until 3:30 PM.")
    items_html = "".join(f'<li style="margin:0 0 6px">{escape(x)}</li>' for x in items)
    why = ([("Why it's held: " + explain_trade(pos))] if pos else []) + market_read(d)
    terms = terms_for_today(d, day_trades, bool(pos), today, n=1)
    alerts = d.get("alerts", [])
    alert_html = "".join(f'<div style="font:13px {SANS};color:{OXBLOOD};margin:8px 0 0">⚠ {escape(x)}</div>' for x in alerts)
    last_run = active[-1][11:16]
    ucol = MOSS if unreal > 0 else OXBLOOD if unreal < 0 else INK
    rcol = MOSS if realized > 0 else OXBLOOD if realized < 0 else INK

    html = f"""<!doctype html><html><body style="margin:0;padding:0;background:{IVORY}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{IVORY}"><tr><td align="center" style="padding:20px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:{PANEL};border:1px solid {LINE};border-radius:20px;overflow:hidden">
<tr><td style="background:{MOSS};padding:22px 26px;color:{CREAM}">
  <div style="font:600 10px {SANS};letter-spacing:3px;text-transform:uppercase;color:{GOLD}">HB Global · The Trading Atelier</div>
  <div style="font:400 26px {SERIF};color:{CREAM};margin:12px 0 2px">Midday Check-in</div>
  <div style="font:13px {SANS};color:{SAGE}">{today:%A, %B %-d} · as of {last_run} ET · paper</div>
  <div style="font:italic 18px {SERIF};color:{CREAM};margin:14px 0 0">{escape(headline)}</div>
</td></tr>
<tr><td style="padding:16px 20px 4px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
  {_tile("Open position", _s(unreal) if pos else "None", "unrealized" if pos else "in cash", ucol)}
  {_tile("Realized today", _s(realized), f"{d['wins']} won · {d['losses']} lost", rcol)}
</tr><tr>
  {_tile("Combined wealth", _m(a.total_project_wealth + unreal), "Atelier A, vault included")}
  {_tile("Profit Vault", _m(a.vault_balance), "protected", MOSS)}
</tr></table></td></tr>
<tr><td style="padding:12px 26px 22px">
  {_label("So far today", MOSS)}
  <ul style="margin:0;padding-left:18px;font:14px/1.55 {SANS};color:{INK}">{items_html}</ul>
  {alert_html}
</td></tr>
<tr><td style="padding:0 26px 8px">
  {_label("Why", MOSS)}
  {_why_block(why)}
  {_terms_block(terms)}
  <div style="font:11px {SANS};color:{MUTED};margin-top:16px;border-top:1px solid {LINE};padding-top:12px">The full Daily Letter follows after the close. Paper trading only; not financial advice.</div>
</td></tr>
</table></td></tr></table></body></html>"""
    return f"Midday Check-in · {today:%a %b %-d} · {headline.rstrip('.')}", html
