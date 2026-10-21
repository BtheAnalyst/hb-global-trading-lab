"""Entry point for GitHub Actions.

  python scripts/run_bot.py trade   # one trading pass (does nothing if the market is closed)
  python scripts/run_bot.py scan    # dry run: signals + contract picks, NEVER sends orders
  python scripts/run_bot.py letter  # write the Daily Letter to letter.md
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from hbgtl import state as st  # noqa: E402
from hbgtl.letter import daily_letter  # noqa: E402
from hbgtl.risk import assert_paper_or_approved  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
cfg = yaml.safe_load(open("config/settings.yaml"))
assert_paper_or_approved(cfg["mode"])
now = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
state = st.load(cfg, now.date())

if mode == "letter":
    Path("letter.md").write_text(daily_letter(cfg, state, now.date()))
    print(Path("letter.md").read_text())
else:
    from hbgtl.gateway import AlpacaPaperGateway
    from hbgtl.runner import run
    gw = AlpacaPaperGateway()
    lines = run(gw, cfg, state, now, mode=mode, persist=lambda: st.save(state))
    for ln in lines:
        print(ln)
        print(f"::notice title=Atelier {mode}::{ln}")

if mode != "scan":
    st.save(state)
