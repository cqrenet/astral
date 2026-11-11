#!/usr/bin/env bash
# Change probe for GitHub/Gitea Actions: load debouncer state from a state branch, poll audit logs,
# save the state (one orphan commit, force-pushed), and dispatch the backup workflow when triggered.
# Env: GRAPH_TOKEN, FORGE_TOKEN, GITHUB_API_URL, GITHUB_REPOSITORY, BASELINE_BRANCH,
#      PROBE_STATE_BRANCH (astral-probe-state), PROBE_QUIET_WINDOW_MINUTES, PROBE_COOLDOWN_MINUTES, BACKUP_WORKFLOW.
set -euo pipefail
source "$(dirname "$0")/ci.sh"
: "${GRAPH_TOKEN:?GRAPH_TOKEN is required}" "${FORGE_TOKEN:?FORGE_TOKEN is required}"

branch="${PROBE_STATE_BRANCH:-astral-probe-state}"
work="${RUNNER_TEMP:-/tmp}/astral-probe"
mkdir -p "$work"
state="$work/state.json"
rm -f "$state"
if git fetch --quiet origin "refs/heads/$branch" 2>/dev/null; then
  git show FETCH_HEAD:state.json > "$state" 2>/dev/null || rm -f "$state"
fi

python3 scripts/probe_tenant_changes.py --state-path "$state" \
  --quiet-window-minutes "${PROBE_QUIET_WINDOW_MINUTES:-15}" --cooldown-minutes "${PROBE_COOLDOWN_MINUTES:-30}" > "$work/result.json"
trigger="$(python3 - "$work/result.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
print(f"reason: {result.get('reason')}", file=sys.stderr)
print("true" if result.get("trigger") else "false")
PY
)"

# Persist state without touching the working tree or keeping history.
blob="$(git hash-object -w "$state")"
tree="$(printf '100644 blob %s\tstate.json\n' "$blob" | git mktree)"
commit="$(GIT_AUTHOR_NAME="ASTRAL probe" GIT_AUTHOR_EMAIL="astral-probe@local.invalid" \
  GIT_COMMITTER_NAME="ASTRAL probe" GIT_COMMITTER_EMAIL="astral-probe@local.invalid" \
  git commit-tree "$tree" -m "ASTRAL probe state")"
git push --quiet --force origin "$commit:refs/heads/$branch"

echo "trigger=$trigger"
if [ "$trigger" = "true" ]; then
  curl -fsS -X POST -H "Authorization: token $FORGE_TOKEN" -H "Accept: application/json" -H "Content-Type: application/json" \
    "${GITHUB_API_URL:?}/repos/${GITHUB_REPOSITORY:?}/actions/workflows/${BACKUP_WORKFLOW:-astral-backup.yml}/dispatches" \
    -d "{\"ref\":\"${BASELINE_BRANCH:-main}\"}"
  echo "Backup dispatched."
fi
