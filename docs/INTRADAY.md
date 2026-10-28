# Intraday sleeve (Oct 28, 2026 directive) — PAPER ONLY

## Why
The Founder's Account was running an 80/20 ETF allocation (buy-and-hold style). The founder's mandate is an
intraday, candlestick-driven, long-only day-trading system. The ETF strategy is now **frozen for the Founder's
Account** (`orders_frozen: true` in `config/equity.yaml`): no buys, and no sells without founder approval.
Holdings, history, marks, reconciliation, drawdown pause and kill switch are unchanged.

## Pieces
| File | Role |
|---|---|
| `hbgtl/candles.py` | Completed-candle features: VWAP, EMA, ATR, engulfing/hammer/shooting star/doji/inside bar, 5-min candles, HH/HL structure, relative volume vs prior-session profile, support/resistance levels |
| `hbgtl/setups.py` | `ORB15_V1` (A), `PULLBACK_VWAP_V1` (B), `MOMO_CONT_V1` (C): entry, stop, 2R target, invalidation; fixed parameters |
| `hbgtl/intraday_sim.py` | One trade simulator used by BOTH the backtest and the live shadow book |
| `hbgtl/intraday_engine.py` | Scan, signals, risk checks, bracket orders, monitoring, exits, T+1 cash books, shadow book, daily report |
| `hbgtl/intraday_gateway.py` | Alpaca PAPER (hard-coded); whole-share bracket orders; IEX real-time data; SIP daily bars for volume |
| `scripts/run_intraday.py` | Session loop (morning job to 12:45 ET, afternoon job to 16:05 ET with flatten + report) |
| `research/intraday_backtest.py` | Predeclared backtest, in-sample / out-of-sample, base and stress costs, $100 cash-account simulation |
| `.github/workflows/intraday-engine.yml` | Long-running session jobs (GitHub cron is hours late here, so the job keeps its own clock) |

## Accounts (never mixed)
* `ENGINEERING` — $10,000 simulated (Portfolio A). **Active** per founder instruction Oct 28: "trade out of the
  larger account today and tomorrow and until we're able to rectify the founder account."
* `FOUNDER` — $100 cap, funded only by `run_intraday.py --allocate <usd> --reason ...` from the Founder's settled
  cash. Currently ~$0.20 settled (the rest is in VTI/SPY/QQQ), so it cannot trade until the founder approves a sale
  and the proceeds settle (T+1).

## Rules (config/intraday.yaml)
0.5% planned risk/trade · −1% daily loss stop · 1 position · ≤3 entries/day · long only · whole shares ·
settled cash only (sale proceeds usable next business day) · no entries after 15:00 · flatten 15:50 · no
overnight · fresh bars (≤150 s) and quote (≤30 s) · IEX spread ≤0.3% · no chasing beyond signal + 0.25R ·
bracket stop at the broker, position closed at once if the stop leg is not live · 2 broker errors → kill switch ·
`KILL_SWITCH` file or `kill_switch: true` in state stops entries.

## Activation (predeclared)
A setup may be listed in `execute` only if, at **stress costs (10 bps per side)**: in-sample expectancy > 0,
out-of-sample expectancy > 0, ≥30 out-of-sample trades, out-of-sample profit factor > 1.1. Strategies are never
rewritten by the bot; any change goes through the backtest and tests again.

## Known limits
* Live data is IEX only (the subscription refuses real-time SIP); the backtest used SIP minute bars. IEX prices
  track the national market closely for liquid names, but IEX volume is a small slice, so relative volume is
  computed IEX-vs-IEX live and SIP-vs-SIP in research.
* The candidate pool is chosen today (mild survivorship bias in the backtest).
* Stops are stop-market orders: gaps and fast markets can fill worse than the stop.
* Bracket legs are DAY orders: if both session jobs died, the afternoon job's `always()` start and the 15:50
  flatten are the backstop; an unexpected overnight position is reported as an error.
