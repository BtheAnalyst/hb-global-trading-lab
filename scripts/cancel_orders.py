"""Cancel specific OPEN Alpaca PAPER orders by client order id (founder-approved action). Places no orders.
Usage: python scripts/cancel_orders.py <client_order_id> [...]"""
import os
import sys
import time

from alpaca.trading.client import TradingClient

tc = TradingClient(os.environ["ALPACA_PAPER_KEY"].strip(), os.environ["ALPACA_PAPER_SECRET"].strip(), paper=True)
bad = 0
for cid in [c for a in sys.argv[1:] for c in a.replace(",", " ").split()]:
    try:
        o = tc.get_order_by_client_id(cid)
    except Exception as e:  # noqa: BLE001
        print(f"::error title=Not found::{cid}: {e}"); bad += 1; continue
    st = str(o.status.value)
    if st in ("filled", "canceled", "expired", "rejected"):
        print(f"::warning title=Already final::{o.symbol} {o.side.value} {cid} is {st}; nothing to cancel"); continue
    tc.cancel_order_by_id(o.id)
    for _ in range(10):
        time.sleep(1)
        st = str(tc.get_order_by_client_id(cid).status.value)
        if st in ("canceled", "filled", "expired", "rejected"):
            break
    lvl = "notice" if st == "canceled" else "error"
    bad += lvl == "error"
    print(f"::{lvl} title=Cancel {o.symbol}::{o.side.value} {o.qty} {o.symbol} ({cid}) is now {st}")
for p in tc.get_all_positions():
    print(f"::notice title=Position::{p.symbol} {p.qty} (available {getattr(p, 'qty_available', '?')})")
sys.exit(1 if bad else 0)
