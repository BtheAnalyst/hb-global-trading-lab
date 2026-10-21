"""Plain-English explanations for the letters: why the bot did what it did, and the
vocabulary behind it. Every sentence is built from what the bot actually recorded."""
from __future__ import annotations

from datetime import date

GLOSSARY = {
    "Paper trading": "Trading with simulated money on real market prices, to test a strategy before risking cash.",
    "Swing trading": "Holding a position for a few days to a few weeks to catch one move. Day trading means opening and closing within the same day.",
    "Call option": "A contract giving the right to buy 100 shares at a set price before a set date. It gains value when the stock rises.",
    "Put option": "A contract giving the right to sell 100 shares at a set price before a set date. It gains value when the stock falls.",
    "Premium": "The price paid for an option. When you buy one, it is the most you can lose.",
    "Strike price": "The price at which the option lets you buy (call) or sell (put) the shares.",
    "Expiration": "The date the option stops existing. The bot always sells at least 7 days before it.",
    "Delta": "Roughly how much the option's price moves when the stock moves $1. A 0.45 delta moves about 45 cents.",
    "Time decay (theta)": "Options lose a little value every day as expiration gets closer, even if the stock doesn't move.",
    "Bid-ask spread": "The gap between what buyers offer and sellers ask. Wide spreads quietly cost you on every trade.",
    "Open interest": "How many contracts of an option are currently open. Higher means easier to buy and sell.",
    "Moving average": "The average closing price over a set number of days (20-day, 50-day). It smooths out daily noise to show the trend.",
    "Uptrend": "Here: price above its 50-day average, with the 20-day average also above the 50-day.",
    "Pullback": "A short dip inside a larger trend. The strategy buys pullbacks in uptrends rather than chasing highs.",
    "RSI": "Relative Strength Index, a 0-100 gauge of recent momentum. Below about 30 is stretched to the downside; above 70 to the upside.",
    "Volume confirmation": "Checking that a move happened on above-average trading volume, which suggests real participation.",
    "Volatility": "How much prices are swinging. The bot pauses new trades when the market's recent volatility is above 30% a year.",
    "Take profit": "A preset exit that locks in a gain (here, when the option is up 50%).",
    "Stop loss": "A preset exit that caps a loss (here, when the option is down 40%).",
    "Time stop": "Closing a trade that hasn't worked within a set time (here, 10 trading days).",
    "Position sizing": "Deciding how much to put in each trade. The bot's 1% rule means no single trade can lose more than 1% of the account.",
    "Risk engine": "Hard-coded rules that check every trade before it's sent. Nothing can override a rejection.",
    "Limit order": "An order that only fills at your price or better, so you never overpay in a fast market.",
    "Drawdown": "How far the account has fallen from its highest point.",
    "Win rate": "The share of trades that made money. On its own it says little: a few big losses can outweigh many small wins.",
    "Profit factor": "Dollars won divided by dollars lost. Above 1.0 means the strategy made money overall.",
    "Expectancy": "The average result per trade after costs: what you expect to make or lose each time, over many trades.",
    "Liquidity": "How easily something trades without moving its price. Liquid ETFs and options keep costs low.",
}

ROTATION = list(GLOSSARY)


def _dir_word(strategy: str) -> str:
    return "call (a bet the price rises)" if strategy == "long_call" else "put (a bet the price falls)"


def _signal_plain(reason: str) -> str:
    r = reason or ""
    if r.startswith("uptrend; RSI dipped"):
        return ("it was in an uptrend (above its 50-day average), dipped on a short pullback (RSI fell into the 35-50 range), "
                "then turned back up on above-average volume")
    if r.startswith("downtrend; RSI bounced"):
        return ("it was in a downtrend (below its 50-day average), bounced briefly (RSI rose into the 50-65 range), "
                "then turned back down on above-average volume")
    return r or "its entry rules were met"


def _exit_plain(reason: str) -> str:
    r = reason or ""
    if r.startswith("take profit"):
        return "it hit the +50% take-profit target, so the bot locked in the gain"
    if r.startswith("stop loss"):
        return "it hit the -40% stop loss, so the bot cut the loss before it could grow"
    if r.startswith("time stop"):
        return "it hadn't worked within 10 trading days, so the bot freed up the capital (a time stop)"
    if "expiration" in r:
        return "it was 7 days from expiration, so the bot sold before time decay speeds up"
    return r


def explain_trade(t: dict) -> str:
    """One closed trade (from state['trades']) or an open position (with entry_price)."""
    und = t.get("underlying", "")
    parts = [f"Bought a {und} {_dir_word(t.get('strategy', 'long_call'))} because {_signal_plain(t.get('signal', ''))}."]
    det = []
    if t.get("strike"):
        det.append(f"${t['strike']:g} strike")
    if t.get("dte_at_entry") or t.get("dte"):
        det.append(f"{t.get('dte_at_entry') or t.get('dte')} days to expiration")
    if t.get("delta") is not None:
        det.append(f"delta {abs(t['delta']):.2f}")
    if det:
        parts.append("Contract: " + ", ".join(det) + ".")
    if t.get("approved_max_loss"):
        parts.append(f"The most it could lose was ${t['approved_max_loss']:,.2f}, inside the 1% rule.")
    if "exit" in t:
        parts.append(f"It closed at ${t['exit']:.2f} (entry ${t['entry']:.2f}) because {_exit_plain(t.get('reason', ''))}.")
    return " ".join(parts)


def market_read(d: dict) -> list[str]:
    """What the bot saw today, from the day's record."""
    out = []
    m = d.get("market")
    if m:
        if m.get("calm"):
            out.append(f"Market conditions passed the volatility check ({m['vol_note']}).")
        else:
            out.append(f"Markets were too turbulent for new trades today ({m['vol_note']}), so the bot only managed what it held.")
    sigs = d.get("signals", {})
    if sigs:
        for sym, s in sigs.items():
            verb = "call setup" if s["direction"] == "long_call" else "put setup"
            out.append(f"{sym}: a {verb} appeared; outcome: {s['outcome']}.")
    elif m and m.get("calm"):
        out.append("None of the 9 ETFs it watches showed a qualifying setup, so it waited. No trade is a valid outcome.")
    return out


def terms_for_today(d: dict, trades_today: list, has_position: bool, today: date, n: int = 3) -> list[tuple[str, str]]:
    """Terms tied to what happened today, topped up by a rotating term of the day."""
    picks = []
    sigs = d.get("signals", {})
    if trades_today or has_position or sigs:
        picks += ["Pullback", "RSI", "Delta"]
    if any("declined" in s.get("outcome", "") for s in sigs.values()):
        picks.insert(0, "Risk engine")
    if any(t.get("reason", "").startswith("stop loss") for t in trades_today):
        picks.insert(0, "Stop loss")
    if any(t.get("reason", "").startswith("take profit") for t in trades_today):
        picks.insert(0, "Take profit")
    if not picks:
        picks = ["Swing trading"]
    seen, out = set(), []
    for p in picks + [ROTATION[today.toordinal() % len(ROTATION)]]:
        if p not in seen and len(out) < n:
            seen.add(p)
            out.append((p, GLOSSARY[p]))
    return out
