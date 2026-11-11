#!/usr/bin/env bash
# Normalize the restore payload and run IntuneCD update (or a dry-run report).
# Env: RESTORE_SOURCE_PATH, GRAPH_TOKEN, DRY_RUN, UPDATE_ASSIGNMENTS, REMOVE_UNMANAGED, ENTRA_UPDATE,
#      MAX_WORKERS, EXCLUDE_CSV. Shared by azure-pipelines-restore.yml and .github/workflows/astral-restore.yml.
set -euo pipefail
source "$(dirname "$0")/ci.sh"
echo "RESTORE_SCRIPT_VERSION=2026-11-10.1"

: "${RESTORE_SOURCE_PATH:?RESTORE_SOURCE_PATH is required}"
: "${GRAPH_TOKEN:?GRAPH_TOKEN is required}"

to_lower() {
  echo "$1" | tr '[:upper:]' '[:lower:]'
}

DRY_RUN="$(to_lower "${DRY_RUN:-true}")"
UPDATE_ASSIGNMENTS="$(to_lower "${UPDATE_ASSIGNMENTS:-true}")"
REMOVE_UNMANAGED="$(to_lower "${REMOVE_UNMANAGED:-false}")"
ENTRA_UPDATE="$(to_lower "${ENTRA_UPDATE:-false}")"
EXCLUDE_CSV="${EXCLUDE_CSV:-}"
MAX_WORKERS="${MAX_WORKERS:-10}"
RESTORE_PATH="$RESTORE_SOURCE_PATH"

RESTORE_PATH_ENV="$RESTORE_PATH" python3 "$(dirname "$0")/restore_normalize_payload.py"

cmd=(
  IntuneCD-startupdate
  --token "$GRAPH_TOKEN"
  --mode=1
  --path "$RESTORE_PATH"
  --exit-on-error
)

if [ "$DRY_RUN" = "true" ]; then
  cmd+=(--report)
fi
if [ "$UPDATE_ASSIGNMENTS" = "true" ]; then
  cmd+=(--update-assignments)
fi
if [ "$REMOVE_UNMANAGED" = "true" ]; then
  cmd+=(--remove)
fi
if [ "$ENTRA_UPDATE" = "true" ]; then
  cmd+=(--entraupdate)
fi

EXCLUDE_CSV_TRIMMED="$(echo "$EXCLUDE_CSV" | xargs)"
EXCLUDE_CSV_NORMALIZED="$(echo "$EXCLUDE_CSV_TRIMMED" | tr '[:upper:]' '[:lower:]')"
if [ "$EXCLUDE_CSV_NORMALIZED" = "none" ] || [ "$EXCLUDE_CSV_NORMALIZED" = "null" ] || [ "$EXCLUDE_CSV_NORMALIZED" = "n/a" ] || [ "$EXCLUDE_CSV_NORMALIZED" = "-" ] || [ "$EXCLUDE_CSV_NORMALIZED" = "_none_" ]; then
  EXCLUDE_CSV_TRIMMED=""
fi

exclude_items=()
if [ -n "$EXCLUDE_CSV_TRIMMED" ]; then
  IFS=',' read -r -a raw_items <<< "$EXCLUDE_CSV_TRIMMED"
  for item in "${raw_items[@]}"; do
    trimmed="$(echo "$item" | xargs)"
    if [ -n "$trimmed" ]; then
      exclude_items+=("$trimmed")
    fi
  done
fi

has_dms_exclude=0
for item in "${exclude_items[@]}"; do
  if [ "$(echo "$item" | tr '[:upper:]' '[:lower:]')" = "devicemanagementsettings" ]; then
    has_dms_exclude=1
    break
  fi
done
if [ "$has_dms_exclude" -eq 0 ]; then
  exclude_items+=("DeviceManagementSettings")
  echo "Auto-excluding DeviceManagementSettings (IntuneCD update requires interactive auth for this category)."
fi

if [ "${#exclude_items[@]}" -gt 0 ]; then
  cmd+=(--exclude)
  cmd+=("${exclude_items[@]}")
fi

echo "Restore command mode: dryRun=$DRY_RUN updateAssignments=$UPDATE_ASSIGNMENTS remove=$REMOVE_UNMANAGED entraupdate=$ENTRA_UPDATE maxWorkers=$MAX_WORKERS sourcePath=$RESTORE_PATH"
if [ "${#exclude_items[@]}" -gt 0 ]; then
  joined_excludes="$(IFS=,; echo "${exclude_items[*]}")"
  echo "Excluding categories: $joined_excludes"
fi

intunecd_log="${AGENT_TEMPDIRECTORY:-${RUNNER_TEMP:-/tmp}}/intunecd-restore.log"
rm -f "$intunecd_log"
set +e
"${cmd[@]}" >"$intunecd_log" 2>&1
intunecd_rc=$?
set -e
echo "IntuneCD exit code captured: $intunecd_rc"

if [ "$intunecd_rc" -ne 0 ]; then
  echo "IntuneCD restore/update failed with exit code: $intunecd_rc"
  marker_pattern="error|\\[ERROR\\]|\\[CRITICAL\\]|request failed|failed with status|modelvalidationfailure|traceback|exception|error updating|failed after|unable to|forbidden|unauthorized"
  marker_count="$(grep -Eic "$marker_pattern" "$intunecd_log" || true)"
  echo "Detected error-marker lines: $marker_count"
  echo "Relevant markers from full output (line:number:text):"
  grep -Ein "$marker_pattern" "$intunecd_log" | tail -n 200 || true
  echo "First 80 lines of IntuneCD output:"
  head -n 80 "$intunecd_log" || true
  echo "Last 120 lines of IntuneCD output:"
  tail -n 120 "$intunecd_log" || true
  if [ "${marker_count:-0}" -eq 0 ]; then
    ci_log warning "IntuneCD returned non-zero without explicit error markers; treating as successful no-op."
    intunecd_rc=0
  fi
else
  echo "Last 60 lines of IntuneCD output:"
  tail -n 60 "$intunecd_log" || true
fi

if [ "$intunecd_rc" -ne 0 ]; then
  exit "$intunecd_rc"
fi
