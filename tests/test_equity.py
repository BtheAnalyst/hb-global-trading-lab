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
def test_execution_disabled_and_no_broker_portfolio_enabled():
    assert CFG["execution_enabled"] is False
    assert not [p for p in specs_from_cfg(CFG) if p.mode == "broker" and p.enabled]


def test_allocation_is_configurable_not_hardcoded():
    weights = {s.rule.core_w for s in specs_from_cfg(CFG)}
    assert {1.0, 0.9, 0.8, 0.7} <= weights
    assert {s.vault_pct for s in specs_from_cfg(CFG)} == {0.0, 0.30}


def test_scheduled_workflow_has_no_schedule():
    wf = (ROOT / ".github" / "workflows" / "equity-engine.yml").read_text()
    assert "schedule" not in yaml.safe_load(wf)[True]   # 'on:' parses as True in YAML 1.1


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
