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
export ENTRA_INCLUDE_GROUPS="${ENTRA_INCLUDE_GROUPS:-true}"
export ENTRA_INCLUDE_ROLE_ASSIGNMENTS="${ENTRA_INCLUDE_ROLE_ASSIGNMENTS:-true}"
export ENTRA_INCLUDE_AUTH_METHODS_POLICY="${ENTRA_INCLUDE_AUTH_METHODS_POLICY:-true}"
export ENTRA_INCLUDE_CROSS_TENANT_ACCESS="${ENTRA_INCLUDE_CROSS_TENANT_ACCESS:-true}"
export ENTRA_INCLUDE_IDENTITY_PROTECTION="${ENTRA_INCLUDE_IDENTITY_PROTECTION:-true}"
export ENTRA_INCLUDE_PRIVILEGED_GROUPS="${ENTRA_INCLUDE_PRIVILEGED_GROUPS:-true}"
export ENTRA_INCLUDE_PIM_POLICIES="${ENTRA_INCLUDE_PIM_POLICIES:-true}"
export ENTRA_INCLUDE_SECURITY_SETTINGS="${ENTRA_INCLUDE_SECURITY_SETTINGS:-true}"
export ENTRA_WATCH_GROUPS_CSV="${ENTRA_WATCH_GROUPS_CSV:-}"
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

# Identity objects (groups, role assignments, PIM, ...) also write their own Markdown reports.
timeout 20m python3 -u scripts/export_entra_identity.py \
  --root "$root" \
  --include-groups "$ENTRA_INCLUDE_GROUPS" \
  --include-role-assignments "$ENTRA_INCLUDE_ROLE_ASSIGNMENTS" \
  --include-auth-methods-policy "$ENTRA_INCLUDE_AUTH_METHODS_POLICY" \
  --include-cross-tenant-access "$ENTRA_INCLUDE_CROSS_TENANT_ACCESS" \
  --include-identity-protection "$ENTRA_INCLUDE_IDENTITY_PROTECTION" \
  --include-privileged-groups "$ENTRA_INCLUDE_PRIVILEGED_GROUPS" \
  --include-pim-policies "$ENTRA_INCLUDE_PIM_POLICIES" \
  --include-security-settings "$ENTRA_INCLUDE_SECURITY_SETTINGS" \
  --watch-groups-csv "$ENTRA_WATCH_GROUPS_CSV" \
  --previous-snapshot-ref "origin/$DRIFT_BRANCH_ENTRA" \
  --reports-root "$reports" \
  --fail-on-export-error true

if [ "$ENTRA_INCLUDE_CONDITIONAL_ACCESS" = "true" ]; then
  python3 scripts/resolve_ca_references.py --root "$root" --groups-root "$root/Groups"
fi
# Report definitions live in reports/definitions.yml (+ definitions.local.yml); conditions read the env above.
python3 scripts/run_reports.py --workload entra --backup-dir "$root" --reports-dir "$reports" \
  --repo-root "$PWD" --mode "$RUN_MODE" --type reports

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
