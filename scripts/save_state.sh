#!/usr/bin/env bash
# Commit and push the bot's saved state back to the repo after a GitHub Actions run.
#
#   scripts/save_state.sh <mode> <path> [path ...]
#
# Fails the job loudly (never a silent success) when:
#   - a saving run (trade / letter) did not leave a valid state/state.json
#   - a listed path cannot be staged
#   - the push still fails after retries, or the remote does not end up with our commit
# Scan mode saves nothing by design (it is a dry run), and says so.
set -euo pipefail

mode="${1:?mode required}"; shift
branch="${GITHUB_REF_NAME:-main}"

if [ "$mode" = "scan" ]; then
  echo "Scan mode is a dry run: nothing is saved, by design."
  exit 0
fi

if [ ! -f state/state.json ]; then
  echo "::error title=State not saved::state/state.json is missing after a '$mode' run"
  exit 1
fi
python -c "import json,sys; json.load(open('state/state.json'))" \
  || { echo "::error title=State not saved::state/state.json is not valid JSON"; exit 1; }

git config user.name "HB Atelier Bot"
git config user.email "atelier-bot@users.noreply.github.com"

to_add=()
for p in "$@"; do
  if [ -e "$p" ]; then
    to_add+=("$p")
  elif [ "$p" = "state/" ] || [ "$p" = "state/state.json" ]; then
    echo "::error title=State not saved::required path '$p' is missing"; exit 1
  else
    echo "Optional path '$p' not present this run; skipping."
  fi
done
git add -- "${to_add[@]}"

if git diff --cached --quiet; then
  echo "State unchanged since the last saved run: nothing to commit."
  exit 0
fi
git commit -q -m "Atelier $mode state $(date -u +%FT%H:%MZ)"

for attempt in 1 2 3 4; do
  if git pull -q --rebase origin "$branch" && git push -q origin "HEAD:$branch"; then
    git fetch -q origin "$branch"
    if [ "$(git rev-parse HEAD)" = "$(git rev-parse FETCH_HEAD)" ]; then
      echo "State saved and pushed: $(git rev-parse --short HEAD)"
      exit 0
    fi
  fi
  git rebase --abort >/dev/null 2>&1 || true
  echo "Push attempt $attempt failed; retrying."
  sleep $((attempt * 5))
done
echo "::error title=State not saved::could not push saved state to $branch after 4 attempts"
exit 1
