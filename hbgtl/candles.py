"""Candlestick and intraday-structure features from COMPLETED 1-minute bars (one symbol, one session).

Every feature at row i uses rows 0..i only (no look-ahead). The caller must pass only finished bars:
the live engine drops the still-forming minute, and the backtest decides at the close of bar i and fills
at the open of bar i+1.

Input frame columns: t (tz-aware), o, h, l, c, v. Rows must be regular-session minutes of ONE day.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ET = "America/New_York"


def session_frame(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values("t").reset_index(drop=True).copy()
    et = pd.to_datetime(d["t"], utc=True).dt.tz_convert(ET)
    d["m"] = et.dt.hour * 60 + et.dt.minute - 570          # minutes since 9:30 ET (0 = 9:30 bar)
    tp = (d["h"] + d["l"] + d["c"]) / 3
    cv = d["v"].cumsum()
    d["cumv"] = cv
    d["vwap"] = (tp * d["v"]).cumsum() / cv.replace(0, np.nan)
    d["vwap"] = d["vwap"].ffill().fillna(d["c"])
    d["ema9"] = d["c"].ewm(span=9, adjust=False).mean()
    d["ema20"] = d["c"].ewm(span=20, adjust=False).mean()
    tr = np.maximum(d["h"] - d["l"], np.maximum((d["h"] - d["c"].shift()).abs(), (d["l"] - d["c"].shift()).abs()))
    d["atr"] = tr.fillna(d["h"] - d["l"]).ewm(span=14, adjust=False).mean()
    d["vavg"] = d["v"].expanding().mean().shift(1)            # average 1-min volume BEFORE this bar
    rng = (d["h"] - d["l"]).replace(0, np.nan)
    body = (d["c"] - d["o"])
    d["body_pct"] = (body.abs() / rng).fillna(0)
    d["close_pos"] = ((d["c"] - d["l"]) / rng).fillna(0.5)     # 1 = closed at the high
    d["bull"] = body > 0
    po, pc, ph, pl = d["o"].shift(), d["c"].shift(), d["h"].shift(), d["l"].shift()
    d["bull_engulf"] = d["bull"] & (pc < po) & (d["c"] >= po) & (d["o"] <= pc)
    d["bear_engulf"] = (~d["bull"]) & (pc > po) & (d["o"] >= pc) & (d["c"] <= po)
    lower_wick = np.minimum(d["o"], d["c"]) - d["l"]
    upper_wick = d["h"] - np.maximum(d["o"], d["c"])
    d["hammer"] = (lower_wick >= 2 * body.abs()) & (upper_wick <= body.abs() + 1e-9) & (d["close_pos"] >= 0.6)
    d["shooting_star"] = (upper_wick >= 2 * body.abs()) & (lower_wick <= body.abs() + 1e-9) & (d["close_pos"] <= 0.4)
    d["doji"] = d["body_pct"] <= 0.1
    d["inside"] = (d["h"] <= ph) & (d["l"] >= pl)
    d["close_above_prev_high"] = d["c"] > ph
    return d


def five_minute(d: pd.DataFrame, upto: int) -> pd.DataFrame:
    """5-minute candles built only from COMPLETE 5-minute blocks within rows 0..upto."""
    x = d.iloc[: upto + 1]
    blk = x["m"] // 5
    last_full = (x["m"].iloc[-1] + 1) // 5          # blocks before this index are complete
    x = x[blk < last_full]
    if x.empty:
        return x
    g = x.groupby(x["m"] // 5)
    return pd.DataFrame({"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last(),
                         "v": g["v"].sum()})


def structure(bars: pd.DataFrame, n: int = 3) -> dict:
    """Higher-high/higher-low vs lower-high/lower-low over the last n candles."""
    if len(bars) < n:
        return {"hh_hl": False, "lh_ll": False}
    h, lo = bars["h"].values[-n:], bars["l"].values[-n:]
    return {"hh_hl": bool(np.all(np.diff(h) > 0) and np.all(np.diff(lo) > 0)),
            "lh_ll": bool(np.all(np.diff(h) < 0) and np.all(np.diff(lo) < 0))}


def rvol(cumv_now: float, minute: int, profile: dict | None) -> float | None:
    """Relative volume: today's cumulative volume vs the average cumulative volume at the same minute
    over prior sessions (profile: {minute_bucket_5: avg_cumv}, built from earlier days only)."""
    if not profile:
        return None
    b = (minute // 5) * 5
    base = profile.get(b) or profile.get(str(b))
    return float(cumv_now / base) if base else None


def volume_profile(days: list[pd.DataFrame]) -> dict:
    """Average cumulative volume at each 5-minute mark from prior sessions (each a session_frame)."""
    rows = []
    for d in days:
        s = d.groupby((d["m"] // 5) * 5)["v"].sum().cumsum()
        rows.append(s)
    if not rows:
        return {}
    return {int(k): float(v) for k, v in pd.concat(rows, axis=1).mean(axis=1).items()}


def levels(d: pd.DataFrame, upto: int, prev_day: dict | None = None) -> dict:
    """Support/resistance candidates: opening range, session high/low so far, prior-day high/low/close."""
    x = d.iloc[: upto + 1]
    out = {"session_high": float(x["h"].max()), "session_low": float(x["l"].min()), "vwap": float(x["vwap"].iloc[-1])}
    orb = x[x["m"] < 15]
    if len(orb):
        out.update(or_high=float(orb["h"].max()), or_low=float(orb["l"].min()))
    if prev_day:
        out.update({f"prev_{k}": v for k, v in prev_day.items()})
    return out


# Typical share of a full day's volume traded by each 5-minute mark (cumulative, through that 5-minute block),
# measured on the research pool (Apr-Oct 2026, SIP). Used when a symbol has no intraday history of its own
# (e.g. a gapper): RVOL = volume so far / (20-day average daily volume x typical share done by now).
DAY_CURVE = {0: 0.038, 5: 0.0572, 10: 0.0742, 15: 0.0914, 20: 0.107, 25: 0.1214, 30: 0.1374, 35: 0.1518, 40: 0.1666, 45: 0.1809, 50: 0.1942, 55: 0.2068, 60: 0.2208, 65: 0.2333, 70: 0.2453, 75: 0.2577, 80: 0.2698, 85: 0.2816, 90: 0.2937, 95: 0.3054, 100: 0.3169, 105: 0.3282, 110: 0.3392, 115: 0.3508, 120: 0.3628, 125: 0.3732, 130: 0.3831, 135: 0.3928, 140: 0.4023, 145: 0.4115, 150: 0.421, 155: 0.4304, 160: 0.4393, 165: 0.4485, 170: 0.4576, 175: 0.4661, 180: 0.4749, 185: 0.4834, 190: 0.4918, 195: 0.5, 200: 0.5081, 205: 0.5163, 210: 0.525, 215: 0.5332, 220: 0.5413, 225: 0.5495, 230: 0.5578, 235: 0.5661, 240: 0.5753, 245: 0.5837, 250: 0.5918, 255: 0.5999, 260: 0.6082, 265: 0.6165, 270: 0.6265, 275: 0.6354, 280: 0.644, 285: 0.6527, 290: 0.6612, 295: 0.6698, 300: 0.679, 305: 0.6878, 310: 0.6967, 315: 0.7061, 320: 0.7155, 325: 0.7257, 330: 0.7368, 335: 0.7476, 340: 0.7587, 345: 0.7704, 350: 0.7826, 355: 0.7962, 360: 0.8127, 365: 0.8285, 370: 0.8464, 375: 0.8686, 380: 0.9036, 385: 1.0}


def rvol_daily(cumv_now: float, minute: int, adv20: float | None) -> float | None:
    if not adv20:
        return None
    frac = DAY_CURVE.get((max(0, minute) // 5) * 5) or 1.0
    return float(cumv_now / (adv20 * frac))
