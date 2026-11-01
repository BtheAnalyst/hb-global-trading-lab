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

## Evidence (Oct 28, 2026, stress costs 10 bps/side, R = multiples of planned risk)
| Setup | Universe | Trades | Expectancy | Out-of-sample | Status |
|---|---|---|---|---|---|
| A `ORB15_V1` | liquid pool $5-$100 | 27 | +0.10R | +0.20R (12) | **paper orders (forward test)**: only non-negative setup; sample too small to prove |
| B `PULLBACK_VWAP_V1` | liquid pool | 215 | -0.29R | -0.35R | signal-only |
| C `MOMO_CONT_V1` | liquid pool | 222 | -0.28R | -0.08R | signal-only |
| D `GAPGO_PB_V1` (Cameron) | gappers $2-$20 | 56 | -0.09R | +0.15R (13) | signal-only |
| D `GAPGO_PB_V1` | gappers $5-$20 | 39 | -0.32R | -0.54R (6) | signal-only (live band) |
| A `ORB15_V1` (Nov 1) | WIDE: every liquid U.S. stock $5-$100 | 185 | -0.24R | -0.27R (66) | **not used**: more trades (~7.5/week) but losing; $100 → $83 |
| A on gappers (SMB in play) | gappers | 2 | n/a | n/a | too few trades (gapper opening ranges are wider than 4%) |
Details: `research/intraday_backtest_results.json`, `research/gapper_backtest_results.json`. Not part of the test:
news catalysts and share float (no data source), which both Cameron and SMB treat as central.

## Nov 1, 2026: fixes tested (stress costs)
| Test | Trades | Expectancy | Out-of-sample | $100 account | Decision |
|---|---|---|---|---|---|
| A on ALL liquid U.S. stocks (1,906 names) | 185 | -0.24R (se 0.07) | -0.27R (66) | -$16.68 (fractional) | **rejected**: wide universe stays OFF |
| A on the stocks outside the 172 pool | 181 | -0.24R | -0.31R | -$17.14 | the pool's small positive result does not generalise |
| A on the 172 pool (same data build) | 25 | +0.06R | +0.03R (11) | whole -$0.87 / **fractional +$1.08** | unchanged; fractional sizing ON |
| A threshold RVOL 1.5 (pool) | 71 | -0.18R | -0.16R | -$2.93 | rejected |
Reading: on a broad, unbiased universe the opening-range breakout loses ~0.24R per trade after costs. The pool's
slightly positive result is most likely selection luck (the pool was chosen in October 2026). Setup A remains a
paper forward test on the pool only; it has NOT shown an edge.

## Strategy lab (Nov 1, 2026): 15 published and influencer strategies, one test
`research/strategy_lab.py` (rules and pass bar predeclared in its docstring; sources listed per strategy in the result
files). Long-only, flat by 15:50, stop on every trade, next-bar fills, in-sample/out-of-sample split, and a STRICTER
pass bar because many strategies were tried at once (t-stat >= 2.5, both halves positive, >= 30 out-of-sample trades,
profit factor > 1.1, and the $100 cash-account simulation must end above $100) at stress costs.

| Strategy | Data | Trades | No costs | Base costs | Stress costs | $100 becomes (stress) | Bar |
|---|---|---|---|---|---|---|---|
| ORB5_ZARATTINI | 170 liquid stocks | 216 | -0.3503R | -0.5561R | -0.7102R | $66.07 | fail |
| ORB30_ZARATTINI | 170 liquid stocks | 212 | 0.0248R | -0.2281R | -0.4187R | $78.34 | fail |
| RED_TO_GREEN | 170 liquid stocks | 34 | 0.0239R | -0.0377R | -0.0947R | $98.34 | fail |
| VWAP_RECLAIM | 170 liquid stocks | 185 | 0.0201R | -0.1902R | -0.3032R | $77.69 | fail |
| BULL_FLAG | 170 liquid stocks | 252 | -0.242R | -0.3853R | -0.518R | $72.28 | fail |
| ABCD | 170 liquid stocks | 99 | 0.0764R | 0.0166R | -0.0715R | $96.4 | fail |
| EMA9_VWAP_CROSS | 170 liquid stocks | 228 | -0.1015R | -0.3995R | -0.5466R | $76.66 | fail |
| HOD_BREAK_PM | 170 liquid stocks | 69 | 0.0996R | 0.0058R | -0.0616R | $97.82 | fail |
| OVERSOLD_BOUNCE | 170 liquid stocks | 186 | 0.141R | -0.2289R | -0.4378R | $87.23 | fail |
| SECOND_DAY | 170 liquid stocks | 5 | -1.0R | -1.0551R | -1.0997R | $97.45 | fail |
| GAP_FILL_LONG | 170 liquid stocks | 119 | -0.0759R | -0.2111R | -0.3543R | $84.53 | fail |
| ORB5_ETF | 14 ETFs | 773 | -0.0467R | -0.5082R | -0.7813R | $38.92 | fail |
| OVERSOLD_BOUNCE | 14 ETFs | 301 | -0.1617R | -0.6313R | -0.8934R | $65.46 | fail |
| GAP_FILL_LONG | 14 ETFs | 299 | -0.0123R | -0.3908R | -0.646R | $55.62 | fail |
| INTRADAY_MOM_LAST30 | 14 ETFs | 501 | 0.0025R | -0.093R | -0.1797R | $63.72 | fail |
| NOISE_BREAKOUT | 14 ETFs | 677 | -0.0339R | -0.3667R | -0.6003R | $40.6 | fail |
| VWAP_TREND_5M | 14 ETFs | 940 | 0.0584R | -0.3058R | -0.5539R | $42.09 | fail |

**Result: none passes; none shows a reliable edge even before costs** (best: VWAP trend on ETFs, +0.06R gross,
t = 1.0, which typical costs erase). The live engine keeps ORB15_V1 as its only paper-execution setup.
Files: `research/strategy_lab_results_intraday_data.json`, `research/strategy_lab_results_etf_data.json`.
