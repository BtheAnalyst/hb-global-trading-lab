"""Model A: the rules-only baseline strategy. Pure functions, no network, fully testable.

Signal is evaluated on COMPLETED daily bars (yesterday and earlier), so it is stable
all day and the bot can act on it any time during the entry window.
"""
from __future__ import annotations

from dataclasses import dataclass


def sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI on the full series; returns the last value."""
    if len(closes) < n + 1:
        return None
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def realized_vol(closes: list[float], n: int = 20) -> float | None:
    """Annualized close-to-close volatility (used as a free stand-in for the VIX)."""
    if len(closes) < n + 1:
        return None
    import math
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    m = sum(rets) / n
    var = sum((r - m) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252)


@dataclass
class Signal:
    symbol: str
    direction: str | None      # "long_call" | "long_put" | None
    reason: str


def evaluate(symbol: str, closes: list[float], volumes: list[float], cfg: dict) -> Signal:
    """closes/volumes: completed daily bars, oldest first."""
    s = cfg["signal"]
    if len(closes) < 55:
        return Signal(symbol, None, f"not enough history ({len(closes)} bars)")
    price = closes[-1]
    s20, s50 = sma(closes, 20), sma(closes, 50)
    r_now = rsi(closes, 14)
    r_prior = [rsi(closes[:i], 14) for i in range(len(closes) - s["lookback_days"], len(closes))]
    avg_vol = sma(volumes[:-1], 20)
    up_day = closes[-1] > closes[-2]
    down_day = closes[-1] < closes[-2]
    vol_ok = avg_vol is not None and volumes[-1] > avg_vol

    if price > s50 and s20 > s50:
        dipped = any(r is not None and s["rsi_low"] <= r <= s["rsi_high"] for r in r_prior)
        if dipped and up_day and vol_ok:
            return Signal(symbol, "long_call", f"uptrend; RSI dipped to {s['rsi_low']}-{s['rsi_high']} and turned up on volume (RSI {r_now:.0f})")
        return Signal(symbol, None, f"uptrend, no pullback trigger yet (RSI {r_now:.0f}, volume ok={vol_ok}, up day={up_day})")
    if price < s50 and s20 < s50:
        hi_low, hi_high = 100 - s["rsi_high"], 100 - s["rsi_low"]
        bounced = any(r is not None and hi_low <= r <= hi_high for r in r_prior)
        if bounced and down_day and vol_ok:
            return Signal(symbol, "long_put", f"downtrend; RSI bounced to {hi_low}-{hi_high} and turned down on volume (RSI {r_now:.0f})")
        return Signal(symbol, None, f"downtrend, no trigger yet (RSI {r_now:.0f})")
    return Signal(symbol, None, "no clear trend: stay in cash")


def market_calm(spy_closes: list[float], max_vol: float) -> tuple[bool, str]:
    v = realized_vol(spy_closes, 20)
    if v is None:
        return False, "not enough SPY history for the volatility filter"
    return v <= max_vol, f"SPY 20-day realized volatility {v:.0%} (limit {max_vol:.0%})"


# ---------- exits ----------

def exit_reason(entry_price: float, mid: float, trading_days_held: int, dte: int, cfg: dict) -> str | None:
    e = cfg["exits"]
    if mid >= entry_price * (1 + e["take_profit_pct"] / 100):
        return f"take profit (+{e['take_profit_pct']}%)"
    if mid <= entry_price * (1 - e["stop_loss_pct"] / 100):
        return f"stop loss (-{e['stop_loss_pct']}%)"
    if dte <= e["close_at_dte"]:
        return f"{dte} days to expiration: close before expiry"
    if trading_days_held >= e["max_trading_days"]:
        return f"time stop ({e['max_trading_days']} trading days)"
    return None
