#!/usr/bin/env bash
# Entra backup for GitHub/Gitea Actions: export, resolve references, reports, validate, filter
# enrichment noise, commit + push drift. Run from the repo root after checkout, with GRAPH_TOKEN set.
# Outputs (GITHUB_OUTPUT): CHANGE_DETECTED, ROLLING_PR_SYNC_REQUIRED (from commit_entra_drift.py).
set -euo pipefail
source "$(dirname "$0")/ci.sh"
: "${GRAPH_TOKEN:?GRAPH_TOKEN is required}"

export BACKUP_FOLDER="${BACKUP_FOLDER:-tenant-state}" REPORTS_SUBDIR="${REPORTS_SUBDIR:-reports}"
export ENTRA_BACKUP_SUBDIR="${ENTRA_BACKUP_SUBDIR:-entra}"
export BASELINE_BRANCH="${BASELINE_BRANCH:-main}" DRIFT_BRANCH_ENTRA="${DRIFT_BRANCH_ENTRA:-drift/entra}"
export ENTRA_INCLUDE_NAMED_LOCATIONS="${ENTRA_INCLUDE_NAMED_LOCATIONS:-true}"
export ENTRA_INCLUDE_AUTHENTICATION_STRENGTHS="${ENTRA_INCLUDE_AUTHENTICATION_STRENGTHS:-true}"
export ENTRA_INCLUDE_CONDITIONAL_ACCESS="${ENTRA_INCLUDE_CONDITIONAL_ACCESS:-true}"
export ENTRA_INCLUDE_ENTERPRISE_APPS="${ENTRA_INCLUDE_ENTERPRISE_APPS:-true}"
export ENTRA_INCLUDE_APP_REGISTRATIONS="${ENTRA_INCLUDE_APP_REGISTRATIONS:-true}"
root="$BACKUP_FOLDER/$ENTRA_BACKUP_SUBDIR"
reports="$BACKUP_FOLDER/$REPORTS_SUBDIR/entra"

git checkout --quiet --force -B "$DRIFT_BRANCH_ENTRA" "origin/$BASELINE_BRANCH"

full=0
if ci_is_full_run; then full=1; fi
RUN_MODE=$([ "$full" = 1 ] && echo full || echo light)
# App categories are exported in full runs only; light runs keep them from the drift branch.
effective_in_this_run() { if [ "$1" = "true" ] && [ "$full" = 1 ]; then echo true; else echo false; fi; }
ENTRA_INCLUDE_ENTERPRISE_APPS_EFFECTIVE="$(effective_in_this_run "$ENTRA_INCLUDE_ENTERPRISE_APPS")"
ENTRA_INCLUDE_APP_REGISTRATIONS_EFFECTIVE="$(effective_in_this_run "$ENTRA_INCLUDE_APP_REGISTRATIONS")"
export ENTRA_INCLUDE_ENTERPRISE_APPS_EFFECTIVE ENTRA_INCLUDE_APP_REGISTRATIONS_EFFECTIVE
echo "Run mode (Entra): $RUN_MODE; enterprise apps: $ENTRA_INCLUDE_ENTERPRISE_APPS_EFFECTIVE; app registrations: $ENTRA_INCLUDE_APP_REGISTRATIONS_EFFECTIVE"

bash scripts/reset_entra_export.sh
BACKUP_START="$(date -u +%Y.%m.%d:%H.%M.%S)"

timeout 45m python3 -u scripts/export_entra_baseline.py \
  --root "$root" \
  --previous-snapshot-ref "origin/$DRIFT_BRANCH_ENTRA" \
  --include-named-locations "$ENTRA_INCLUDE_NAMED_LOCATIONS" \
  --include-authentication-strengths "$ENTRA_INCLUDE_AUTHENTICATION_STRENGTHS" \
  --include-conditional-access "$ENTRA_INCLUDE_CONDITIONAL_ACCESS" \
  --include-enterprise-applications "$ENTRA_INCLUDE_ENTERPRISE_APPS_EFFECTIVE" \
  --enterprise-app-workers "${ENTRA_ENTERPRISE_APP_WORKERS:-8}" \
  --include-app-registrations "$ENTRA_INCLUDE_APP_REGISTRATIONS_EFFECTIVE" \
  --fail-on-export-error true

if [ "$ENTRA_INCLUDE_CONDITIONAL_ACCESS" = "true" ]; then
  python3 scripts/resolve_ca_references.py --root "$root"
  python3 scripts/generate_assignment_report.py --root "$root" --output-dir "$reports"
fi
if [ "$ENTRA_INCLUDE_APP_REGISTRATIONS_EFFECTIVE" = "true" ] || [ "$ENTRA_INCLUDE_ENTERPRISE_APPS_EFFECTIVE" = "true" ]; then
  python3 scripts/generate_app_inventory_report.py --root "$root" --output-dir "$reports"
fi
python3 scripts/generate_object_inventory_reports.py --root "$root" --output-dir "$reports"

python3 scripts/validate_backup_outputs.py --workload entra --mode "$RUN_MODE" --root "$root" --reports-root "$reports" \
  --include-named-locations "$ENTRA_INCLUDE_NAMED_LOCATIONS" \
  --include-authentication-strengths "$ENTRA_INCLUDE_AUTHENTICATION_STRENGTHS" \
  --include-conditional-access "$ENTRA_INCLUDE_CONDITIONAL_ACCESS" \
  --include-enterprise-applications "$ENTRA_INCLUDE_ENTERPRISE_APPS" \
  --include-enterprise-applications-effective "$ENTRA_INCLUDE_ENTERPRISE_APPS_EFFECTIVE" \
  --include-app-registrations "$ENTRA_INCLUDE_APP_REGISTRATIONS" \
  --include-app-registrations-effective "$ENTRA_INCLUDE_APP_REGISTRATIONS_EFFECTIVE"

python3 scripts/filter_entra_enrichment_noise.py --repo-root "$PWD" --workload-root "$root" --fail-on-residual-enrichment-drift true

python3 scripts/commit_entra_drift.py \
  --repo-root "$PWD" \
  --workload-root "$root" \
  --baseline-branch "$BASELINE_BRANCH" \
  --drift-branch "$DRIFT_BRANCH_ENTRA" \
  --service-name "${USER_NAME:-ASTRAL Backup Service}" \
  --service-email "${USER_EMAIL:-astral-backup@local.invalid}" \
  --build-reason "${BUILD_REASON:-}" \
  --requested-for "${BUILD_REQUESTEDFOR:-}" \
  --backup-start "$BACKUP_START"
