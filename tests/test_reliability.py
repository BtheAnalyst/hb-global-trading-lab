"""Reliability tests: scan diagnostics, state round-trip across processes, and the save script."""
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from hbgtl import state as st
from hbgtl.runner import run
from tests.test_runner import NOW, FakeGW

ROOT = Path(__file__).resolve().parents[1]
SAVE = ROOT / "scripts" / "save_state.sh"


@pytest.fixture
def cfg():
    return yaml.safe_load(open(ROOT / "config/settings.yaml"))


def test_scan_reports_every_symbol_and_every_contract_reason(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW(ask=3.00, bid=2.95)
    out = run(gw, cfg, s, NOW, mode="scan")
    assert gw.sent == []                                         # scan never trades
    for sym in cfg["trading"]["universe"]:
        assert any(ln.startswith(f"[diag] {sym} ") for ln in out), sym
    rejected = [ln for ln in out if "REJECTED" in ln]
    assert rejected and "unaffordable" in rejected[0] and "quote age" in rejected[0]


def test_scan_flags_quotes_too_old_for_live_trading(cfg):
    s, gw = st.new_state(cfg, NOW.date()), FakeGW(quote_time=NOW - timedelta(minutes=15))
    out = run(gw, cfg, s, NOW, mode="scan")
    line = next(ln for ln in out if "REJECTED" in ln)
    assert "stale" in line and "900s" in line


def test_scan_does_not_loosen_limits(cfg):
    before = json.dumps(cfg, sort_keys=True)
    run(FakeGW(), cfg, st.new_state(cfg, NOW.date()), NOW, mode="scan")
    assert json.dumps(cfg, sort_keys=True) == before


def test_state_survives_a_separate_process(cfg, tmp_path):
    p = tmp_path / "state" / "state.json"
    s = st.new_state(cfg, NOW.date())
    s["log"].append("run 1")
    s["pending"] = {"client_order_id": "A-buy-X-1"}
    st.save(s, str(p))
    code = ("import json,sys; from datetime import date; sys.path.insert(0, %r);"
            "from hbgtl import state as st; s = st.load({}, date.today(), %r);"
            "print(json.dumps([s['log'], s['pending']]))") % (str(ROOT), str(p))
    got = json.loads(subprocess.check_output([sys.executable, "-c", code], text=True))
    assert got == [["run 1"], {"client_order_id": "A-buy-X-1"}]


# ---------- scripts/save_state.sh against a real (local) git remote ----------

def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    _git(work, "config", "user.email", "t@t"); _git(work, "config", "user.name", "t")
    (work / "README").write_text("x")
    _git(work, "add", "."); _git(work, "commit", "-qm", "init"); _git(work, "push", "-q", "origin", "HEAD:main")
    return work, remote


def _save(work, *args):
    env = {**os.environ, "GITHUB_REF_NAME": "main"}
    return subprocess.run(["bash", str(SAVE), *args], cwd=work, env=env, capture_output=True, text=True)


def test_save_pushes_state_and_next_run_reads_it(repo, tmp_path):
    work, remote = repo
    (work / "state").mkdir()
    (work / "state/state.json").write_text(json.dumps({"log": ["run 1"]}))
    r = _save(work, "trade", "state/")
    assert r.returncode == 0, r.stdout + r.stderr
    fresh = tmp_path / "fresh"                                   # a brand-new checkout = the next run
    _git(tmp_path, "clone", "-q", str(remote), str(fresh))
    assert json.loads((fresh / "state/state.json").read_text()) == {"log": ["run 1"]}


def test_save_fails_loudly_when_state_missing(repo):
    work, _ = repo
    r = _save(work, "trade", "state/")
    assert r.returncode != 0 and "missing" in r.stdout


def test_save_fails_loudly_on_corrupt_state(repo):
    work, _ = repo
    (work / "state").mkdir()
    (work / "state/state.json").write_text("{not json")
    assert _save(work, "trade", "state/").returncode != 0


def test_save_fails_loudly_when_push_impossible(repo, tmp_path):
    work, remote = repo
    (work / "state").mkdir()
    (work / "state/state.json").write_text("{}")
    _git(work, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    r = subprocess.run(["bash", "-c", f"sed 's/sleep .*/true/' {SAVE} > s.sh && bash s.sh trade state/"],
                       cwd=work, env={**os.environ, "GITHUB_REF_NAME": "main"}, capture_output=True, text=True)
    assert r.returncode != 0 and "could not push" in r.stdout


def test_scan_saves_nothing_and_says_so(repo):
    work, _ = repo
    r = _save(work, "scan", "state/")
    assert r.returncode == 0 and "nothing is saved" in r.stdout
