# Backtest and engine audit (Gate 2, item 7)

Scope:
- the research engine (`research/backtest_v2.py`),
- the live paper engine (`hbgtl/equity_*.py`),
- the data in `research/data/`.

Every check can be re-run: `research/methodology_checks.py`, `research/replay_engine.py`, `research/risk_evidence.py`, `pytest`.

## Findings (errors found and fixed)

### Finding 1: v1 rebalanced every day (preserved at the Council's request)

`research/backtest.py` (v1) traded back to target weights **every day** after any rebalance. This had two effects:
- Trade counts and costs were inflated.
- The strategy being tested silently became a daily-rebalanced portfolio, which is not the strategy specified.

v2 trades only when one of these happens:
- a signal changes,
- new cash arrives to invest, or
- the core and sleeve drift more than 10 points apart.

v1 is kept in the repo, unchanged, as the record of the error. No v1 number is used anywhere.

### Finding 2: v2 left fragments after exits (found by building the live engine)

When v2 sold a sleeve ETF that dropped out of the ranks, it sold the dollar value measured at the **prior close**. If the ETF opened higher the next morning, a sliver of shares stayed behind. The sliver was never sold and was never part of any target.
- The live engine sells every share on an exit, so its replay disagreed with the backtest.
- The backtest now does the same (`full_exit=True`).
- Corrected Gate 1 results ($11,800 contributed, Jan 2017 to Oct 2026):

| Portfolio | Original v2 | Corrected | 100% VTI |
|---|---|---|---|
| B 90/10 | $25,239 | **$25,497** | $25,658 |
| C 80/20 | $24,806 | **$25,322** | $25,658 |
| D 70/30 | $24,383 | **$25,113** | $25,658 |

Most of the change is not the slivers' value. Slivers changed which of two nearly tied ETFs the sleeve held at a few turning points, and that changed the path. **The conclusion did not change: no mix beat 100% VTI.** The original results are kept in `research/gate1_results_v2_original.json`.

### Finding 3: the first draft of the engine overstated sleeve drawdowns

The first draft tracked the sleeve's performance with "units". After a full exit, a few units could remain with nothing behind them, and later purchases were then priced against a stale value. In replay, this made portfolio D's breaker trip falsely in January 2022.

It was replaced with a flow-adjusted daily index: each day's return equals (value − money moved in) ÷ the prior value. After the fix, the breaker never trips in history, which is consistent with the sleeve's real worst drawdown of 19.7%.

### Finding 4: a swapped argument in the price check (caught by the first live dry run)

The bad-price guard was passed the quote's age where the last close belonged. In replay the guard was silently skipped. In the first read-only run against live Alpaca data it fired and refused every order, so the guard failed safe.
- The argument order is fixed.
- A new test pushes an order through the whole runner with a 40% price jump and expects a refusal.

## Methodology checks

| Item | Result |
|---|---|
| **Trade costs** | Assumed 0.05% per side. On Oct 23, 2026, live spreads on all 10 ETFs were 0.00–0.03% (full spread), so the assumption is several times the half-spread actually paid. Alpaca charges $0 commission. Small regulatory fees on sales are not modeled; at these order sizes they are cents or less. Results at 0.10% and 0.20% per side: C $25,145 / $24,824; VTI $25,645 / $25,620. |
| **Signal timing** | Decide on the close of day t; fill at the open of day t+1. A look-ahead test on 150 random days found **0** differences between picks made with data cut off at day t and picks made with the full history. The live engine uses completed bars only: the dry run on Oct 23 used bars through Oct 22, the previous session. |
| **Adjusted prices** | Alpaca SIP bars, adjusted for splits and dividends. VTI calendar-year returns from our data: 2017 21.2%, 2018 −5.2%, 2019 30.7%, 2020 21.1%, 2021 25.7%, 2022 −19.5%, 2023 26.1%, 2024 23.8%, 2025 17.1%. Published total returns: 21.2, −5.2, 30.7, 21.1, 25.7, −19.5, 26.0, 23.8, 17.1. They match within 0.1 point. Caveat: adjusted prices assume dividends are reinvested on the ex-date. A real account receives cash on the pay date and invests it at the next pass, which is a small difference. |
| **ETF availability** | All 10 ETFs have data from Jan 4, 2016, before the first decision on Jan 3, 2017. None launched mid-test. On Oct 23, 2026, all 10 were active, tradable, fractionable and passed the leveraged/inverse check at Alpaca. Caveat: the universe was chosen today from funds that still exist. That bias is small for broad asset-class funds, but it is not zero. |
| **Data completeness** | Checked for all 10 ETFs: 2,716 sessions each, 0 missing days, 0 duplicate dates, 0 non-positive prices, 0 opens outside the day's high–low range, 0 one-day moves over 15%. |
| **Out-of-sample method** | One walk-forward split: tuned on 2017–2021, tested on 2022–Oct 2026, over a grid of 27 settings. **None beat VTI, either in-sample or out-of-sample.** The predeclared default returned 10.5%/yr out-of-sample vs VTI's 10.9%. The best in-sample setting returned 9.4%, worse than the default, which is a sign that tuning would have overfit. Limitations: a single split, an out-of-sample period of only 4.8 years, and one universe. |
| **Live engine = backtest** | The live engine was replayed day by day over 2017–2026 with backtest timing. It matched the corrected backtest **to within a few cents** for 7 of 8 portfolios (C with Vault: within 0.03%). Trade counts were identical. |
| **Path sensitivity** | Across 18 start months (Jan 2017 to Jun 2018), C trailed VTI in every run, by 1.0–1.3%. With the live engine's timing (cash invested a day sooner), C trailed by 2.7%; B by 1.3%; D by 4.0%. VTI itself moved by less than 0.02%. In that run the sleeve held a different ETF on only 104 of 2,464 days, yet that was enough to move the result by about $700. **Differences of a few percent between the mixes are within this timing noise. The direction never changed.** |

## What history beginning in 2016 cannot tell us

- **Too few bad markets.** The decisions span Jan 2017 to Oct 2026, under ten years. The data holds three sharp crashes (2018 Q4, 2020, spring 2025) and one rate-driven bear market (2022). It holds no long, grinding bear market like 2000–2002 or 2008–2009. Those are the conditions trend filters and momentum rotation are designed for, so this test can **neither confirm nor rule out** the sleeve's benefit there.
- **An unusually strong period for U.S. stocks.** VTI returned about 14.5% a year. Any mix that holds less VTI and more bonds, gold or international stocks starts at a disadvantage in this period.
- **Small samples behind the safeguards.** 85 sleeve exits, 6 days down 5% or more, and 2 sleeve drawdowns deeper than 15%. Thresholds set from samples this small are judgment calls; they are not statistical estimates.
- **Lookback warm-up.** The first year of data (2016) is used only to build the 252-day lookback. Starting earlier would need another data source, such as index proxies before 2016; that is listed as a Gate 4 option.
- **Simplifications.** Taxes are estimated (24% of each year's positive net realized gain), not computed. Uninvested cash earns nothing, which is conservative. Live fills will differ from opening prices.
