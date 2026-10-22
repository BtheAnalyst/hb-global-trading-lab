"""Scheduler relay: starts the trading workflow every 30 minutes during market hours.

Why this exists: GitHub's built-in cron never fired for this repo. A workflow may,
however, start another workflow with its own GITHUB_TOKEN (workflow_dispatch is the
documented exception to the "GITHUB_TOKEN events don't start runs" rule). So one
relay job stays awake during the session and dispatches atelier-trading.yml in
trade mode at :04 and :34 past the hour, 9:34 AM - 4:34 PM ET on weekdays.

Safety: the relay only *starts* the normal trading workflow. Every run still goes
through the bot's own market-clock check, risk engine and saved state; trading
runs share one concurrency group and each loads the latest saved state, so even a
duplicate start cannot create a duplicate order. Paper only, as everywhere else.

GitHub jobs are capped at 6 hours, so before the cap the relay starts a fresh copy
of itself (the relay's concurrency group keeps exactly one relay alive).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FIRST = (9, 34)
LAST = (16, 34)
HANDOFF_AFTER = timedelta(hours=5, minutes=20)   # well under the 6-hour job cap


def slots(day: datetime) -> list[datetime]:
    """All :04/:34 slots from 9:34 to 16:34 ET on the given day (ET-aware)."""
    out, t = [], day.replace(hour=FIRST[0], minute=FIRST[1], second=0, microsecond=0)
    end = day.replace(hour=LAST[0], minute=LAST[1], second=0, microsecond=0)
    while t <= end:
        out.append(t)
        t += timedelta(minutes=30)
    return out


def next_slot(now: datetime, grace: timedelta = timedelta(minutes=3),
              last_fired: datetime | None = None) -> datetime | None:
    """The next slot to fire. A slot that passed less than `grace` ago still counts,
    but a slot this relay already fired never fires again."""
    if now.weekday() >= 5:
        return None
    for s in slots(now):
        if last_fired is not None and s <= last_fired:
            continue
        if s >= now - grace:
            return s
    return None


def gh(*args: str) -> bool:
    for attempt in range(3):
        r = subprocess.run(["gh", *args], capture_output=True, text=True)
        if r.returncode == 0:
            return True
        print(f"gh {' '.join(args)} failed (attempt {attempt + 1}): {r.stderr.strip()}", flush=True)
        time.sleep(10 * (attempt + 1))
    return False


def main() -> int:
    started = datetime.now(ET)
    failures = 0
    last_fired: datetime | None = None
    print(f"Relay started {started:%a %Y-%m-%d %H:%M:%S} ET", flush=True)
    while True:
        now = datetime.now(ET)
        s = next_slot(now, last_fired=last_fired)
        if s is None:
            print("No more slots today: relay done.", flush=True)
            break
        if s - started > HANDOFF_AFTER:
            ok = gh("workflow", "run", "atelier-relay.yml", "--ref", "main")
            print(f"Handing off to a fresh relay before the 6-hour cap: {'ok' if ok else 'FAILED'}", flush=True)
            if not ok:
                print("::error title=Relay handoff failed::the next relay could not be started", flush=True)
                return 1
            return 1 if failures else 0
        wait = (s - now).total_seconds()
        if wait > 0:
            print(f"Next start at {s:%H:%M} ET (sleeping {wait / 60:.1f} min)", flush=True)
            time.sleep(wait)
        ok = gh("workflow", "run", "atelier-trading.yml", "--ref", "main", "-f", "mode=trade")
        last_fired = s
        stamp = datetime.now(ET)
        if ok:
            print(f"{stamp:%H:%M:%S} ET: started trading run for the {s:%H:%M} slot", flush=True)
        else:
            failures += 1
            print(f"::error title=Trading run not started::{s:%H:%M} ET slot", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    if os.environ.get("RELAY_DRY_RUN"):
        print([f"{s:%H:%M}" for s in slots(datetime.now(ET))])
        sys.exit(0)
    sys.exit(main())
