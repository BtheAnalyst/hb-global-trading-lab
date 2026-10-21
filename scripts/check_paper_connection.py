"""Read-only check that the Alpaca PAPER keys work.

Places NO orders. Prints only non-secret facts. Exits non-zero on any problem.
Keys come from environment variables set by GitHub Secrets; they are never printed.
Results are also emitted as GitHub annotations (::notice:: / ::error::) so they are
visible on the run summary page.
"""
import os
import sys


def fail(msg: str) -> None:
    print(f"::error title=Paper check failed::{msg}")
    sys.exit(f"FAIL: {msg}")


key = os.environ.get("ALPACA_PAPER_KEY", "").strip()
secret = os.environ.get("ALPACA_PAPER_SECRET", "").strip()
if not key:
    fail("ALPACA_PAPER_KEY secret is missing or empty (check the exact name).")
if not secret:
    fail("ALPACA_PAPER_SECRET secret is missing or empty (check the exact name).")
if not key.startswith("PK"):
    fail(f"ALPACA_PAPER_KEY does not start with 'PK' (starts with '{key[:2]}', length {len(key)}). "
         "Paper keys start with PK. Possibly the Secret was pasted into the Key slot, or a live key was used.")

try:
    from alpaca.trading.client import TradingClient
    client = TradingClient(key, secret, paper=True)  # paper=True hard-coded: cannot touch a live account
    acct = client.get_account()
    clock = client.get_clock()
except Exception as e:  # never echo the keys; exception text from Alpaca does not contain them
    fail(f"Alpaca rejected the connection: {type(e).__name__}: {str(e)[:200]} "
         f"(key length {len(key)}, secret length {len(secret)})")

summary = (f"Connected to Alpaca PAPER. Status {acct.status}; paper cash ${float(acct.cash):,.2f}; "
           f"options level {getattr(acct, 'options_trading_level', 'unknown')}; "
           f"market open now: {clock.is_open}; orders placed: 0")
print(f"::notice title=Paper check passed::{summary}")
print("PASS: " + summary)
