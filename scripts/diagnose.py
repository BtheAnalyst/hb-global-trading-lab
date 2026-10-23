"""Read-only strictness check. Places NO orders and saves nothing.

1. Signal frequency: replays the entry rule over ~1 year of daily bars for each ETF.
2. Contract filter: for each ETF (calls and puts, 30-45 DTE, delta band), shows which
   risk check blocks the best candidates, and how many would pass under looser settings.
Results are printed as GitHub annotations so they can be read from the run.
"""
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import copy  # noqa: E402

import yaml  # noqa: E402

from hbgtl.gateway import AlpacaPaperGateway  # noqa: E402
from hbgtl.risk import AccountState, OptionQuote, RiskEngine  # noqa: E402
from hbgtl.selection import rank  # noqa: E402
from hbgtl.strategy import evaluate, rsi, sma  # noqa: E402


def note(title, msg):
    print(f"::notice title={title}::{msg}")


cfg = yaml.safe_load(open("config/settings.yaml"))
t = cfg["trading"]
gw = AlpacaPaperGateway()
now = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
bars = gw.daily_bars(sorted(set(t["universe"] + ["SPY"])), days=420)

# ---------- 1. signal frequency over the past year ----------
tot_days, tot_sig = 0, 0
sig_lines, con_lines = [], []
for sym in t["universe"]:
    c, v = bars[sym]
    days = sigs = 0
    for i in range(60, len(c) + 1):
        days += 1
        if evaluate(sym, c[:i], v[:i], t).direction:
            sigs += 1
    tot_days, tot_sig = tot_days + days, tot_sig + sigs
    s20, s50, r = sma(c, 20), sma(c, 50), rsi(c, 14)
    trend = "uptrend" if c[-1] > s50 and s20 > s50 else "downtrend" if c[-1] < s50 and s20 < s50 else "no trend"
    sig_lines.append(f"{sym}: {sigs} signal days in {days}; today {trend}, RSI {r:.0f}")
yrs = tot_days / len(t["universe"]) / 252
note("Signals by ETF", " | ".join(sig_lines))
note("Signal total", f"{tot_sig} signal days across {len(t['universe'])} ETFs in ~{yrs:.1f} years "
     f"(about {tot_sig / max(yrs, 0.01) / 52:.1f} per week across the whole list)")

# ---------- 2. which risk check blocks the contracts ----------
variants = {
    "current (1% risk, 10% spread)": {},
    "spread 15%": {"max_spread_pct_of_mid": 15.0},
    "risk 2%": {"max_risk_per_trade_pct": 2.0},
    "risk 2% + spread 15%": {"max_risk_per_trade_pct": 2.0, "max_spread_pct_of_mid": 15.0},
    "OI ignored": {"min_open_interest": 0},
    "OI ignored + risk 2%": {"min_open_interest": 0, "max_risk_per_trade_pct": 2.0},
    "OI ignored + risk 2% + spread 15%": {"min_open_interest": 0, "max_risk_per_trade_pct": 2.0, "max_spread_pct_of_mid": 15.0},
}
engines = {}
for name, over in variants.items():
    c2 = copy.deepcopy(cfg)
    c2["risk"].update(over)
    engines[name] = RiskEngine(c2, "paper")
acct = AccountState(equity=10_000, start_of_day_equity=10_000)
blockers = Counter()
oi_zero = 0
passes = Counter()
checked = 0
for sym in t["universe"]:
    price = bars[sym][0][-1]
    for strat in ("long_call", "long_put"):
        try:
            cands = rank(gw.option_candidates(sym, strat, price, now.date(), t["contracts"]["dte_min"],
                                              t["contracts"]["dte_max"]), t, price)[: t["contracts"]["max_checked"]]
        except Exception as e:  # keep going; report the failure
            con_lines.append(f"{sym} {strat}: lookup failed ({type(e).__name__})")
            continue
        if not cands:
            con_lines.append(f"{sym} {strat}: no contract in range")
            continue
        best = cands[0]
        row = []
        for c in cands:
            checked += 1
            oi_zero += c.open_interest == 0
            q = OptionQuote(c.symbol, c.bid, c.ask, c.open_interest, c.dte, getattr(c, "quote_time", now), c.strategy)
            for name, eng in engines.items():
                dec = eng.check(q, acct, getattr(c, "quote_time", now))  # staleness judged vs quote time
                if dec.approved:
                    passes[name] += 1
                elif name.startswith("current"):
                    blockers.update({r.split(":")[0].split("(")[0].strip() for r in dec.reasons})
        con_lines.append(f"{sym} {strat}: ask ${best.ask:.2f} (${best.ask * 100:,.0f} per contract), spread {best.spread_pct:.0f}%, "
             f"OI {best.open_interest}, delta {abs(best.delta) if best.delta is not None else float('nan'):.2f}")

note("Best contract per ETF", " | ".join(con_lines))
note("Blockers (current rules)", "; ".join(f"{k}: {n}" for k, n in blockers.most_common()) or "none")
note("Open interest data", f"{oi_zero} of {checked} candidates report open interest 0")
note("Would pass", f"of {checked} candidate contracts: " + "; ".join(f"{k}: {passes[k]}" for k in variants))
