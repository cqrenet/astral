#!/usr/bin/env bash
# Run IntuneCD backup and fail on unexpected Graph errors.
# Env: INTUNE_ROOT, GRAPH_TOKEN, ENABLE_ENTRA_CONDITIONAL_ACCESS, EXCLUDE_SCRIPT_BACKUP, INTUNE_EXCLUDE_CSV.
# Sets BACKUP_START for later steps. Shared by azure-pipelines.yml and .github/workflows/astral-backup.yml.
set -euo pipefail
source "$(dirname "$0")/ci.sh"

: "${INTUNE_ROOT:?INTUNE_ROOT is required}"
: "${GRAPH_TOKEN:?GRAPH_TOKEN is required}"
mkdir -p "$INTUNE_ROOT"

BACKUP_START="${BACKUP_START:-$(date -u +%Y.%m.%d:%H.%M.%S)}"
ci_set_env BACKUP_START "$BACKUP_START"

backup_log="$(mktemp)"
trap 'rm -f "$backup_log"' EXIT

EXCLUDE_ARGS=(
  CompliancePartnerHeartbeat
  ManagedGooglePlay
  VPPusedLicenseCount
)
if [ "${ENABLE_ENTRA_CONDITIONAL_ACCESS:-}" = "true" ]; then
  EXCLUDE_ARGS+=(ConditionalAccess)
fi
if [ "${EXCLUDE_SCRIPT_BACKUP:-}" = "true" ]; then
  EXCLUDE_ARGS+=(
    ComplianceScripts
    CustomAttributes
    ProactiveRemediation
    PowershellScripts
    ShellScripts
  )
fi
if [ -n "${INTUNE_EXCLUDE_CSV:-}" ]; then
  IFS=',' read -r -a raw_items <<< "$INTUNE_EXCLUDE_CSV"
  for item in "${raw_items[@]}"; do
    trimmed="$(echo "$item" | xargs)"
    if [ -n "$trimmed" ]; then
      EXCLUDE_ARGS+=("$trimmed")
    fi
  done
fi

set +e
# ponytail: IntuneCD only takes the token as an argument; use single-job (ephemeral) runners.
IntuneCD-startbackup \
  --token "$GRAPH_TOKEN" \
  --mode=1 \
  --output=json \
  --path="$INTUNE_ROOT" \
  --exclude "${EXCLUDE_ARGS[@]}" \
  --append-id \
  --ignore-omasettings \
  --enrich-documentation \
  2>&1 | tee "$backup_log"
intunecd_exit="${PIPESTATUS[0]}"
set -e

handled_403=0
has_auth_403=0
if grep -Eiq "Application is not authorized to perform this operation|one of the following scopes|Request failed with status 403" "$backup_log"; then
  has_auth_403=1
  if [ "${EXCLUDE_SCRIPT_BACKUP:-}" = "true" ] && grep -Eiq "DeviceManagementScripts\\.Read\\.All|DeviceManagementScripts\\.ReadWrite\\.All" "$backup_log"; then
    ci_log warning "Ignoring script-related 403 because EXCLUDE_SCRIPT_BACKUP=true."
    handled_403=1
  fi
  if [ "${ENABLE_ENTRA_CONDITIONAL_ACCESS:-}" = "true" ] && grep -Eiq "Policy\\.Read\\.ConditionalAccess|conditionalAccess/policies" "$backup_log"; then
    ci_log warning "Ignoring Conditional Access-related 403 in Intune workload because ENABLE_ENTRA_CONDITIONAL_ACCESS=true."
    handled_403=1
  fi
  if [ "$handled_403" -ne 1 ]; then
    ci_log error "Intune backup has Microsoft Graph authorization failures (HTTP 403)."
    ci_log error "Grant required Graph application permissions and admin consent."
    exit 1
  fi
fi

non_403_statuses="$(grep -Eo "status[[:space:]]+[0-9]{3}" "$backup_log" | grep -Eo "[0-9]{3}" | sort -u | grep -Ev "^403$" || true)"
if [ -n "$non_403_statuses" ]; then
  ci_log error "Intune backup log contains non-403 HTTP failures: $non_403_statuses"
  exit 1
fi

if [ "$intunecd_exit" -ne 0 ]; then
  if [ "$has_auth_403" -eq 1 ] && [ "$handled_403" -eq 1 ]; then
    ci_log warning "IntuneCD exited non-zero, but only allowed 403 scopes were detected for this mode. Continuing."
  else
    ci_log error "IntuneCD backup command failed with exit code $intunecd_exit."
    exit "$intunecd_exit"
  fi
fi
