#!/usr/bin/env bash
# Called at the end of each trading run. Between 4:30 and 6:00 PM ET on a weekday,
# starts the Daily Letter workflow unless today's Letter is already posted.
# (The Letter workflow re-checks before posting, so a double start can't double-post.)
set -euo pipefail
hm=$(TZ=America/New_York date +%H%M)
dow=$(TZ=America/New_York date +%u)
if [ "$dow" -gt 5 ] || [ "$hm" -lt 1630 ] || [ "$hm" -ge 1800 ]; then
  echo "Not the after-close window ($hm ET): no Letter from this run."; exit 0
fi
title="The Daily Letter · $(TZ=America/New_York date +'%a %b %-d, %Y')"
if gh issue list --label daily-letter --state all --limit 30 --json title -q '.[].title' | grep -qxF "$title"; then
  echo "Today's Letter is already posted."; exit 0
fi
gh workflow run atelier-daily-letter.yml --ref main
echo "Daily Letter started."
