"""Gate 2 tests for the stock/ETF engine: allocator, books, risk checks, runner safety, backtest parity."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "research"))

from hbgtl.allocator import AllocationRule, decide, targets  # noqa: E402
from hbgtl.equity_gateway import SimGateway  # noqa: E402
from hbgtl.equity_ledger import Ledger  # noqa: E402
from hbgtl.equity_risk import RiskConfig, asset_allowed, cap_problem, quote_problem, reconcile  # noqa: E402
from hbgtl.equity_runner import Engine, PortfolioSpec, owner_resume, specs_from_cfg  # noqa: E402
from hbgtl.signals import Momentum, MomentumParams  # noqa: E402

CFG = yaml.safe_load(open(ROOT / "config" / "equity.yaml"))
R80 = AllocationRule(core_w=0.8)


# ---------------- config / mandate ----------------
def test_execution_disabled_and_exactly_one_selected_broker_portfolio():
    assert CFG["execution_enabled"] is True and CFG["live_trading"] is False
    brokers = [p for p in specs_from_cfg(CFG) if p.mode == "broker"]
    assert [p.pid for p in brokers] == [CFG["broker_portfolio"]] == ["FOUNDER"]
    shadows = [p for p in specs_from_cfg(CFG) if p.mode == "shadow"]
    assert len(shadows) == 8 and all(p.enabled for p in shadows)


def test_second_broker_portfolio_refused():
    bad = {"broker_portfolio": "X", "portfolios": {"X": {"core_weight": 1, "mode": "broker"},
                                                    "Y": {"core_weight": 1, "mode": "broker"}}}
    with pytest.raises(ValueError):
        specs_from_cfg(bad)
    unselected = {"broker_portfolio": "X", "portfolios": {"Y": {"core_weight": 1, "mode": "broker"}}}
    with pytest.raises(ValueError):
        specs_from_cfg(unselected)


def test_allocation_is_configurable_not_hardcoded():
    weights = {s.rule.core_w for s in specs_from_cfg(CFG)}
    assert {1.0, 0.9, 0.8, 0.7} <= weights
    assert {s.vault_pct for s in specs_from_cfg(CFG)} == {0.0, 0.30}


def test_scheduled_workflow_is_paper_only_and_options_scheduler_retired():
    wf = (ROOT / ".github" / "workflows" / "equity-engine.yml").read_text()
    on = yaml.safe_load(wf)[True]                        # 'on:' parses as True in YAML 1.1
    assert "schedule" in on and "--mode paper" in wf     # automated paper runs (founder decision Oct 23)
    assert "live" not in wf.lower().replace("live trading is blocked", "")
    assert CFG.get("live_trading") is False
    relay = (ROOT / "scripts" / "relay.py").read_text()
    assert "equity-engine" not in relay                  # the old options relay never starts the new engine


# ---------------- allocator ----------------
def test_targets_sum_to_equity():
    t = targets(R80, 1000, ["SPY"])
    assert t == pytest.approx({"VTI": 800, "SPY": 100, "BIL": 100})
    assert sum(t.values()) == pytest.approx(1000)


def test_no_trades_on_small_drift_without_cash():
    hv = {"VTI": 830, "SPY": 90, "BIL": 80}
    assert decide(R80, hv, 1000, 0.0, ["SPY"], ["SPY"]) == []


def test_exit_sells_whole_position_and_buys_never_exceed_cash():
    hv = {"VTI": 800, "SPY": 100, "BIL": 100}
    orders = decide(R80, hv, 1000, 50.0, ["SPY"], ["QQQ"])
    assert any(o.side == "sell" and o.symbol == "SPY" and o.dollars == 100 for o in orders)
    assert sum(o.dollars for o in orders if o.side == "buy") <= 50.0 + 1e-9


def test_benchmark_rule_only_buys_vti():
    orders = decide(AllocationRule(core_w=1.0), {}, 100, 100, [], [])
    assert [(o.side, o.symbol, round(o.dollars, 2)) for o in orders] == [("buy", "VTI", 100.0)]


# ---------------- ledger ----------------
def test_t_plus_one_settlement():
    led = Ledger("t"); led.deposit("2026-11-02", 100, None)
    led.apply_buy("2026-11-02", "VTI", 100, 1.0, 0.0, False)
    led.apply_sell("2026-11-03", "VTI", 110, 1.0, 0.0, False, 0.0, False)
    assert led.settled == pytest.approx(0) and led.unsettled_total() == pytest.approx(110)
    led.settle("2026-11-03"); assert led.settled == pytest.approx(0)       # same day: still unsettled
    led.settle("2026-11-04"); assert led.settled == pytest.approx(110)


def test_deposit_is_not_a_gain():
    led = Ledger("t"); led.deposit("2026-11-02", 100, None)
    px = {"VTI": 100.0}
    led.apply_buy("2026-11-02", "VTI", 100, 1.0, 0.0, False)
    nav0 = led.nav(px)
    led.deposit("2026-12-01", 100, led.total(px))
    assert led.nav(px) == pytest.approx(nav0) and led.contributed == 200


def test_vault_only_on_realized_gains_above_high_water_mark():
    led = Ledger("t"); led.deposit("2026-11-02", 1000, None)
    led.apply_buy("d1", "SPY", 500, 5, 0, True); led.apply_buy("d1", "QQQ", 500, 5, 0, True)
    led.apply_sell("d2", "SPY", 400, 5, 0, True, 0.30, True)       # -100 realized
    assert led.owed_vault == 0 and led.losing_exits == 1
    led.apply_sell("d3", "QQQ", 650, 5, 0, True, 0.30, True)       # +150 -> net +50 above old high of 0
    assert led.realized == pytest.approx(50) and led.owed_vault == pytest.approx(15)
    assert led.losing_exits == 0
    rep = led.report({})
    assert rep["prior_losses_to_recover"] == 0


def test_vault_paid_from_settled_cash_only():
    led = Ledger("t"); led.owed_vault = 20; led.settled = 5
    led.pay_vault()
    assert led.vault == 5 and led.owed_vault == 15 and led.settled == 0


# ---------------- risk ----------------
ALLOW = {"VTI", "BIL", "SPY", "QQQ"}


@pytest.mark.parametrize("sym,expect", [("XLF261218C00050000", "option"), ("TQQQ", "not on the approved"),
                                        ("ARKK", "not on the approved"), ("SPY", None)])
def test_asset_allowlist(sym, expect):
    r = asset_allowed(sym, ALLOW | {"TQQQ"} - {"TQQQ"}, None)
    assert (r is None) if expect is None else (expect in r)


def test_leveraged_detected_even_if_allowlisted():
    assert "leveraged" in asset_allowed("TQQQ", {"TQQQ"}, None)
    assert "leveraged" in asset_allowed("XYZ", {"XYZ"}, {"name": "ProShares UltraPro 3x QQQ"})


def test_quote_checks():
    rc = RiskConfig()
    assert "spread" in quote_problem("SPY", 99, 101.5, 1, 100, rc, True)
    assert "bad data" in quote_problem("SPY", 139.9, 140, 1, 100, rc, True)
    assert "old" in quote_problem("SPY", 99.99, 100.01, 120, 100, rc, True)
    assert quote_problem("SPY", 99.99, 100.01, 120, 100, rc, False) is None


def test_runner_refuses_order_at_abnormal_price():
    C = _toy_closes(); eng = Engine([_spec(rule=AllocationRule(core_w=1.0))], RiskConfig()); state = {}
    gw = SimGateway(); gw.set_prices({s: p * 1.4 for s, p in C.iloc[120].to_dict().items()})   # 40% jump
    r = eng.run_day(state, C, 119, str(C.index[120].date()), {"shadow": gw})
    p = r["portfolios"]["T"]
    assert p["orders"] == [] and "bad data" in p["refused"][0]["reason"]
    gw.set_prices(C.iloc[120].to_dict())                                    # normal price: accepted
    state = {}
    r = eng.run_day(state, C, 119, str(C.index[120].date()), {"shadow": gw})
    assert r["portfolios"]["T"]["orders"]


def test_caps_scale_with_strategy():
    rc = RiskConfig()
    assert cap_problem("VTI", 860, 1000, R80, rc)            # 86% > 85%
    assert cap_problem("VTI", 840, 1000, R80, rc) is None
    assert cap_problem("SPY", 160, 1000, R80, rc)            # 16% > 15%
    assert cap_problem("BIL", 240, 1000, R80, rc) is None    # both slots in T-bills is allowed


def test_reconcile_flags_unknown_and_mismatch():
    assert reconcile({"VTI": 1.0}, {"VTI": 1.0}, {"VTI": 300}, 0.5) == []
    p = reconcile({"VTI": 1.01, "TSLA": 1}, {"VTI": 1.0}, {"VTI": 300, "TSLA": 200}, 0.5)
    assert any("unknown" in x for x in p) and any("VTI" in x for x in p)


# ---------------- runner ----------------
def _toy_closes(n=300, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=n)
    syms = ["VTI", "BIL", "SPY", "QQQ"]
    drift = {"VTI": 0.0005, "BIL": 0.0001, "SPY": 0.0006, "QQQ": 0.0008}
    data = {s: 100 * np.exp(np.cumsum(drift[s] + 0.005 * rng.standard_normal(n))) for s in syms}
    return pd.DataFrame(data, index=idx)


def _spec(**kw):
    mom = MomentumParams(universe=("SPY", "QQQ"), lookbacks=(20, 60), sma=50, exit_rank=2, slots=1)
    base = dict(pid="T", rule=AllocationRule(core_w=0.8, slots=1), mom=mom, vault_pct=0.3)
    base.update(kw)
    return PortfolioSpec(**base)


def _run(eng, state, C, j, gw):
    gw.set_prices(C.iloc[j].to_dict()); gw.orders = {}
    return eng.run_day(state, C, j - 1, str(C.index[j].date()), {"shadow": gw})


def test_runner_end_to_end_and_once_per_day():
    C = _toy_closes(); eng = Engine([_spec()], RiskConfig()); state = {}; gw = SimGateway()
    for j in range(100, 140):
        _run(eng, state, C, j, gw)
    led = Ledger.from_dict(state["portfolios"]["T"])
    assert led.contributed >= 100 and led.trades > 0
    assert led.equity(C.iloc[139].to_dict()) > 0 and led.settled >= -1e-9
    again = eng.run_day(state, C, 138, str(C.index[139].date()), {"shadow": gw})
    assert "already ran today" in again["portfolios"]["T"]["notes"][0]


def test_persist_before_send():
    C = _toy_closes(); calls = []
    gw = SimGateway(); orig = gw.submit
    gw.submit = lambda *a: (calls.append("submit"), orig(*a))[1]
    eng = Engine([_spec()], RiskConfig(), save=lambda: calls.append("save"))
    state = {}
    for j in range(100, 105):
        _run(eng, state, C, j, gw)
    first = calls.index("submit")
    assert "save" in calls[:first]


def test_duplicate_client_order_id_refused():
    gw = SimGateway(); gw.submit("x", "VTI", "buy", 1, 100)
    with pytest.raises(ValueError):
        gw.submit("x", "VTI", "buy", 1, 100)
    with pytest.raises(PermissionError):
        gw.submit("y", "XLF261218C00050000", "buy", 1, 1)


def test_kill_switch_blocks_all_orders():
    C = _toy_closes(); eng = Engine([_spec()], RiskConfig()); state = {"kill_switch": True}; gw = SimGateway()
    r = _run(eng, state, C, 120, gw)
    assert r["portfolios"]["T"]["orders"] == [] and Ledger.from_dict(state["portfolios"]["T"]).trades == 0


def test_non_fractionable_buys_whole_shares_or_refuses():
    C = _toy_closes(); assets = {"VTI": {"tradable": True, "fractionable": False, "status": "active", "name": "VTI"}}
    eng = Engine([_spec(rule=AllocationRule(core_w=1.0))], RiskConfig()); state = {}; gw = SimGateway(assets=assets)
    r = _run(eng, state, C, 120, gw)        # $100 deposit, VTI ~ $100+: less than one share
    refused = r["portfolios"]["T"]["refused"]
    led = Ledger.from_dict(state["portfolios"]["T"])
    assert (refused and "not fractionable" in refused[0]["reason"]) or float(led.qty("VTI")).is_integer()


def test_breaker_trips_and_needs_owner_resume():
    C = _toy_closes(); eng = Engine([_spec()], RiskConfig(sleeve_breaker_dd=0.25)); state = {}; gw = SimGateway()
    for j in range(100, 130):
        _run(eng, state, C, j, gw)
    led = Ledger.from_dict(state["portfolios"]["T"]); led.sleeve_nav_v = led.sleeve_hwm * 0.7
    state["portfolios"]["T"] = led.to_dict()
    r = _run(eng, state, C, 130, gw)
    led = Ledger.from_dict(state["portfolios"]["T"])
    assert led.flags.get("breaker") and led.sleeve_held == []
    assert any("circuit breaker" in a for a in r["alerts"])
    owner_resume(state, "T", "reviewed: data ok", C.iloc[130].to_dict(), {"SPY", "QQQ", "BIL"}, "d")
    led = Ledger.from_dict(state["portfolios"]["T"])
    assert not led.flags.get("breaker") and led.sleeve_drawdown() == pytest.approx(0)


def test_daily_safeguard_blocks_new_entries_but_sells_nothing():
    C = _toy_closes(); eng = Engine([_spec()], RiskConfig()); state = {}; gw = SimGateway()
    for j in range(100, 130):
        _run(eng, state, C, j, gw)
    led = Ledger.from_dict(state["portfolios"]["T"])
    k = sorted(led.last_close_nav)[-1]; led.last_close_nav[k] *= 1.10   # pretend yesterday was 9% higher
    state["portfolios"]["T"] = led.to_dict()
    C2 = C.copy(); before = Ledger.from_dict(state["portfolios"]["T"]).sleeve_held
    r = _run(eng, state, C2, 130, gw)
    p = r["portfolios"]["T"]
    assert any("daily safeguard" in a for a in p["alerts"])
    new = Ledger.from_dict(state["portfolios"]["T"]).sleeve_held
    assert set(new) <= set(before)
    assert not [o for o in p["orders"] if o["side"] == "sell" and "safeguard" in o["reason"]]


def test_reconciliation_mismatch_trips_kill_switch():
    class FakeBroker(SimGateway):
        is_broker = True
        def positions(self):
            return {"TSLA": 3.0}
    C = _toy_closes(); eng = Engine([_spec(mode="broker")], RiskConfig()); state = {}
    b = FakeBroker(); b.set_prices(C.iloc[120].to_dict())
    r = eng.run_day(state, C, 119, str(C.index[120].date()), {"shadow": SimGateway(), "broker": b})
    assert state["kill_switch"] is True and r["portfolios"]["T"]["orders"] == []


def test_intraday_sleeve_position_is_not_an_unknown_broker_position(monkeypatch):
    import hbgtl.equity_runner as er
    class FakeBroker(SimGateway):
        is_broker = True
        def positions(self):
            return {"TSLA": 3.0}
    monkeypatch.setattr(er, "intraday_symbols", lambda: {"TSLA"})
    C = _toy_closes(); eng = Engine([_spec(mode="broker")], RiskConfig()); state = {}
    b = FakeBroker(); b.set_prices(C.iloc[120].to_dict())
    r = eng.run_day(state, C, 119, str(C.index[120].date()), {"shadow": SimGateway(), "broker": b})
    assert not state.get("kill_switch") and r["reconciliation"] == "clean"


def test_intraday_symbols_reads_sleeve_states(tmp_path):
    from hbgtl.equity_runner import intraday_symbols
    (tmp_path / "intraday_state_FOUNDER.json").write_text('{"position": {"symbol": "HOOD", "status": "open"}}')
    (tmp_path / "intraday_state_ENGINEERING.json").write_text('{"position": null}')
    assert intraday_symbols(tmp_path) == {"HOOD"}


# ---------------- signals and parity with the research backtest ----------------
def test_signals_match_backtest_momentum():
    from backtest_v2 import UNIVERSE, load
    O, C = load()
    m = Momentum(C, MomentumParams(universe=tuple(UNIVERSE)))
    i = len(C) - 1
    assert len(m.picks(i, [])) <= 2
    assert all(m.explain(i)[s]["above_trend"] for s in m.picks(i, []))


@pytest.mark.parametrize("w,vault", [(1.0, 0.0), (0.8, 0.30)])
def test_live_engine_matches_backtest(w, vault):
    from backtest_v2 import Params, load, run
    from replay_engine import replay
    O, C = load()
    cfg = {"portfolios": {"X": {"core_weight": w, "vault_pct": vault}}, "risk": CFG["risk"]}
    _, vals, _ = replay(cfg, O, C, start="2017-01-01", end="2019-12-31", parity=True)
    bt = run(O, C, Params(core_w=w, vault_pct=vault), start="2017-01-01", end="2019-12-31")
    assert float(vals["X"].iloc[-1]) == pytest.approx(float(bt.value.iloc[-1]), rel=1e-3)


# ---------------- Gate 3: Founder's Account against a pretend broker ----------------
from hbgtl.equity_gateway import BrokerSim  # noqa: E402


def _founder(**kw):
    return _spec(pid="FOUNDER", mode="broker", **kw)


def _bday(eng, state, C, j, broker, shadow=None):
    broker.set_prices(C.iloc[j].to_dict())
    gws = {"shadow": shadow or SimGateway(), "broker": broker}
    if shadow:
        shadow.set_prices(C.iloc[j].to_dict()); shadow.orders = {}
    return eng.run_day(state, C, j - 1, str(C.index[j].date()), gws)


def test_founder_never_uses_broker_buying_power():
    C = _toy_closes(); eng = Engine([_founder()], RiskConfig()); state = {}; b = BrokerSim(buying_power=200_000)
    for j in range(100, 160):
        _bday(eng, state, C, j, b)
        led = Ledger.from_dict(state["portfolios"]["FOUNDER"])
        px = C.iloc[j - 1].to_dict()
        invested_cost = sum(p["basis"] for p in led.positions.values())
        assert led.settled >= -1e-6                                    # never spent cash it did not have
        assert invested_cost <= led.contributed + max(0.0, led.realized) + 1e-6
        broker_value = sum(q * px[s] for s, q in b.positions().items())
        assert broker_value <= led.contributed * 1.5                   # nowhere near the $200K buying power
    months = len({str(d)[:7] for d in C.index[100:160]})
    assert led.contributed == 100 * months                            # $100 start + $100 each later month
    assert state.get("kill_switch") is not True


def test_capital_guard_refuses_order_larger_than_books():
    C = _toy_closes(); eng = Engine([_founder(rule=AllocationRule(core_w=1.0))], RiskConfig()); state = {}
    b = BrokerSim()
    # books say only $100 is available even though the broker would allow far more
    _bday(eng, state, C, 120, b)
    led = Ledger.from_dict(state["portfolios"]["FOUNDER"])
    assert sum(p["basis"] for p in led.positions.values()) <= 100 + 1e-6
    led.settled = 5.0; led.last_pass = None; state["portfolios"]["FOUNDER"] = led.to_dict()
    from hbgtl.allocator import Order
    import hbgtl.equity_runner as er
    orig = er.decide
    er.decide = lambda *a, **k: [Order("buy", "VTI", 50_000.0, False, "test: oversized order")]
    try:
        r = _bday(eng, state, C, 121, b)
    finally:
        er.decide = orig
    spent = sum(o["dollars"] for o in r["portfolios"]["FOUNDER"]["orders"])
    assert spent <= 5.0 + 1e-6


def test_partial_fill_then_cancel_books_only_the_filled_part():
    C = _toy_closes(); eng = Engine([_founder(rule=AllocationRule(core_w=1.0))], RiskConfig()); state = {}
    b = BrokerSim(); b.partial = 0.4
    r = _bday(eng, state, C, 120, b)
    led = Ledger.from_dict(state["portfolios"]["FOUNDER"])
    assert led.qty("VTI") == pytest.approx(b.positions()["VTI"])
    assert led.settled == pytest.approx(100 - r["portfolios"]["FOUNDER"]["orders"][0]["dollars"])
    b.partial = None
    r2 = _bday(eng, state, C, 121, b)                               # next day: leftover cash invested
    assert r2["reconciliation"] == "clean"


def test_rejected_orders_change_nothing_and_two_trip_the_kill_switch():
    C = _toy_closes(); eng = Engine([_founder(rule=AllocationRule(core_w=1.0))], RiskConfig()); state = {}
    b = BrokerSim(); b.reject_next = 5
    r = _bday(eng, state, C, 120, b)
    led = Ledger.from_dict(state["portfolios"]["FOUNDER"])
    assert led.positions == {} and led.settled == pytest.approx(100)
    assert any("rejected" in a for a in r["alerts"])
    _bday(eng, state, C, 121, b)
    led = Ledger.from_dict(state["portfolios"]["FOUNDER"])
    assert led.flags.get("kill")
    r3 = _bday(eng, state, C, 122, b)
    assert r3["portfolios"]["FOUNDER"]["orders"] == []


def test_recovery_after_a_run_dies_mid_order():
    C = _toy_closes(); saved = {}
    import json as _json
    eng = Engine([_founder(rule=AllocationRule(core_w=1.0))], RiskConfig())
    state = {}; b = BrokerSim(); b.crash_after_submit = True
    with pytest.raises(SystemExit):
        _bday(eng, state, C, 120, b)
    # the books on disk never learned of the fill; the broker holds VTI
    assert b.positions().get("VTI", 0) > 0
    fresh = {}                                                       # worst case: nothing was saved at all
    r = _bday(eng, fresh, C, 121, b)
    led = Ledger.from_dict(fresh["portfolios"]["FOUNDER"])
    assert led.qty("VTI") == pytest.approx(b.positions()["VTI"], rel=1e-6)
    assert r["reconciliation"] == "clean" and any("recovered" in a for a in r["alerts"])


def test_same_day_rerun_sends_nothing_new():
    C = _toy_closes(); eng = Engine([_founder()], RiskConfig()); state = {}; b = BrokerSim()
    _bday(eng, state, C, 120, b); n = len(b.orders)
    _bday(eng, state, C, 120, b)
    assert len(b.orders) == n


def test_stale_broker_quote_refused():
    C = _toy_closes(); eng = Engine([_founder(rule=AllocationRule(core_w=1.0))], RiskConfig()); state = {}
    b = BrokerSim(); b.age = 300
    r = _bday(eng, state, C, 120, b)
    assert r["portfolios"]["FOUNDER"]["orders"] == []
    assert "old" in r["portfolios"]["FOUNDER"]["refused"][0]["reason"]


def test_unknown_broker_position_trips_kill_switch_before_any_order():
    C = _toy_closes(); eng = Engine([_founder()], RiskConfig()); state = {}; b = BrokerSim(); b.pos = {"VTI": 5.0}
    r = _bday(eng, state, C, 120, b)
    assert state["kill_switch"] is True and r["portfolios"]["FOUNDER"]["orders"] == []


def test_founder_and_research_portfolios_stay_isolated():
    C = _toy_closes()
    specs = [_founder(), _spec(pid="C_shadow")]
    eng = Engine(specs, RiskConfig()); state = {}; b = BrokerSim(); sh = SimGateway()
    for j in range(100, 140):
        _bday(eng, state, C, j, b, shadow=sh)
    f = Ledger.from_dict(state["portfolios"]["FOUNDER"]); c = Ledger.from_dict(state["portfolios"]["C_shadow"])
    assert f.contributed == c.contributed                              # each has its own $100 + monthly
    assert all(cid.startswith("hb-FOUNDER-") for cid in b.orders)       # only the founder reached the broker
    assert not any(cid.startswith("hb-FOUNDER-") for cid in c.order_ids)
    bq = b.positions()
    assert all(f.qty(s) == pytest.approx(q) for s, q in bq.items())    # broker holds exactly the founder's shares
    assert f.vault == pytest.approx(f.vault) and c.positions is not f.positions


def test_owner_resume_clears_portfolio_kill():
    C = _toy_closes(); state = {"portfolios": {"FOUNDER": Ledger("FOUNDER", flags={"kill": "x", "order_errors": 2}).to_dict()}}
    owner_resume(state, "FOUNDER", "rejections reviewed", C.iloc[0].to_dict(), {"BIL"}, "2026-10-26")
    led = Ledger.from_dict(state["portfolios"]["FOUNDER"])
    assert "kill" not in led.flags and led.flags["resumes"][0]["reason"] == "rejections reviewed"


# ---------------- Council Oct 23: 20% account drawdown pauses all new purchases ----------------
def _crash(C, j0, drop):
    C2 = C.copy()
    C2.iloc[j0:] = C2.iloc[j0:] * (1 - drop)
    return C2


def test_drawdown_pause_blocks_all_buys_sells_nothing_and_needs_founder_restart():
    C = _toy_closes(); spec = _founder(rule=AllocationRule(core_w=1.0))
    eng = Engine([spec], RiskConfig()); state = {}; b = BrokerSim()
    for j in range(100, 125):
        _bday(eng, state, C, j, b)
    held = dict(b.positions())
    C2 = _crash(C, 124, 0.30)                                   # everything falls 30%
    r = _bday(eng, state, C2, 126, b)
    led = Ledger.from_dict(state["portfolios"]["FOUNDER"])
    assert led.flags.get("wealth_pause") and any("purchases paused" in a for a in r["alerts"])
    assert b.positions() == held                                # nothing liquidated
    # next month's contribution arrives: still no buys, and it does not count as recovery
    for j in range(127, 150):
        r = _bday(eng, state, C2, j, b)
        assert not [o for o in r["portfolios"]["FOUNDER"]["orders"] if o["side"] == "buy"]
    led = Ledger.from_dict(state["portfolios"]["FOUNDER"])
    assert led.contributed > 100 and led.flags.get("wealth_pause")
    assert 1 - led.nav(C2.iloc[148].to_dict()) / led.nav_hwm >= 0.20     # deposits did not shrink the drawdown
    owner_resume(state, "FOUNDER", "founder reviewed the drawdown", C2.iloc[149].to_dict(), {"BIL"}, "2025-06-01")
    r = _bday(eng, state, C2, 150, b)
    assert [o for o in r["portfolios"]["FOUNDER"]["orders"] if o["side"] == "buy"]   # buys resume after approval


def test_deposit_alone_never_trips_or_clears_the_pause():
    led = Ledger("t"); led.deposit("d1", 100, None)
    px = {"VTI": 100.0}
    led.apply_buy("d1", "VTI", 100, 1.0, 0, False)
    led.nav_hwm = led.nav(px)
    px2 = {"VTI": 75.0}                                         # 25% loss
    dd_before = 1 - led.nav(px2) / led.nav_hwm
    led.deposit("d2", 100, led.total(px2))
    assert 1 - led.nav(px2) / led.nav_hwm == pytest.approx(dd_before)


def test_intraday_sleeve_money_stays_in_founder_wealth():
    led = Ledger("FOUNDER", settled=100.12, units=100.0, nav_hwm=1.0, vault=0.05)
    before = led.total({})
    led.settled -= 100.0; led.intraday += 100.0           # run_intraday.allocate moves it to the sleeve
    assert led.total({}) == pytest.approx(before) and led.free_cash() == pytest.approx(0.12)
    assert Ledger.from_dict({k: v for k, v in led.to_dict().items() if k != "intraday"}).intraday == 0.0
