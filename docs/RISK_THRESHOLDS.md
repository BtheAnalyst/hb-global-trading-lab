# Risk thresholds: proposal and evidence (Gate 2, item 5)

Written **before** the thresholds were implemented, as the Council directed. The evidence comes from
`research/risk_evidence.py` → `research/risk_evidence.json`: Jan 2017 – Oct 2026, $100 start plus $100/month,
corrected v2 engine, dividend-adjusted daily prices. One history covering about ten years and one full bear market is a small sample.
Every number below is a configurable setting in `config/equity.yaml`, not a hard-coded rule.

## What a safeguard can and cannot do

The bot makes one decision pass a day on completed closes, and orders fill at the next session's prices.
**No daily pause can guarantee protection against market losses.**
- On VTI's 10 worst days since 2017, an average of **68% of the day's loss was already in the opening gap**, before any order could be filled.
- A pause cannot stop the value of holdings already owned from falling.
- These safeguards exist to stop the *bot* from compounding a problem (bad data, a bug, a runaway sleeve). They do not exist to stop the market.

## 1. Daily-loss safeguard: proposed at 5% of trading equity

| Evidence (portfolio C 80/20) | Value |
|---|---|
| Days down 3% or more | 22 in 2,463 |
| Days down 5% or more | 6 in 2,463 |
| Average next-day return after a 5%+ drop | **+2.7%** |
| Average next-day return, all days | +0.06% |

Rule:
- If trading equity closes 5% or more below the prior close, **new sleeve entries are blocked for the next session**.
- Nothing is sold because of the pause. Exits that the strategy's own signals call for still go through, and the core is not touched.
- Deposits are still credited and held as cash until the pause clears.

Why it is limited to that:
- The days right after large drops were, on average, among the best days.
- A pause that sold or froze the whole portfolio would historically have locked in losses and missed rebounds.
- The 5% level trips about once every 1.6 years, so it rarely interferes.

Restart: clears automatically after one session. It is logged and reported in the letter.

## 2. Position limits: target plus a margin, scaled to each strategy

| Highest weight reached on its own | B 90/10 | C 80/20 | D 70/30 |
|---|---|---|---|
| VTI (core) | 91.0% | 82.0% | 72.9% |
| Largest single sleeve ETF | 7.0% | 13.7% | 20.0% |

Fixed caps such as "VTI 90%, ETF 15%" would block portfolio B's normal holding and portfolio D's normal slot size. So the caps scale with each strategy's own targets:

- **Core hard cap = core target + 5 points** (at most 100%): 95% for B, 85% for C, 75% for D. None was breached in history.
- **Single sleeve ETF hard cap = 1.5 × slot target**: 7.5% for B, 15% for C, 22.5% for D. None was breached.
- **Universe allowlist only:**
  - No options, leveraged ETFs, inverse ETFs, margin or shorts.
  - Each asset must be tradable and fractionable (otherwise whole shares only).
  - At most 2 sleeve ETFs at a time.

These caps never bind when the strategy behaves as designed. If an order would breach one, something is wrong: a bug, a bad price or a reconciliation error. The order is refused, logged, and flagged for review.

## 3. Sleeve drawdown circuit breaker: proposed at 25%

| Evidence (sleeve alone, time-weighted) | Value |
|---|---|
| Worst sleeve drawdown | 20.7% (Dec 2022) |
| Episodes deeper than 15% | 2 |
| Episodes deeper than 20% | 1 |
| Episodes deeper than 25% | **0** |

Rule:
- If the sleeve falls 25% below its own contribution-adjusted high, the breaker trips.
- The sleeve's ETFs are sold into BIL (T-bills) at the next session.
- Sleeve entries stop until the owner reviews it. The core is untouched.

Why 25%:
- It sits beyond anything in this history, so it does not interfere with the strategy's normal ups and downs.
- When it does trip, conditions are outside what was tested, which is exactly when a human should look.
- To be clear, it would **not** have acted in 2022.
- A 20% level would have tripped once, near the 2022 low, which is historically about the worst moment to sell.

Restart: requires (a) an owner resume command, logged with a reason, and (b) a fresh signal pass. The sleeve's high-water mark resets to its value at restart, so it cannot re-trip immediately.

## 4. Losing-exit streaks: proposed as an alert, not a pause (changed from the draft plan)

| Evidence (80/20, 101 sleeve exits) | Value |
|---|---|
| Win rate | 50.5% |
| Average winning exit / average losing exit | +$65.82 / −$22.80 |
| Longest losing streak | 11 exits |
| Streaks of 4+ / 6+ losses | 4 / 3 |

The draft plan paused the sleeve after 4 losing exits in a row. The evidence argues against it:
- Momentum makes its money from a few large winners after runs of small losers.
- A 4-loss pause would have tripped 4 times and would likely have cut off the recoveries.

Proposal:
- After **6 consecutive losing exits**, send an owner review alert. There is no automatic pause.
- The drawdown breaker in section 3 remains the hard stop.
- The Council may still choose a pause. The setting exists, and it is off by default.

## 5. Total-wealth alert: 20% below the contribution-adjusted high

Portfolio C had 2 such episodes in ten years; VTI alone had 3. This setting only sends an alert, with a plain-English note in the letter. It never sells anything.

## 6. Data and order checks (operational)

- **Quotes:** for orders placed at broker prices, a quote older than 60 seconds is skipped.
- **Spreads:** a spread wider than 1% of the price skips that order for the day. Normal spreads on these ETFs are a few cents.
- **Price moves:** a price more than 25% away from the last close on an unleveraged ETF is rejected as bad data.
- **Bars:** decisions use only completed daily bars. If a bar is missing for any universe ETF, the bot holds and makes no sleeve changes that day.
- **Cash and minimums:** buys need settled cash under the bot's own T+1 rule, and $1 is the minimum order.
- **Duplicates:** each order carries a client order id built from portfolio, date, symbol and side. A repeat is refused, both by the bot's ledger and by the broker.

## 7. Kill switch

Trips on any of the following:
- A reconciliation mismatch over $0.50 or an unknown symbol in the broker account.
- An order rejected twice.
- A ledger that cannot be read.

What happens: all order sending stops for every portfolio.

Restart requires both:
- a clean reconciliation, and
- an owner resume command, logged with a reason.

The emergency kill switch file (`KILL_SWITCH`) works the same way.

## Summary for the Council

| Safeguard | Proposed setting | Tripped 2017–2026 | Action | Restart |
|---|---|---|---|---|
| Daily loss | ≥5% in a day | 6 days | Block next-day sleeve entries | Automatic after 1 session |
| Core cap | target + 5 pts | 0 | Refuse order, flag | Automatic, after the issue is reviewed |
| Sleeve ETF cap | 1.5 × slot target | 0 | Refuse order, flag | Same |
| Sleeve breaker | 25% sleeve drawdown | 0 | Sleeve to BIL | Owner resume |
| Losing exits | 6 in a row | 3 | Alert only | — |
| Wealth alert | 20% below high | 2 | Alert only | — |
| Kill switch | Reconciliation, data or order errors | — | Stop everything | Clean reconciliation plus owner resume |
