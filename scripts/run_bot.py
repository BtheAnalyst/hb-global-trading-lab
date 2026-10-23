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

if mode in ("letter", "midday"):
    # Stocks & ETFs reporting (Oct 2026): built from the stock/ETF engine's saved books. Options letters retired.
    import json as _json
    from hbgtl.equity_report import daily_letter as eq_letter, midday_email as eq_midday
    eq = _json.loads(Path("state/equity_state.json").read_text()) if Path("state/equity_state.json").exists() else {}
    eq["_journal"] = "state/equity_journal.jsonl"
    label = f"{now:%a %b} {now.day}"
    Path("dashboard").mkdir(exist_ok=True)
    if mode == "letter":
        subject, html, md = eq_letter(eq, str(now.date()), label)
        Path("letter.md").write_text(md); print(md)
        Path("dashboard/letter_email.html").write_text(html)
        Path("dashboard/letter_subject.txt").write_text(subject)
    else:
        res = eq_midday(eq, str(now.date()), label)
        if res is None:
            print("SKIP: no engine pass has run today; no midday email.")
        else:
            Path("dashboard/midday_email.html").write_text(res[1])
            Path("dashboard/midday_subject.txt").write_text(res[0])
            print("Midday email built:", res[0])
    sys.exit(0)

if mode == "letter_options_retired":
    Path("letter.md").write_text(daily_letter(cfg, state, now.date()))
    print(Path("letter.md").read_text())
    from hbgtl.email_letter import daily_email
    subject, html = daily_email(cfg, state, now.date())
    Path("dashboard").mkdir(exist_ok=True)
    Path("dashboard/letter_email.html").write_text(html)
    Path("dashboard/letter_subject.txt").write_text(subject)
elif mode == "midday":
    from hbgtl.email_letter import midday_email
    res = midday_email(cfg, state, now)
    Path("dashboard").mkdir(exist_ok=True)
    if res is None:
        print("SKIP: the bot has not run during market hours today; no midday email.")
    else:
        Path("dashboard/midday_email.html").write_text(res[1])
        Path("dashboard/midday_subject.txt").write_text(res[0])
        print("Midday email built:", res[0])
else:
    from hbgtl.gateway import AlpacaPaperGateway
    from hbgtl.runner import run
    gw = AlpacaPaperGateway()
    lines = run(gw, cfg, state, now, mode=mode, persist=lambda: st.save(state))
    for ln in lines:
        print(ln)
        print(f"::notice title=Atelier {mode}::{ln}")

if mode not in ("scan", "midday"):
    st.save(state)
