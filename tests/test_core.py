from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from hbgtl.broker import SimulatedBroker, place_order
from hbgtl.feasibility import required_daily_growth, simulate
from hbgtl.metrics import max_drawdown, trade_stats, xirr
from hbgtl.portfolio import load_portfolios
from hbgtl.projections import (affordability_by_month, dca_benchmark, project,
                               required_monthly_return)
from hbgtl.reports import DayStats, end_of_day_report
from hbgtl.risk import (AccountState, LiveTradingBlocked, OptionQuote, RiskEngine,
                        assert_paper_or_approved, loss_alert_needed, max_affordable_premium)
from hbgtl.store import Store
from hbgtl.vault import ProfitVault

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    with open("config/settings.yaml") as f:
        return yaml.safe_load(f)


def quote(ask=0.50, bid=0.48, oi=2000, dte=30, age=5, strategy="long_call", tz=True):
    t = NOW - timedelta(seconds=age)
    return OptionQuote("XLF261218C00050000", bid, ask, oi, dte, t if tz else t.replace(tzinfo=None), strategy)


# ---------- risk engine ----------

def test_portfolio_b_month_12_still_cannot_trade(cfg):
    d = RiskEngine(cfg, "paper").check(quote(ask=0.50), AccountState(1200, 1200), NOW)
    assert not d.approved and any("unaffordable" in r for r in d.reasons)


def test_portfolio_a_sizes_within_1pct_including_fees(cfg):
    d = RiskEngine(cfg, "paper").check(quote(ask=0.50, bid=0.48), AccountState(10_000, 10_000), NOW)
    assert d.approved and d.contracts == 1          # 1 x ($50 + $0.10 fees) fits; 2 x $50.10 = $100.20 doesn't
    assert d.max_loss <= 100


@pytest.mark.parametrize("kw,acct_kw,needle", [
    ({"age": 300}, {}, "stale"),
    ({"tz": False}, {}, "timezone"),
    ({"bid": 0.30, "ask": 0.50}, {}, "spread"),
    ({"oi": 10}, {}, "open interest"),
    ({"dte": 3}, {}, "expiration"),
    ({"strategy": "short_put"}, {}, "not allowed"),
    ({}, {"kill_switch": True}, "kill switch"),
    ({}, {"consecutive_losses": 3}, "in a row"),
    ({}, {"open_positions": 1}, "open positions"),
])
def test_hard_rejections(cfg, kw, acct_kw, needle):
    d = RiskEngine(cfg, "paper").check(quote(**kw), AccountState(10_000, 10_000, **acct_kw), NOW)
    assert not d.approved and any(needle in r for r in d.reasons)


def test_daily_loss_limit_blocks_new_trades(cfg):
    d = RiskEngine(cfg, "paper").check(quote(), AccountState(9_690, 10_000), NOW)
    assert not d.approved and any("daily loss" in r for r in d.reasons)


def test_deposit_cannot_mask_a_daily_loss(cfg):
    # Lost $350 trading, then a $100 deposit landed: still a -3.4% day.
    acct = AccountState(9_750, 10_000, deposits_today=100)
    assert acct.daily_pnl == -350
    d = RiskEngine(cfg, "paper").check(quote(), acct, NOW)
    assert any("daily loss" in r for r in d.reasons)


def test_live_mode_blocked_without_owner_approval(monkeypatch, cfg):
    monkeypatch.delenv("HBGTL_LIVE_APPROVED", raising=False)
    with pytest.raises(LiveTradingBlocked):
        assert_paper_or_approved("live")
    with pytest.raises(LiveTradingBlocked):
        RiskEngine(cfg, "live")


def test_affordability_math():
    assert max_affordable_premium(100, 1) == pytest.approx(0.01)
    assert max_affordable_premium(10_000, 1) == pytest.approx(1.00)


def test_loss_alerts_reconciled_with_1pct_cap(cfg):
    pct = cfg["alerts"]["single_trade_loss_pct"]
    assert pct <= cfg["risk"]["max_risk_per_trade_pct"]          # alert can actually fire
    assert loss_alert_needed(30, 100, 10_000, pct) == []          # ordinary loss: no push
    assert loss_alert_needed(100, 100, 10_000, pct)               # full approved risk lost
    assert any("execution error" in a for a in loss_alert_needed(150, 100, 10_000, pct))


# ---------- profit vault / cash-flow-adjusted high-water mark ----------

def test_vault_70_30_split_on_new_profit():
    v = ProfitVault(100, vault_pct=30)
    e = v.record_closed_trade("t1", 100)
    assert e.eligible_profit == 100 and e.to_vault == 30
    assert v.trading_equity == 170 and v.vault_balance == 30 and v.total_project_wealth == 200


def test_deposit_is_never_profit():
    v = ProfitVault(100, vault_pct=30)
    e = v.deposit(100)
    assert e.eligible_profit == 0 and v.vault_balance == 0
    assert v.total_contributions == 200 and v.cumulative_net_gain == 0
    e = v.record_closed_trade("t1", 10)
    assert e.eligible_profit == 10 and e.to_vault == 3


def test_recovered_loss_is_not_new_profit_even_with_deposit_between():
    v = ProfitVault(100, vault_pct=30)
    v.record_closed_trade("loss", -40)
    v.deposit(100)
    assert v.loss_to_recover == 40
    e = v.record_closed_trade("recover", 40)
    assert e.eligible_profit == 0 and v.vault_balance == 0 and v.trading_equity == 200


def test_swept_profit_not_double_counted():
    v = ProfitVault(100, vault_pct=30)
    v.record_closed_trade("w1", 100)
    v.record_closed_trade("l1", -30)
    e = v.record_closed_trade("w2", 30)
    assert e.eligible_profit == 0 and v.vault_balance == 30


def test_cents_dont_drift():
    v = ProfitVault(100, vault_pct=30)
    for i in range(1000):
        v.record_closed_trade(f"t{i}", 0.10)
    assert v.total_project_wealth == pytest.approx(200.0, abs=0.01)


# ---------- portfolios ----------

def test_portfolios_are_separate_and_deposits_idempotent(cfg):
    ps = load_portfolios(cfg, date(2026, 11, 1))
    a, b = ps["A"], ps["B"]
    assert a.ledger.trading_equity == 10_000 and b.ledger.trading_equity == 100
    b.apply_scheduled_deposits(date(2027, 10, 15))
    b.apply_scheduled_deposits(date(2027, 10, 15))                # re-run: no double deposit
    assert b.ledger.total_contributions == 1_200                    # 12 deposits incl. month one
    assert a.apply_scheduled_deposits(date(2027, 10, 15)) == [] and a.ledger.total_contributions == 10_000


# ---------- orders: idempotency + confirmed fills ----------

def test_duplicate_order_prevented(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    br = SimulatedBroker({"XLF": (0.48, 0.50)})
    r1 = place_order(s, br, "A", "A-2026-10-21-XLF-1", "XLF", "buy", 1, 0.50)
    r2 = place_order(s, br, "A", "A-2026-10-21-XLF-1", "XLF", "buy", 1, 0.50)
    assert r1.status == "filled" and r1.avg_fill_price == 0.50
    assert r2.status == "duplicate"


def test_unfilled_order_is_not_assumed_filled(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    r = place_order(s, SimulatedBroker({"XLF": (0.48, 0.50)}), "A", "o1", "XLF", "buy", 1, 0.45)
    assert r.status == "submitted" and r.filled_qty == 0
    assert s.open_orders()[0]["client_order_id"] == "o1"


def test_partial_fill_recorded(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    r = place_order(s, SimulatedBroker({"XLF": (0.48, 0.50)}, partial={"XLF": 1}), "A", "o2", "XLF", "buy", 3, 0.50)
    assert r.status == "partially_filled" and r.filled_qty == 1


def test_audit_log_is_append_only(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.log("trade_closed", {"pnl": -12.5}, "A")
    with pytest.raises(Exception):
        s.db.execute("DELETE FROM events")
    assert s.events("trade_closed")[0]["pnl"] == -12.5


# ---------- metrics ----------

def test_xirr_adjusts_for_deposit_timing():
    flows = [(date(2026, 1, 1), 100), (date(2026, 7, 1), 100)]
    r = xirr(flows, 200, date(2027, 1, 1))
    assert abs(r) < 1e-6                                          # no growth = 0%
    r2 = xirr([(date(2026, 1, 1), 100)], 110, date(2027, 1, 1))
    assert r2 == pytest.approx(0.10, abs=1e-4)


def test_max_drawdown_and_stats():
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)
    st = trade_stats([50, -25, 50, -25])
    assert st["win_rate"] == 0.5 and st["profit_factor"] == 2 and st["expectancy"] == 12.5


# ---------- projections ----------

def test_contributions_and_required_return():
    rows = project(0.0)
    assert rows[-1]["contributions"] == 1200 and rows[-1]["wealth"] == 1200
    assert required_monthly_return() == pytest.approx(0.305, abs=0.002)


def test_portfolio_b_affordability_stays_tiny():
    assert affordability_by_month()[-1]["max_premium"] < 0.13


def test_dca_benchmark():
    assert 1240 < dca_benchmark(0.07) < 1250


def test_required_growth_100_day():
    assert required_daily_growth(100, 100) == pytest.approx(0.04713, abs=1e-5)


def test_1pct_stake_never_reaches_10k_even_with_edge():
    assert simulate(0.01, 0.45, n_paths=5_000)["p_10000"] == 0


# ---------- reporting ----------

def _day(**kw):
    base = dict(date="2026-10-21", mode="paper", portfolio="A", start_equity=10_000, end_equity=9_900,
                deposits_today=0, vault_balance=0, new_vault_allocation=0, total_contributions=10_000,
                wins=0, losses=1, rule_violations=0, high_water_mark=10_000, next_milestone=25_000,
                council_note="Loss was within normal range.")
    base.update(kw)
    return DayStats(**base)


def test_losing_day_reported_accurately():
    txt = end_of_day_report(_day())
    assert "Today's trading P&L: -$100.00 (-1.00%)" in txt
    assert "Current drawdown from peak: $100.00" in txt


def test_deposit_not_shown_as_trading_gain():
    txt = end_of_day_report(_day(portfolio="B", start_equity=500, end_equity=600, deposits_today=100,
                                 total_contributions=600, losses=0, high_water_mark=600))
    assert "Today's trading P&L: +$0.00" in txt and "not counted as profit" in txt


def test_vault_sweep_still_counts_in_daily_pnl():
    txt = end_of_day_report(_day(end_equity=10_070, new_vault_allocation=30, vault_balance=30,
                                 wins=1, losses=0, high_water_mark=10_070))
    assert "Today's trading P&L: +$100.00" in txt


def test_rule_violation_is_never_hidden():
    txt = end_of_day_report(_day(rule_violations=1))
    assert "RULE VIOLATION" in txt and "Every trade followed" not in txt


def test_no_trade_day_not_called_a_gain():
    txt = end_of_day_report(_day(end_equity=10_000, losses=0))
    assert "stayed in cash on purpose" in txt and "not a gain" in txt


def test_options_orders_refused_by_mandate():
    from hbgtl.gateway import is_option_symbol
    assert is_option_symbol("XLF261218C00050000") and is_option_symbol("SPY261127P00480000")
    assert not is_option_symbol("SPY") and not is_option_symbol("VTI") and not is_option_symbol("BRK.B")


def test_trading_paused_in_config():
    with open("config/settings.yaml") as f:
        assert yaml.safe_load(f)["trading"]["enabled"] is False
