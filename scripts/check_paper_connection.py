"""Read-only check that the Alpaca PAPER keys work.

Places NO orders. Prints only non-secret facts. Exits non-zero on any problem.
Keys come from environment variables set by GitHub Secrets; they are never printed.
"""
import os
import sys

from alpaca.trading.client import TradingClient

key = os.environ.get("ALPACA_PAPER_KEY", "")
secret = os.environ.get("ALPACA_PAPER_SECRET", "")
if not key or not secret:
    sys.exit("FAIL: ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET secrets are not set.")
if not key.startswith("PK"):
    sys.exit("FAIL: this does not look like a PAPER key (paper keys start with 'PK'). Stopping for safety.")

client = TradingClient(key, secret, paper=True)  # paper=True is hard-coded: this script cannot touch a live account
acct = client.get_account()
clock = client.get_clock()

print("PASS: connected to Alpaca PAPER trading")
print(f"  account status: {acct.status}")
print(f"  paper cash: ${float(acct.cash):,.2f}")
print(f"  options trading level: {getattr(acct, 'options_trading_level', 'unknown')}")
print(f"  market open right now: {clock.is_open}")
print("  orders placed: 0 (this check is read-only)")
