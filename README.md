# HB Global Trading Lab (paper trading only)

Live trading is blocked in code. Two simulated portfolios, kept apart:
- **A**: $10,000 engineering account (tests the software; its buying power says nothing about B)
- **B**: $100 start + $100/month (Haley's real funding plan)

## Modules
| File | What it does | Status |
|---|---|---|
| config/settings.yaml | Portfolios, risk limits, 70/30 vault, alerts, fee estimate | done |
| hbgtl/risk.py | Hard-veto risk engine, incl. fees in max loss, deposit-proof daily limit, loss alerts | tested |
| hbgtl/vault.py | Profit Vault with cash-flow-adjusted high-water mark, cents accounting | tested |
| hbgtl/portfolio.py | Portfolios A and B, idempotent monthly deposits | tested |
| hbgtl/store.py | SQLite: idempotent orders, append-only audit log | tested |
| hbgtl/broker.py | Simulated broker (tested) + Alpaca paper broker (written, NOT yet run against Alpaca) | partial |
| hbgtl/metrics.py | Money-weighted return (XIRR), max drawdown, profit factor, expectancy | tested |
| hbgtl/reports.py | Daily report: progress first, deposits never shown as gains, violations never hidden | tested |
| hbgtl/projections.py | 365-day scenarios, required return, vault-split test, buy-and-hold benchmark | tested |
| hbgtl/feasibility.py | Original 100-day math | tested |

## Run
    pip install -r requirements.txt
    python -m pytest -q          # 38 tests
    python -m hbgtl.projections

## Not built yet
Market data feed, scanner, strategy signals, position monitor, AI Council, dashboard,
alert delivery, cloud schedule. API keys go in environment secrets, never in this folder.
