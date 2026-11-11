#!/usr/bin/env bash
# Intune backup for GitHub/Gitea Actions: export, filter noise, reports, validate, commit + push drift.
# Run from the repo root after checkout, with GRAPH_TOKEN set. Outputs (GITHUB_OUTPUT):
# CHANGE_DETECTED, ROLLING_PR_SYNC_REQUIRED, ... (from commit_intune_drift.ps1).
# Env: BASELINE_BRANCH, DRIFT_BRANCH_INTUNE, BACKUP_TIMEZONE, FULL_RUN_HOUR, FORCE_FULL_RUN, plus
# run_intune_backup.sh / commit_intune_drift.ps1 inputs. Azure DevOps runs the same steps individually.
set -euo pipefail
source "$(dirname "$0")/ci.sh"
: "${GRAPH_TOKEN:?GRAPH_TOKEN is required}"

export BACKUP_FOLDER="${BACKUP_FOLDER:-tenant-state}" REPORTS_SUBDIR="${REPORTS_SUBDIR:-reports}"
export INTUNE_BACKUP_SUBDIR="${INTUNE_BACKUP_SUBDIR:-intune}" ENTRA_BACKUP_SUBDIR="${ENTRA_BACKUP_SUBDIR:-entra}"
export BASELINE_BRANCH="${BASELINE_BRANCH:-main}" DRIFT_BRANCH_INTUNE="${DRIFT_BRANCH_INTUNE:-drift/intune}"
root="$BACKUP_FOLDER/$INTUNE_BACKUP_SUBDIR"
reports="$BACKUP_FOLDER/$REPORTS_SUBDIR/intune"

git checkout --quiet --force -B "$DRIFT_BRANCH_INTUNE" "origin/$BASELINE_BRANCH"

if [ "${FORCE_FULL_RUN:-false}" = "true" ] || [ "$(TZ="${BACKUP_TIMEZONE:-UTC}" date +%H)" = "${FULL_RUN_HOUR:-02}" ]; then
  RUN_MODE=full
else
  RUN_MODE=light
fi
echo "Run mode: $RUN_MODE"

rm -rf "$root" "$reports"
mkdir -p "$root"
export BACKUP_START="$(date -u +%Y.%m.%d:%H.%M.%S)"
INTUNE_ROOT="$PWD/$root" bash scripts/run_intune_backup.sh

python3 scripts/filter_intune_partial_settings_noise.py --repo-root "$PWD" --backup-root "$PWD/$root" \
  --baseline-ref "origin/$BASELINE_BRANCH" --fail-on-unresolved-partial-exports true
python3 scripts/filter_intune_formatting_noise.py --repo-root "$PWD" --backup-root "$PWD/$root" --baseline-ref "origin/$BASELINE_BRANCH"
python3 scripts/filter_intune_omission_noise.py --repo-root "$PWD" --backup-root "$PWD/$root" --baseline-ref "origin/$BASELINE_BRANCH"

BUILD_SOURCESDIRECTORY="$PWD" BACKUP_FOLDER="$root" ACCESS_TOKEN="$GRAPH_TOKEN" python3 scripts/resolve_assignment_group_names.py

python3 scripts/generate_assignment_report.py --root "$root" --output-dir "$reports"
python3 scripts/generate_object_inventory_reports.py --root "$root" --output-dir "$reports"
if [ "${ENABLE_WORKLOAD_ENTRA:-true}" = "false" ] && { [ "${ENTRA_INCLUDE_APP_REGISTRATIONS:-true}" = "true" ] || [ "${ENTRA_INCLUDE_ENTERPRISE_APPS:-true}" = "true" ]; }; then
  python3 scripts/generate_app_inventory_report.py --root "$BACKUP_FOLDER/$ENTRA_BACKUP_SUBDIR" --output-dir "$BACKUP_FOLDER/$REPORTS_SUBDIR/entra"
fi

python3 scripts/validate_backup_outputs.py --workload intune --mode "$RUN_MODE" --root "$root" --reports-root "$reports"

REPO_ROOT="$PWD" pwsh -NoProfile -NonInteractive -File scripts/commit_intune_drift.ps1
