"""Reporting for the stock/ETF engine: dashboard tables, the Daily Letter and the Midday Check-in.

Reads state/equity_state.json (+ state/equity_journal.jsonl). No pandas, no network, so a plain Claude
session can build the emails from the saved books. Every number is SIMULATED (paper) and labelled so.
"""
from __future__ import annotations

import json
from datetime import date
from html import escape
from pathlib import Path

from .equity_ledger import Ledger

BENCH = "A_vti"
FOUNDER = "FOUNDER"
ORDER = ["FOUNDER", "A_vti", "B_90_10", "C_80_20", "D_70_30", "A_vti_vault", "B_90_10_vault", "C_80_20_vault",
         "D_70_30_vault"]
NAMES = {"FOUNDER": "Founder's Account", "A_vti": "A · 100% VTI (benchmark)", "B_90_10": "B · 90/10",
         "C_80_20": "C · 80/20", "D_70_30": "D · 70/30", "A_vti_vault": "A · 100% VTI · Vault",
         "B_90_10_vault": "B · 90/10 · Vault", "C_80_20_vault": "C · 80/20 · Vault", "D_70_30_vault": "D · 70/30 · Vault"}
KIND = {"FOUNDER": "Paper-broker fills · simulated money"}
SIM = "Simulated fills · simulated money"


def _sorted(pids):
    return sorted(pids, key=lambda p: ORDER.index(p) if p in ORDER else 99)


def portfolio_rows(state: dict, px: dict, as_of: str, tax_rate: float = 0.24) -> list[dict]:
    books = state.get("portfolios", {})
    out = []
    bench = Ledger.from_dict(books[BENCH]) if BENCH in books else None
    b_total = bench.total(px) if bench else None
    b_nav = bench.nav(px) if bench else None
    for pid in _sorted(books):
        led = Ledger.from_dict(books[pid])
        r = led.report(px, tax_rate)
        invested = sum(p["basis"] for p in led.positions.values())
        mv = sum(led.values(px).values())
        nav = led.nav(px)
        out.append({
            "portfolio": pid, "name": NAMES.get(pid, pid), "fills": KIND.get(pid, SIM), "as_of": as_of,
            "contributed": r["contributed"], "invested_capital": round(invested, 2), "market_value": round(mv, 2),
            "available_cash": round(led.free_cash(), 2), "unsettled_cash": r["unsettled_cash"],
            "realized_pnl": r["realized"], "unrealized_pnl": r["unrealized"], "tax_reserve_est": r["tax_reserve_est"],
            "vault": r["protected_vault"], "vault_owed": r["vault_owed"], "trading_equity": r["trading_equity"],
            "wealth": r["total_wealth"], "net_gain": r["net_gain"],
            "return_pct": round(nav - 1, 4), "vs_vti": round(r["total_wealth"] - b_total, 2) if b_total is not None else None,
            "vs_vti_pct": round(nav / b_nav - 1, 4) if b_nav else None,
            "drawdown": r["drawdown_from_high"], "sleeve": ", ".join(led.sleeve_held) or "T-bills",
            "sleeve_drawdown": r["sleeve_drawdown"], "trades": led.trades, "costs": r["costs"],
            "holdings": ", ".join(f"{s} {h['qty']:.4f}" for s, h in r["holdings"].items()) or "cash only",
            "kill_switch": bool(state.get("kill_switch") or led.flags.get("kill")),
            "breaker": bool(led.flags.get("breaker")),
            "simulated": True,
        })
    return out


def record_history(state: dict, px: dict, as_of: str) -> None:
    hist = [h for h in state.get("history", []) if h["date"] != as_of]
    for r in portfolio_rows(state, px, as_of):
        hist.append({"date": as_of, "portfolio": r["portfolio"], "wealth": r["wealth"], "contributed": r["contributed"],
                     "trading": r["trading_equity"], "vault": r["vault"], "realized": r["realized_pnl"],
                     "unrealized": r["unrealized_pnl"], "net_gain": r["net_gain"], "return_pct": r["return_pct"]})
    state["history"] = sorted(hist, key=lambda h: (h["date"], h["portfolio"]))


def journal_fills(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "fill":
            rows.append(e)
    return rows


def export_dashboard(state: dict, journal: Path, outdir: Path) -> dict:
    marks = state.get("marks", {})
    px, as_of = marks.get("px", {}), marks.get("date", "")
    summary = portfolio_rows(state, px, as_of) if px else []
    daily = state.get("history", [])
    fills = journal_fills(journal)
    trades = [{"date": f["t"], "portfolio": f["pid"], "name": NAMES.get(f["pid"], f["pid"]), "side": f["side"],
               "symbol": f["symbol"], "qty": round(float(f["qty"]), 6), "price": round(float(f["price"]), 4),
               "dollars": round(float(f["qty"]) * float(f["price"]), 2), "pnl": f.get("pnl"),
               "reason": f.get("reason", ""), "fills": KIND.get(f["pid"], SIM)} for f in reversed(fills)]
    vault = [{"date": h["date"], "portfolio": h["portfolio"], "balance": h["vault"]} for h in daily if h["vault"] > 0]
    stub = {"date": as_of or str(date.today()), "placeholder": True}
    tables = {"summary": summary or [stub | {"portfolio": "none", "name": "No pass yet"}],
              "daily": daily or [stub | {"portfolio": "none", "wealth": 0}],
              "trades": trades or [stub | {"symbol": "none yet", "reason": "No fills yet"}],
              "vault": vault or [stub | {"portfolio": "none", "balance": 0}]}
    outdir.mkdir(exist_ok=True)
    for k, rows in tables.items():
        (outdir / f"{k}.json").write_text(json.dumps(rows, indent=1))
    return tables


# ------------------------------------------------------------------ email
MOSS_DEEP, MOSS, SAGE_TINT = "#2F3A22", "#4A5A32", "#E8ECDD"
IVORY, PANEL, LINE, INK, MUTED = "#FAF6EE", "#FFFDF8", "#D5DCC6", "#2B1D17", "#6B5646"
GOLD, OXBLOOD, CREAM = "#C8A96A", "#5E1A22", "#F4ECDD"
SERIF = "Georgia, 'Times New Roman', serif"
SANS = "-apple-system, 'Helvetica Neue', Arial, sans-serif"


def _m(x) -> str:
    if x is None:
        return "—"
    x = round(float(x), 2) + 0.0
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def _s(x) -> str:
    return "—" if x is None else ("+" if round(float(x), 2) >= 0 else "") + _m(x)


def _c(x) -> str:
    return INK if x is None else (MOSS if x >= 0 else OXBLOOD)


def _label(t, color=MUTED):
    return (f'<div style="font:600 10px {SANS};letter-spacing:2px;text-transform:uppercase;color:{color};'
            f'margin:0 0 6px">{escape(t)}</div>')


def _row(k, v, color=INK):
    return (f'<tr><td style="padding:8px 0;border-bottom:1px solid {SAGE_TINT};font:13px {SANS};color:{INK}">{escape(k)}</td>'
            f'<td align="right" style="padding:8px 0;border-bottom:1px solid {SAGE_TINT};font:13px {SANS};color:{color};'
            f'white-space:nowrap">{escape(v)}</td></tr>')


def _card(inner):
    return (f'<tr><td style="padding:0 16px 14px"><div style="background:{PANEL};border:1px solid {LINE};border-radius:16px;'
            f'padding:16px 18px">{inner}</div></td></tr>')


def _shell(eyebrow, title, sub, cards):
    return (f'<!doctype html><html><body style="margin:0;background:{IVORY}">'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="background:{IVORY}"><tr><td align="center">'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px">'
            f'<tr><td style="background:{MOSS_DEEP};padding:24px 22px;border-radius:0 0 18px 18px">'
            f'<div style="font:600 10px {SANS};letter-spacing:3px;text-transform:uppercase;color:{GOLD}">{escape(eyebrow)}</div>'
            f'<div style="font:400 26px {SERIF};color:{CREAM};margin-top:8px">{escape(title)}</div>'
            f'<div style="font:13px {SANS};color:#D9CFB8;margin-top:6px">{escape(sub)}</div></td></tr>'
            f'<tr><td style="height:16px"></td></tr>{"".join(cards)}'
            f'<tr><td style="padding:4px 22px 26px;font:11px/1.5 {SANS};color:{MUTED}">SIMULATED RESULTS. Paper trading only: '
            f'no real money is invested and no real deposits are made. Contributions are credited on the books. '
            f'Stocks and ETFs only; no options, margin, leverage or short selling. Not financial advice.</td></tr>'
            f'</table></td></tr></table></body></html>')


def _founder_card(f):
    rows = "".join([
        _row("Total founder contributions", _m(f["contributed"])),
        _row("Invested capital (cost of holdings)", _m(f["invested_capital"])),
        _row("Market value of holdings", _m(f["market_value"])),
        _row("Available trading cash (settled)", _m(f["available_cash"])),
        _row("Cash settling (T+1)", _m(f["unsettled_cash"])),
        _row("Realized profit / loss", _s(f["realized_pnl"]), _c(f["realized_pnl"])),
        _row("Unrealized gain / loss", _s(f["unrealized_pnl"]), _c(f["unrealized_pnl"])),
        _row("Estimated tax reserve (24%, estimate)", _m(f["tax_reserve_est"])),
        _row("Protected Profit Vault", _m(f["vault"])),
        _row("Total combined wealth", _m(f["wealth"])),
        _row("Versus 100% VTI benchmark", _s(f["vs_vti"]), _c(f["vs_vti"])),
    ])
    return _card(f'{_label("Founder’s Account · " + f["fills"], MOSS)}'
                 f'<div style="font:400 34px {SERIF};color:{INK}">{escape(_m(f["wealth"]))}</div>'
                 f'<div style="font:13px {SANS};color:{MUTED};margin:2px 0 10px">{escape(_s(f["net_gain"]))} net gain on '
                 f'{escape(_m(f["contributed"]))} contributed · holdings: {escape(f["holdings"])}</div>'
                 f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')


def _compare_card(rows):
    head = (f'<tr><td style="font:600 10px {SANS};color:{MUTED};padding:6px 0">PORTFOLIO</td>'
            f'<td align="right" style="font:600 10px {SANS};color:{MUTED}">WEALTH</td>'
            f'<td align="right" style="font:600 10px {SANS};color:{MUTED}">VS VTI</td></tr>')
    body = "".join(
        f'<tr><td style="padding:7px 0;border-top:1px solid {SAGE_TINT};font:13px {SANS};color:{INK}">{escape(r["name"])}</td>'
        f'<td align="right" style="border-top:1px solid {SAGE_TINT};font:13px {SANS}">{escape(_m(r["wealth"]))}</td>'
        f'<td align="right" style="border-top:1px solid {SAGE_TINT};font:13px {SANS};color:{_c(r["vs_vti"])}">'
        f'{escape(_s(r["vs_vti"]) if r["portfolio"] != BENCH else "benchmark")}</td></tr>' for r in rows)
    return _card(f'{_label("Research portfolios · " + SIM, MOSS)}'
                 f'<p style="margin:0 0 8px;font:13px/1.5 {SANS};color:{INK}">Eight strategy and Vault combinations, each '
                 f'with its own $100 start and $100 a month. 80/20 is a research candidate, not a chosen winner.</p>'
                 f'<table width="100%" cellpadding="0" cellspacing="0">{head}{body}</table>')


def _today_card(state, today):
    fills = [e for e in journal_fills(Path(state.get("_journal", "state/equity_journal.jsonl"))) if e["t"] == today]
    f_fills = [e for e in fills if e["pid"] == FOUNDER]
    lines = "".join(_row(f'{e["side"].title()} {e["symbol"]}: {e.get("reason", "")}',
                         _m(float(e["qty"]) * float(e["price"]))) for e in f_fills) or _row("No Founder orders today", "")
    alerts = state.get("last_alerts", [])
    al = "".join(f'<p style="margin:6px 0 0;font:13px/1.5 {SANS};color:{OXBLOOD}">{escape(a)}</p>' for a in alerts)
    return _card(f'{_label("Today · Founder’s paper orders", MOSS)}<table width="100%" cellpadding="0" cellspacing="0">'
                 f'{lines}</table>{al}')


def daily_letter(state: dict, today: str, day_label: str) -> tuple[str, str, str]:
    """Returns (subject, html, markdown)."""
    marks = state.get("marks", {})
    rows = portfolio_rows(state, marks.get("px", {}), marks.get("date", today)) if marks.get("px") else []
    f = next((r for r in rows if r["portfolio"] == FOUNDER), None)
    research = [r for r in rows if r["portfolio"] != FOUNDER]
    tag = (f"Founder {_m(f['wealth'])} ({_s(f['vs_vti'])} vs VTI)" if f and f["vs_vti"] is not None
           else "Founder's Account not started" if not f else f"Founder {_m(f['wealth'])}")
    subject = f"The Daily Letter · {day_label} · {tag} · simulated"
    cards = ([_founder_card(f)] if f else [_card(_label("Founder’s Account") + f'<p style="font:13px {SANS}">'
                                                 'No paper pass has run yet.</p>')])
    cards += [_today_card(state, today)]
    if research:
        cards.append(_compare_card(research))
    html = _shell("HB Global · The Trading Atelier", "The Daily Letter",
                  f"{day_label} · marked at the {marks.get('date', '—')} close · SIMULATED", cards)
    md = [f"# The Daily Letter · {day_label}", "", "**SIMULATED RESULTS · paper trading only, no real money.**", ""]
    if f:
        md += ["## Founder's Account (paper-broker fills, simulated money)", "",
               "| Item | Amount |", "|---|---|"] + [f"| {k} | {v} |" for k, v in [
                   ("Total founder contributions", _m(f["contributed"])), ("Invested capital", _m(f["invested_capital"])),
                   ("Available trading cash", _m(f["available_cash"])), ("Realized P&L", _s(f["realized_pnl"])),
                   ("Unrealized P&L", _s(f["unrealized_pnl"])), ("Estimated tax reserve", _m(f["tax_reserve_est"])),
                   ("Protected Profit Vault", _m(f["vault"])), ("Total combined wealth", _m(f["wealth"])),
                   ("vs 100% VTI", _s(f["vs_vti"]))]] + [""]
    if research:
        md += ["## Research portfolios (simulated fills)", "", "| Portfolio | Wealth | vs VTI |", "|---|---|---|"]
        md += [f"| {r['name']} | {_m(r['wealth'])} | {_s(r['vs_vti']) if r['portfolio'] != BENCH else 'benchmark'} |"
               for r in research]
    for a in state.get("last_alerts", []):
        md.append(f"- ⚠️ {a}")
    return subject, html, "\n".join(md) + "\n"


def midday_email(state: dict, today: str, day_label: str) -> tuple[str, str] | None:
    if state.get("last_run") != today:
        return None
    marks = state.get("marks", {})
    rows = portfolio_rows(state, marks.get("px", {}), marks.get("date", "")) if marks.get("px") else []
    f = next((r for r in rows if r["portfolio"] == FOUNDER), None)
    cards = [_today_card(state, today)]
    if f:
        cards.append(_card(_label("Founder’s Account at the last close", MOSS) +
                           f'<table width="100%" cellpadding="0" cellspacing="0">'
                           f'{_row("Total combined wealth", _m(f["wealth"]))}'
                           f'{_row("Available trading cash", _m(f["available_cash"]))}'
                           f'{_row("Holdings", f["holdings"])}{_row("Sleeve", f["sleeve"])}</table>'))
    subject = f"Midday Check-in · {day_label} · simulated"
    return subject, _shell("HB Global · The Trading Atelier", "Midday Check-in",
                           f"{day_label} · this morning's pass · SIMULATED", cards)
