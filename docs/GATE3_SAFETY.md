# Gate 3 safety verification: the exact settings in force before the first paper order

Source of truth: `config/equity.yaml` (the `risk:` block) and `hbgtl/equity_runner.py`. Each behavior below is covered by an automated test (`tests/test_equity.py`, 116 tests passing). Evidence for each setting: `docs/RISK_THRESHOLDS.md`.

| Control | Exact setting | What triggers it | What it does | Alert | Blocks new buys | Sells or closes positions | Pauses the system | How it restarts |
|---|---|---|---|---|---|---|---|---|
| Daily loss safeguard | `daily_loss_pct: 0.05` | The portfolio's contribution-adjusted value fell 5% or more from the prior close to the last close | No new sleeve ETF entries this session. Core buys with new cash continue | Yes | Sleeve entries only | No | No | Automatically after one session |
| Maximum account drawdown (hard, Council Oct 23) | `wealth_pause_dd: 0.20` (alert also at 0.20) | Contribution-adjusted unit value is 20% or more below its high (deposits buy units at the prior value, so they never count as gains or recovery) | **ALL new purchases paused**, core included. Deposits are credited and held as cash. Nothing is sold because of the pause; normal strategy sales may still happen | Yes | Yes, all buys | No | Purchases only | Founder approval (`resume`). It does not re-trip in the same episode; it re-arms once the account is back within 10% of its high |
| Momentum sleeve circuit breaker (hard) | `sleeve_breaker_dd: 0.25` | The sleeve's flow-adjusted value is 25% or more below its high | Sleeve ETFs are sold into BIL (T-bills) at the next pass. No sleeve entries until restart. The core is untouched | Yes | Sleeve entries | Yes: sleeve positions move to T-bills | Pauses the sleeve | Owner resume, logged with a reason; the sleeve high resets |
| Maximum position exposure (hard) | Core: target + 5 points (`core_cap_margin: 0.05`). Each sleeve ETF: 1.5 × slot target (`etf_cap_mult: 1.5`). BIL: sleeve weight + 5 points | A buy would push a holding above its cap. For the Founder's Account (80/20) the caps are VTI 85%, each sleeve ETF 15%, BIL 25% | The buy is refused and logged. Price drift above a cap is reported, not sold | Yes (refusal) | That buy | No | No | Automatic. A repeated refusal is reviewed |
| Consecutive losing-trade alert | `losing_exit_alert: 6`, `losing_exit_pause: 0` (pause off) | 6 sleeve exits in a row closed at a loss, then each further loss | Owner review alert | Yes | No | No | No | n/a. The pause can be switched on with one setting if the Council chooses |
| Data-quality failures (hard) | Quote age over 60 s (`max_quote_age_s`, broker orders). Spread over 1% (`max_spread_pct`). Price more than 25% from the last close (`max_move_pct`). A missing close for any universe ETF. Last completed bar not equal to the previous session. Price fetch failure | Any of these | Bad quote or spread: that order is refused. Missing close: hold, no sleeve changes. Stale bars or fetch failure: the whole run stops before deciding, and nothing changes | Yes | Yes | No | That run, or that day | Automatically at the next good run |
| Broker reconciliation failure (hard) | `recon_tolerance: 0.50` | A broker share count differs from the Founder's books by more than $0.50, or the broker holds an unknown symbol | **Account-wide kill switch ON before any order** | Yes | Yes, all | No | Yes, every portfolio | A clean reconciliation, then an owner resume (`resume`, pid `GLOBAL`) |
| Order failures (hard) | 2 rejected or failed orders without a successful fill in between | Broker rejections or errors | That portfolio's kill switch turns ON | Yes | Yes | No | That portfolio | Owner resume (`resume`, pid `FOUNDER`) |
| Emergency kill switch (hard) | A `KILL_SWITCH` file in the repo, or `kill_switch: true` in the books | Owner or Council action, or a reconciliation failure | No orders from any portfolio. Books and reports keep updating | Yes | Yes, all | No (the owner decides any sale) | Yes | Remove the file or clear the flag, plus an owner resume |

Two limits worth stating plainly:
- **No control is guaranteed protection against market losses.** Prices can gap overnight before any order can be filled.
- **The 20% account-drawdown control pauses purchases; it never sells.** (Council decision, Oct 23, 2026.) Selling a broad-market core after a 20–35% fall has historically locked in losses: VTI fell 35% in 2020 and recovered within months.
  - The hard stops that do exist are the sleeve breaker, the exposure caps, the data and reconciliation checks, and the kill switch.
  - If the Council wants an account-level hard stop (for example, pause all new buys at 30%), it is a one-setting addition. It would be tightening, not loosening, and it is not built until the Council asks.

**Change from the draft plan (Council condition):** the draft's 4-losing-exits sleeve pause became a 6-loss alert.
- Historical support:
  - 85 sleeve exits with a 55% win rate.
  - Winners averaged +$78.81; losers averaged −$27.94.
  - A 4-loss pause would have tripped 4 times and cut off the recoveries.
  - Streak length swung from 5 to 13 with a one-day timing shift.
- Independent hard-loss protections still in force: the sleeve breaker, the exposure caps, the data checks, reconciliation and the kill switch.
- **No hard-loss setting was loosened.** The thresholds are unchanged since the Gate 2 report.

## Portfolio isolation

- **Nine portfolios, nine separate books.**
  - Each has its own cash, settled and unsettled balances, contributions, positions, cost basis, Profit Vault, high-water marks, tax estimate and order IDs.
  - They share nothing except market prices.
  - Each starts at $100 and adds $100 a month on its own books.
- **The 8 research portfolios are simulated.** They are 100% VTI, 90/10, 80/20 and 70/30, each with full reinvestment and with the 70/30 Vault. Their fills are calculated at live quotes, and no order leaves the bot.
- **Exactly one portfolio can reach the paper broker: `FOUNDER`.**
  - It runs 80/20 with the 70/30 Vault, chosen because it exercises every order type: core, sleeve entries and exits, T-bills and vault payments. This is not a strategy endorsement.
  - The code refuses to start if a second portfolio is set to broker mode, or if the broker portfolio is not the one named in `broker_portfolio`.
  - Order IDs carry the portfolio's name (`hb-FOUNDER-…`). Recovery and reconciliation look only at the Founder's own orders and positions.
  - The research portfolio "C · 80/20 · Vault 70/30" is a separate simulated book with the same rules. Comparing it with the Founder's Account shows the real-versus-simulated execution gap.
- **Founder's capital limit.**
  - Every buy is capped at the Founder's own free settled cash, re-checked per order, with a running total inside each pass.
  - The paper account's ~$100,000 cash and buying power are never read for sizing.
  - Sells are capped at the smaller of the shares on the books and the shares at the broker, so the bot cannot sell short.
  - Tests use a pretend broker with $200,000 of buying power and check that the Founder never spends beyond its books.
  - Broker-side backstops (`harden` mode): shorting off, margin multiplier 1, options level 0.
- **No margin, options, futures, short selling, leveraged or inverse ETFs, or real money.** These are enforced by the allowlist, the leveraged/inverse screen, the option-symbol refusal and the paper-only client. Orders are fractional or whole-share **limit DAY orders** only. Anything unfilled after about 30 seconds is cancelled; nothing is left working.

## Gate 3 run procedure (manual only; no schedule)

1. **Once:** a `harden` run sets the paper-account backstops. Then a `check` run, which is read-only.
2. **Each trading day, after 9:45 ET:** one manual `paper` run. The bot refuses to run before 9:45, when the market is closed, or when data is stale.
3. **Every run:**
   - finish or recover the Founder's earlier orders;
   - reconcile with the broker;
   - decide;
   - check risk;
   - save the books;
   - send orders;
   - commit the books even if the run fails.
4. **A same-day rerun sends nothing new.**
