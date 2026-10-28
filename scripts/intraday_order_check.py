"""Order-handling check on Alpaca PAPER (no fill possible): sends ONE 1-share bracket buy for F with the entry
limit ~40% below the market, confirms the broker accepted it with a stop leg and a take-profit leg, cancels
it, and confirms everything is canceled and nothing is held. Also runs the intraday pre-market scan once."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from hbgtl.intraday_engine import IntradayEngine, new_state, profiles_from_bars  # noqa: E402
from hbgtl.intraday_gateway import AlpacaIntradayGateway  # noqa: E402
from hbgtl.intraday_universe import RESEARCH_POOL  # noqa: E402

ok = True
gw = AlpacaIntradayGateway("iex")
q = gw.quote("F")
print(f"::notice title=Quote F::bid {q[0]} ask {q[1]} age {q[2]:.0f}s")
ref = q[1] or q[0]
if not ref:
    ref = float(gw.daily_sip(["F"], 10)["F"]["close"].iloc[-1])
cid = f"it-ordertest-{int(time.time())}"
lim = round(ref * 0.6, 2)
o = gw.submit_bracket(cid, "F", 1, lim, round(lim * 0.98, 2), round(lim * 1.04, 2))
time.sleep(2)
o = gw.order(cid)
legs = {x["type"]: x["status"] for x in o["legs"]}
print(f"::notice title=Bracket accepted::status {o['status']} legs {legs}")
ok &= o["status"] in ("new", "accepted", "pending_new") and "stop" in legs and "limit" in legs
gw.cancel(o["id"])
for _ in range(10):
    time.sleep(1)
    o = gw.order(cid)
    if o["status"] == "canceled" and all(x["status"] == "canceled" for x in o["legs"]):
        break
legs = {x["type"]: x["status"] for x in o["legs"]}
print(f"::notice title=Bracket canceled::status {o['status']} legs {legs}; F position {gw.position_qty('F')}")
ok &= o["status"] == "canceled" and gw.position_qty("F") == 0

# pre-market scan, exactly as the session job does it (nothing saved)
cfg = yaml.safe_load((ROOT / "config" / "intraday.yaml").read_text())
eng = IntradayEngine(cfg, gw, new_state(), lambda r: None, lambda: None)
today = pd.Timestamp.now(tz="America/New_York").date().isoformat()
pool = sorted(set(RESEARCH_POOL))
daily = gw.daily_sip(pool, 45)
daily = {k: v[pd.to_datetime(v["timestamp"], utc=True).dt.tz_convert("America/New_York").dt.date.astype(str) < today]
         for k, v in daily.items()}
five = gw.bars(pool, datetime.now(timezone.utc) - timedelta(days=35), minutes=5)
prof = profiles_from_bars(five, today)
scan = eng.start_day(today, pool, daily, prof, gw.assets(pool))
print(f"::notice title=Scan::{len(scan['eligible'])}/{len(pool)} eligible; profiles {len(prof)}; "
      f"eligible sample {scan['eligible'][:25]}")
reasons = {}
for r in scan["excluded"].values():
    k = r.split(" ")[0] + " " + (r.split(" ")[1] if len(r.split(" ")) > 1 else "")
    reasons[k] = reasons.get(k, 0) + 1
print(f"::notice title=Excluded::{json.dumps(reasons)}")
ok &= len(scan["eligible"]) >= 20
print(f"::{'notice' if ok else 'error'} title=Order test::{'PASS' if ok else 'FAIL'}")
sys.exit(0 if ok else 1)
