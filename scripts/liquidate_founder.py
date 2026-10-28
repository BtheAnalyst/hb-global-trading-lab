"""Founder-approved liquidation of the Founder's Account ETF holdings (Alpaca PAPER), so the $100 can fund the
intraday sleeve. Founder approval: Oct 28, 2026 ("yes sell that stuff, we need to be able to trade from the
founder's account"). Market hours only. Marketable fractional DAY limit sells with hb- client order ids;
fills are booked into the Founder ledger (proceeds unsettled until T+1). Reconciles against the broker after."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hbgtl.equity_gateway import AlpacaEquityGateway  # noqa: E402
from hbgtl.equity_ledger import Ledger  # noqa: E402

ET = ZoneInfo("America/New_York")
STATE = ROOT / "state" / "equity_state.json"
JOURNAL = ROOT / "state" / "equity_journal.jsonl"


def note(t, m, lvl="notice"):
    print(f"::{lvl} title={t}::{' '.join(str(m).split())[:3000]}", flush=True)


ap = argparse.ArgumentParser()
ap.add_argument("--reason", required=True)
a = ap.parse_args()
gw = AlpacaEquityGateway(limit_buffer=0.003)
if not gw.trading.get_clock().is_open:
    note("Market closed", "liquidation runs only during regular hours; nothing sent", "error"); sys.exit(1)
today = datetime.now(ET).date().isoformat()
st = json.loads(STATE.read_text())
led = Ledger.from_dict(st["portfolios"]["FOUNDER"])
broker = gw.positions()
ok = True
for sym in list(led.positions):
    q_books = led.qty(sym)
    qty = min(q_books, broker.get(sym, 0.0))
    if qty <= 0:
        note(f"{sym} skipped", f"books {q_books}, broker {broker.get(sym)}", "warning"); ok = False; continue
    cid = f"hb-FOUNDER-{today}-{sym}-liquidate"
    if cid in led.order_ids:
        note(f"{sym} skipped", "liquidation already sent today", "warning"); continue
    q = gw.quote(sym)
    qty, limit = gw.sell_terms(qty, q[0], q[1])
    led.order_ids.append(cid)
    STATE.write_text(json.dumps({**st, "portfolios": {**st["portfolios"], "FOUNDER": led.to_dict()}}, indent=1))
    r = gw.submit(cid, sym, "sell", qty, limit)
    fq, fp = float(r.get("filled_qty") or 0), r.get("filled_avg_price")
    if fq > 0 and fp:
        pnl = led.apply_sell(today, sym, fq * float(fp), fq, abs(float(fp) - q[0]) * fq, sym in led.sleeve_held, 0.30, True)
        with JOURNAL.open("a") as f:
            f.write(json.dumps({"t": today, "pid": "FOUNDER", "event": "fill", "symbol": sym, "side": "sell", "qty": fq,
                                "price": float(fp), "reason": "founder-approved liquidation: " + a.reason,
                                "pnl": round(pnl, 4)}) + "\n")
        note(f"Sold {sym}", f"{fq} @ {fp} (status {r['status']}), P&L ${pnl:.4f}")
    if r["status"] != "filled":
        note(f"{sym} not fully filled", r, "warning"); ok = False
led.sleeve_held = [s for s in led.sleeve_held if s in led.positions]
led.flags["etf_liquidated"] = {"on": today, "reason": a.reason}
st["portfolios"]["FOUNDER"] = led.to_dict()
STATE.write_text(json.dumps(st, indent=1))
after = gw.positions()
left = {s: after.get(s, 0) for s in ("VTI", "SPY", "QQQ") if after.get(s, 0) > 1e-9}
note("Founder after liquidation", f"books positions {led.positions or 'none'}; broker ETF positions {left or 'none'}; "
     f"settled ${led.settled:.2f}; unsettled (T+1) ${led.unsettled_total():.2f}; vault owed ${led.owed_vault:.2f}")
sys.exit(0 if ok and not left else 1)
