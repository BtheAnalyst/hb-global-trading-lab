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


# ---------- scripts/start_letter.sh (after-close Daily Letter starter) ----------

def _start_letter(tmp_path, hm, dow, posted_titles):
    bin_ = tmp_path / "bin"; bin_.mkdir(exist_ok=True)
    (bin_ / "date").write_text(f"""#!/bin/sh
case "$1" in +%H%M) echo {hm};; +%u) echo {dow};; *) echo 'Thu Oct 22, 2026';; esac
""")
    titles = "\\n".join(posted_titles)
    (bin_ / "gh").write_text(f"""#!/bin/sh
if [ "$1" = issue ]; then printf '{titles}\\n'; else echo "$@" >> {tmp_path}/gh_calls; fi
""")
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", str(ROOT / "scripts/start_letter.sh")], env=env, capture_output=True, text=True)
    calls = (tmp_path / "gh_calls").read_text() if (tmp_path / "gh_calls").exists() else ""
    return r, calls


def test_letter_started_once_after_close(tmp_path):
    r, calls = _start_letter(tmp_path, "1634", "4", [])
    assert r.returncode == 0 and "workflow run atelier-daily-letter.yml" in calls


def test_letter_not_started_twice(tmp_path):
    r, calls = _start_letter(tmp_path, "1704", "4", ["The Daily Letter · Thu Oct 22, 2026"])
    assert r.returncode == 0 and calls == ""


def test_letter_not_started_during_market_or_weekend(tmp_path):
    assert _start_letter(tmp_path, "1404", "4", [])[1] == ""
    assert _start_letter(tmp_path, "1634", "6", [])[1] == ""


# ---------- scripts/relay.py (replacement scheduler) ----------

def _relay():
    import importlib.util
    spec = importlib.util.spec_from_file_location("relay", ROOT / "scripts/relay.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_relay_slots_cover_the_session():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    r = _relay()
    day = datetime(2026, 10, 22, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    s = [f"{x:%H:%M}" for x in r.slots(day)]
    assert s[0] == "09:34" and s[-1] == "16:34" and len(s) == 15


def test_relay_next_slot():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    r, ET = _relay(), ZoneInfo("America/New_York")
    assert f"{r.next_slot(datetime(2026, 10, 22, 9, 0, tzinfo=ET)):%H:%M}" == "09:34"
    assert f"{r.next_slot(datetime(2026, 10, 22, 14, 6, tzinfo=ET)):%H:%M}" == "14:04"   # within grace
    assert f"{r.next_slot(datetime(2026, 10, 22, 14, 10, tzinfo=ET)):%H:%M}" == "14:34"
    assert r.next_slot(datetime(2026, 10, 22, 16, 40, tzinfo=ET)) is None
    assert r.next_slot(datetime(2026, 10, 24, 11, 0, tzinfo=ET)) is None                 # Saturday
