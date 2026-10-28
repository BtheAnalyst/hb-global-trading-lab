"""Intraday sleeve: candle features, setups (no look-ahead), sizing, settlement, order lifecycle, safety."""
import math

import pandas as pd
import pytest

from hbgtl.candles import session_frame, volume_profile, rvol
from hbgtl.intraday_engine import IntradayEngine, new_state
from hbgtl.intraday_sim import simulate_trade
from hbgtl.intraday_universe import asset_ok
from hbgtl.setups import Signal, evaluate

DAY = "2026-10-28"
CFG = {"mode": "paper", "execute": ["ORB15_V1"], "evaluate": ["ORB15_V1", "PULLBACK_VWAP_V1", "MOMO_CONT_V1"],
       "risk": {"risk_per_trade_pct": 0.005, "daily_loss_pct": 0.01, "max_open_positions": 1,
                "max_entries_per_day": 3, "chase_r": 0.25, "entry_timeout_s": 120, "max_quote_age_s": 30,
                "max_bar_age_s": 150, "max_spread_pct": 0.003, "last_entry": "15:00", "flatten": "15:50",
                "consecutive_error_kill": 2},
       "universe": {"price_min": 5, "price_max": 100, "adv_min": 1_000_000, "shortlist": 25}}


def bars(n=60, brk=20, base=20.0):
    """Opening range 20.00-20.20 for 15 min, quiet drift, breakout close 20.30 at minute `brk` on big volume,
    then follow-through to 20.70."""
    t0 = pd.Timestamp(f"{DAY} 09:30", tz="America/New_York").tz_convert("UTC")
    rows = []
    for m in range(n):
        if m < 15:
            o, c = (base + 0.05, base + 0.15) if m % 2 else (base + 0.15, base + 0.05)
            h, lo, v = base + 0.20, base, 10_000
        elif m < brk:
            o, c, h, lo, v = base + 0.12, base + 0.14, base + 0.18, base + 0.10, 8_000
        elif m == brk:
            o, c, h, lo, v = base + 0.14, base + 0.30, base + 0.31, base + 0.13, 60_000
        else:
            k = m - brk
            o = base + 0.30 + 0.02 * (k - 1); c = o + 0.02
            h, lo, v = c + 0.01, o - 0.01, 20_000
        rows.append({"t": t0 + pd.Timedelta(minutes=m), "o": o, "h": h, "l": lo, "c": c, "v": v})
    return pd.DataFrame(rows)


def profile():
    return {m: 2_000 * (m + 5) for m in range(0, 390, 5)}     # small history -> high relative volume


def test_features_use_only_past_bars():
    full = session_frame(bars(60))
    part = session_frame(bars(60).iloc[:21])
    for col in ("vwap", "ema20", "atr", "vavg", "bull_engulf"):
        a, b = full[col].iloc[:21].astype(float).fillna(-1), part[col].astype(float).fillna(-1)
        assert a.tolist() == pytest.approx(b.tolist()), col


def test_orb_signal_and_no_lookahead():
    d = session_frame(bars(60))
    st = {}
    sigs = [evaluate("ORB15_V1", d, i, "XYZ", 5.0, None, st) for i in range(len(d))]
    real = [s for s in sigs if isinstance(s, Signal)]
    assert len(real) == 1 and real[0].m == 20
    assert real[0].stop == pytest.approx(20.10) and real[0].target > real[0].ref
    # the same signal appears when the future does not exist yet
    d2 = session_frame(bars(21))
    s2 = evaluate("ORB15_V1", d2, 20, "XYZ", 5.0, None, {})
    assert isinstance(s2, Signal) and s2.ref == real[0].ref


def test_orb_rejects_low_relative_volume():
    d = session_frame(bars(30))
    st = {}
    out = [evaluate("ORB15_V1", d, i, "XYZ", 1.1, None, st) for i in range(len(d))]
    assert any(isinstance(x, str) and "relative volume" in x for x in out)
    assert not any(isinstance(x, Signal) for x in out)


def test_simulated_trade_target_and_stop():
    d = session_frame(bars(60))
    s = evaluate("ORB15_V1", d, 20, "XYZ", 5.0, None, {})
    res = simulate_trade(s, d[d["m"] >= 20].reset_index(drop=True), 0.001)
    assert res["how"] == "target" and res["R"] > 1.5
    # a stop is taken first when a bar touches both
    dd = d[d["m"] >= 20].reset_index(drop=True).copy()
    dd.loc[2, "l"] = 19.0; dd.loc[2, "h"] = 30.0
    assert simulate_trade(s, dd, 0.001)["how"] == "stop"


def test_relative_volume_profile():
    d = session_frame(bars(60))
    prof = volume_profile([d, d])
    assert rvol(float(d["cumv"].iloc[30]), 30, prof) == pytest.approx(d["cumv"].iloc[30] / prof[30])


def test_universe_excludes_etfs_otc_leveraged():
    ok = {"class": "us_equity", "tradable": True, "status": "active", "exchange": "NYSE", "name": "Ford Motor Co"}
    assert asset_ok(ok) is None
    assert "OTC" in asset_ok({**ok, "exchange": "OTC"})
    assert "ETF" in asset_ok({**ok, "name": "ProShares UltraPro QQQ"})
    assert "ETF" in asset_ok({**ok, "name": "SPDR S&P 500 ETF Trust"})


# ---------------------------------------------------------------- engine with a fake broker
class FakeGW:
    def __init__(self, quote=(20.29, 20.31, 1.0)):
        self.q, self.orders, self.sent = quote, {}, []

    def quote(self, sym):
        return self.q

    def submit_bracket(self, cid, sym, qty, limit, stop, target):
        assert qty == int(qty) and qty >= 1
        o = {"id": cid, "cid": cid, "symbol": sym, "status": "new", "type": "limit", "qty": qty, "filled_qty": 0,
             "filled_avg_price": None, "legs": [
                 {"id": cid + "-tp", "type": "limit", "status": "held", "filled_avg_price": None, "filled_qty": 0},
                 {"id": cid + "-sl", "type": "stop", "status": "held", "filled_avg_price": None, "filled_qty": 0}]}
        self.orders[cid] = o; self.sent.append(("bracket", cid, sym, qty, limit, stop, target))
        return o

    def order(self, cid):
        return self.orders.get(cid)

    def cancel(self, oid):
        for o in self.orders.values():
            if o["id"] == oid and o["status"] not in ("filled",):
                o["status"] = "canceled"
            for x in o.get("legs", []):
                if x["id"] == oid and x["status"] != "filled":
                    x["status"] = "canceled"

    def market_sell(self, cid, sym, qty):
        self.sent.append(("sell", cid, sym, qty))
        self.orders[cid] = {"id": cid, "cid": cid, "symbol": sym, "status": "filled", "type": "market",
                            "filled_qty": qty, "filled_avg_price": 20.25, "legs": []}
        return self.orders[cid]

    def position_qty(self, sym):
        return 0

    def fill(self, cid, px):
        o = self.orders[cid]; o.update(status="filled", filled_qty=o["qty"], filled_avg_price=px)
        o["legs"][1]["status"] = "new"; o["legs"][0]["status"] = "new"


def engine(settled=100.0, mode="paper", gw=None):
    st = new_state()
    st["sleeve"].update(allocated=settled, settled=settled)
    j = []
    gw = gw or FakeGW()
    e = IntradayEngine({**CFG, "mode": mode}, gw, st, j.append, lambda: None)
    daily = {"XYZ": pd.DataFrame({"close": [20.0] * 25, "volume": [5_000_000] * 25})}
    asset = {"XYZ": {"class": "us_equity", "tradable": True, "status": "active", "exchange": "NYSE", "name": "Xyz Inc"}}
    e.start_day(DAY, ["XYZ"], daily, {"XYZ": profile()}, asset)
    return e, st, j, gw


def run_to(e, upto_m, df=None):
    df = bars(60) if df is None else df
    e.update_bars({"XYZ": df})
    now = pd.Timestamp(f"{DAY} 09:30", tz="America/New_York").tz_convert("UTC") + pd.Timedelta(minutes=upto_m, seconds=6)
    return e.tick(now, True)


def test_signal_mode_never_sends_orders_but_shadow_trades():
    e, st, j, gw = engine(mode="signal")
    run_to(e, 21)
    assert any(r["event"] == "signal" for r in j) and gw.sent == []
    run_to(e, 59)
    assert st["shadow"]["ORB15_V1"]["trades"] and gw.sent == []


def test_paper_entry_sizing_whole_shares_settled_cash():
    e, st, j, gw = engine(100.0)
    run_to(e, 21)
    kind, cid, sym, qty, limit, stop, target = gw.sent[0]
    risk_ps = min(20.31, limit) - stop
    assert kind == "bracket" and qty == min(math.floor(0.50 / risk_ps), math.floor(100 / limit))
    assert qty * risk_ps <= 0.50 + 1e-9 and qty * limit <= 100
    assert st["position"]["status"] == "entering"
    gw.fill(cid, 20.30)
    run_to(e, 22)
    assert st["position"]["status"] == "open"
    assert st["sleeve"]["settled"] == pytest.approx(100 - 20.30 * qty)
    # stop leg fills at the broker -> trade closed, proceeds unsettled (T+1), not reusable today
    o = gw.orders[cid]; o["legs"][1].update(status="filled", filled_avg_price=20.10, filled_qty=qty)
    o["legs"][0]["status"] = "canceled"
    run_to(e, 23)
    assert st["position"] is None and st["trades"][-1]["how"] == "stop"
    assert st["sleeve"]["unsettled"][-1] == [DAY, pytest.approx(20.10 * qty)]
    assert st["trades"][-1]["pnl"] == pytest.approx((20.10 - 20.30) * qty)


def test_tiny_account_refuses_with_reason():
    e, st, j, gw = engine(0.20)
    run_to(e, 21)
    assert gw.sent == []
    ref = [r for r in j if r["event"] == "entry_refused"]
    assert ref and "account too small" in ref[0]["reason"]


def test_closes_immediately_if_stop_leg_not_live():
    e, st, j, gw = engine(100.0)
    run_to(e, 21)
    cid = gw.sent[0][1]
    gw.fill(cid, 20.30)
    gw.orders[cid]["legs"][1]["status"] = "canceled"          # protective stop missing
    run_to(e, 22)
    run_to(e, 23)
    assert any(s[0] == "sell" for s in gw.sent)
    assert st["position"] is None and "protective stop" in st["trades"][-1]["how"]


def test_stale_signal_never_traded():
    e, st, j, gw = engine(100.0)
    run_to(e, 40)                                             # breakout bar (20) is long past when first seen
    assert gw.sent == []
    assert any(r["event"] == "signal" and r["stale"] for r in j)


def test_wide_spread_and_runaway_price_refused():
    e, st, j, gw = engine(100.0, gw=FakeGW(quote=(20.20, 20.40, 1.0)))
    run_to(e, 21)
    assert gw.sent == [] and "spread" in [r for r in j if r["event"] == "entry_refused"][0]["reason"]
    e, st, j, gw = engine(100.0, gw=FakeGW(quote=(20.39, 20.40, 1.0)))
    run_to(e, 21)
    assert gw.sent == [] and "ran away" in [r for r in j if r["event"] == "entry_refused"][0]["reason"]


def test_kill_switch_and_daily_loss_block_entries():
    e, st, j, gw = engine(100.0)
    st["kill_switch"] = True
    run_to(e, 21)
    assert gw.sent == []
    e, st, j, gw = engine(100.0)
    st["day"]["realized"] = -1.0
    run_to(e, 21)
    assert gw.sent == [] and "daily loss" in [r for r in j if r["event"] == "entry_refused"][0]["reason"]


def test_settlement_next_day():
    e, st, j, gw = engine(10.0)
    st["sleeve"]["unsettled"] = [[DAY, 50.0]]
    e.start_day("2026-10-29", ["XYZ"], {}, {}, {})
    assert st["sleeve"]["settled"] == pytest.approx(60.0) and st["sleeve"]["unsettled"] == []


def test_founder_etf_orders_frozen():
    import yaml
    from hbgtl.equity_runner import specs_from_cfg
    cfg = yaml.safe_load(open("config/equity.yaml"))
    f = next(s for s in specs_from_cfg(cfg) if s.pid == "FOUNDER")
    assert f.frozen is True and f.mode == "broker"
