"""End-to-end tests of the trading loop against a fake broker (no network)."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml

from hbgtl import state as st
from hbgtl.letter import daily_letter
from hbgtl.runner import run
from hbgtl.selection import Candidate
from hbgtl.strategy import evaluate, exit_reason, rsi

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 22, 11, 0, tzinfo=ET)


@pytest.fixture
def cfg():
    return yaml.safe_load(open("config/settings.yaml"))


def uptrend_with_pullback(n=80):
    """Steady uptrend, a 4-day dip, then an up day on high volume (a buy trigger)."""
    closes = [50 + 0.15 * i for i in range(n - 6)]
    for _ in range(4):
        closes.append(closes[-1] - 0.45)
    closes.append(closes[-1] + 0.05)
    closes.append(closes[-1] + 0.40)
    vols = [1_000_000.0] * (n - 1) + [2_000_000.0]
    return closes, vols


def flat(n=80):
    return [100 + (0.3 if i % 2 else -0.3) for i in range(n)], [1e6] * n


class FakeGW:
    def __init__(self, signal_symbol="XLF", ask=0.50, bid=0.48, open_=True, fill=True, quote_time=NOW):
        self.signal_symbol, self.ask, self.bid, self.open_, self.fill = signal_symbol, ask, bid, open_, fill
        self.quote_time = quote_time
        self.sent, self.orders, self.broker_positions = [], {}, {}

    def clock(self):
        return self.open_, NOW

    def daily_bars(self, symbols, days=120):
        return {s: (uptrend_with_pullback() if s == self.signal_symbol else flat()) for s in symbols}

    def option_candidates(self, underlying, strategy, price, today, dte_min, dte_max):
        c = Candidate(f"{underlying}261127C00062000", underlying, strategy, 62.0, today + timedelta(days=36),
                      36, 3000, self.bid, self.ask, 0.45)
        c.quote_time = self.quote_time
        return [c]

    def option_quote(self, symbol):
        return (self.bid, self.ask, NOW)

    def submit_limit(self, cid, symbol, side, qty, limit):
        self.sent.append((cid, symbol, side, qty, limit))
        self.orders[cid] = {"status": "filled" if self.fill else "new", "filled_qty": qty if self.fill else 0,
                            "filled_avg_price": limit if self.fill else None}
        if self.fill:
            self.broker_positions[symbol] = self.broker_positions.get(symbol, 0) + (qty if side == "buy" else -qty)
            if self.broker_positions[symbol] == 0:
                del self.broker_positions[symbol]
        return self.orders[cid]

    def get_order(self, cid):
        return self.orders.get(cid)

    def cancel(self, cid):
        self.orders[cid]["status"] = "canceled"

    def positions(self):
        return dict(self.broker_positions)


def test_rsi_bounds():
    assert rsi([float(i) for i in range(1, 40)]) == 100.0
    assert 0 <= rsi(flat()[0]) <= 100


def test_signal_fires_on_pullback(cfg):
    c, v = uptrend_with_pullback()
    assert evaluate("XLF", c, v, cfg["trading"]).direction == "long_call"
    assert evaluate("ZZZ", *flat(), cfg["trading"]).direction is None


def test_exit_rules(cfg):
    t = cfg["trading"]
    assert "take profit" in exit_reason(1.00, 1.55, 1, 30, t)
    assert "stop loss" in exit_reason(1.00, 0.55, 1, 30, t)
    assert "expiration" in exit_reason(1.00, 1.00, 1, 7, t)
    assert "time stop" in exit_reason(1.00, 1.00, 10, 30, t)
    assert exit_reason(1.00, 1.10, 2, 30, t) is None


def test_market_closed_sends_nothing(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW(open_=False)
    out = run(gw, cfg, s, NOW)
    assert gw.sent == [] and "closed" in out[0]


def test_full_cycle_entry_fill_exit_and_ledger(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW()
    run(gw, cfg, s, NOW)                                   # entry sent + reserved
    assert len(gw.sent) == 1 and gw.sent[0][2] == "buy" and s["pending"]
    qty = gw.sent[0][3]
    assert qty * (0.49 * 100 + 0.10) <= 100              # within 1% of $10,000 incl. fees
    run(gw, cfg, s, NOW + timedelta(minutes=30))          # fill confirmed -> position
    assert s["position"] and s["pending"] is None
    gw.bid, gw.ask = 0.80, 0.82                           # +60%: take profit
    run(gw, cfg, s, NOW + timedelta(minutes=60))
    assert gw.sent[-1][2] == "sell"
    run(gw, cfg, s, NOW + timedelta(minutes=90))          # exit fill confirmed
    assert s["position"] is None and len(s["trades"]) == 1 and s["trades"][0]["net_pnl"] > 0
    a = st.ledger(s, "A", 30)
    assert a.vault_balance > 0 and a.trading_equity > 10_000
    d = st.day(s, NOW.date())
    assert d["wins"] == 1 and d["vault_added"] == a.vault_balance
    # only one entry per day: no new buy after the exit
    run(gw, cfg, s, NOW + timedelta(minutes=120))
    assert sum(1 for x in gw.sent if x[2] == "buy") == 1


def test_unaffordable_contract_is_declined(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW(ask=3.00, bid=2.95)
    out = run(gw, cfg, s, NOW)
    assert gw.sent == [] and st.day(s, NOW.date())["rejected"] == 1
    assert any("declined" in x for x in out)


def test_stale_quote_declined(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW(quote_time=NOW - timedelta(hours=3))
    run(gw, cfg, s, NOW)
    assert gw.sent == []


def test_outside_entry_window_no_entry(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW()
    run(gw, cfg, s, NOW.replace(hour=9, minute=45))
    assert gw.sent == []


def test_kill_switch_and_disabled_block_entries(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW()
    s["kill_switch"] = True
    run(gw, cfg, s, NOW)
    assert gw.sent == []
    s2, cfg2 = st.new_state(cfg, NOW.date()), {**cfg, "trading": {**cfg["trading"], "enabled": False}}
    run(gw, cfg2, s2, NOW)
    assert gw.sent == []


def test_unknown_broker_position_trips_kill_switch(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW()
    gw.broker_positions = {"QQQ261127P00480000": 1}
    run(gw, cfg, s, NOW)
    assert s["kill_switch"] and gw.sent == [] and st.day(s, NOW.date())["alerts"]


def test_unfilled_entry_canceled_after_two_checks(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW(fill=False)
    run(gw, cfg, s, NOW)
    run(gw, cfg, s, NOW + timedelta(minutes=30))
    run(gw, cfg, s, NOW + timedelta(minutes=60))
    assert s["pending"] is None and s["position"] is None


def test_scan_mode_never_sends_orders(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW(open_=False)
    out = run(gw, cfg, s, NOW, mode="scan")
    assert gw.sent == [] and any("[scan only]" in x for x in out)


def test_stop_loss_logs_loss_and_letter_shows_it(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW()
    run(gw, cfg, s, NOW); run(gw, cfg, s, NOW + timedelta(minutes=30))
    gw.bid, gw.ask = 0.25, 0.27
    run(gw, cfg, s, NOW + timedelta(minutes=60)); run(gw, cfg, s, NOW + timedelta(minutes=90))
    assert s["trades"][0]["net_pnl"] < 0 and s["consecutive_losses"] == 1
    txt = daily_letter(cfg, s, NOW.date())
    assert "1 lost" in txt and "-$" in txt and "Every trade followed the risk rules" in txt


def test_portfolio_b_deposits_and_note(cfg):
    s = st.new_state(cfg, date(2026, 10, 21))
    later = datetime(2026, 12, 2, 11, 0, tzinfo=ET)
    run(FakeGW(open_=False), cfg, s, later)
    b = st.ledger(s, "B", 30)
    assert b.total_contributions == 300 and b.cumulative_net_gain == 0
    run(FakeGW(open_=False), cfg, s, later)                 # idempotent
    assert st.ledger(s, "B", 30).total_contributions == 300


def test_no_trade_letter_is_honest(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW(signal_symbol="NONE")
    run(gw, cfg, s, NOW)
    txt = daily_letter(cfg, s, NOW.date())
    assert "stayed in cash on purpose" in txt and "not a gain" in txt and "Paper mode" in txt


def test_email_letter_matches_numbers_and_shows_losses(cfg):
    from hbgtl.email_letter import daily_email
    s, gw = st.new_state(cfg, NOW.date()), FakeGW()
    run(gw, cfg, s, NOW); run(gw, cfg, s, NOW + timedelta(minutes=30))
    gw.bid, gw.ask = 0.25, 0.27
    run(gw, cfg, s, NOW + timedelta(minutes=60)); run(gw, cfg, s, NOW + timedelta(minutes=90))
    subject, html = daily_email(cfg, s, NOW.date())
    loss = s["trades"][0]["net_pnl"]
    assert f"-${abs(loss):,.2f}" in html and "a loss, kept within its limits" in html
    assert "The Daily Letter" in subject and "Paper" in html


def test_midday_email_shows_open_position_and_skips_idle_days(cfg):
    from hbgtl.email_letter import midday_email
    s, gw = st.new_state(cfg, NOW.date()), FakeGW()
    assert midday_email(cfg, s, NOW) is None                      # no runs today: nothing sent
    run(gw, cfg, s, NOW); run(gw, cfg, s, NOW + timedelta(minutes=30))
    gw.bid, gw.ask = 0.40, 0.42                                     # position down, inside limits
    run(gw, cfg, s, NOW + timedelta(minutes=60))
    subject, html = midday_email(cfg, s, NOW + timedelta(minutes=60))
    assert "Midday Check-in" in subject and "inside its limits" in html and "-$" in html
    closed = st.new_state(cfg, NOW.date())
    run(FakeGW(open_=False), cfg, closed, NOW)
    assert midday_email(cfg, closed, NOW) is None                  # market-closed day: nothing sent


def test_letters_explain_why_and_teach_terms(cfg):
    from hbgtl.email_letter import daily_email, midday_email
    s, gw = st.new_state(cfg, NOW.date()), FakeGW()
    run(gw, cfg, s, NOW); run(gw, cfg, s, NOW + timedelta(minutes=30))
    sub, mid = midday_email(cfg, s, NOW + timedelta(minutes=30))
    assert "Why it&#x27;s held" in mid and "pullback" in mid and "Speak the language" in mid
    gw.bid, gw.ask = 0.80, 0.82
    run(gw, cfg, s, NOW + timedelta(minutes=60)); run(gw, cfg, s, NOW + timedelta(minutes=90))
    t = s["trades"][0]
    assert t["signal"] and t["strike"] == 62.0 and t["delta"] == 0.45
    _, html = daily_email(cfg, s, NOW.date())
    assert "Why the bot did what it did" in html and "take-profit" in html and "Take profit" in html
    md = daily_letter(cfg, s, NOW.date())
    assert "## Why the bot did what it did" in md and "## Speak the language" in md


def test_declined_setup_counted_once_per_day(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW(ask=3.00, bid=2.95)
    for i in range(4):
        run(gw, cfg, s, NOW + timedelta(minutes=30 * i))
    d = st.day(s, NOW.date())
    assert d["rejected"] == 1 and "declined" in d["signals"]["XLF"]["outcome"]
