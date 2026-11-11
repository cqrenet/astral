#!/usr/bin/env bash
# Rolling-PR review for one workload on GitHub/Gitea Actions: create/refresh the rolling PR (optional),
# apply reviewer /reject decisions, refresh the summary, queue post-merge remediation.
# The PR scripts speak Azure DevOps REST; scripts/forge_compat.py translates it (SYSTEM_*/BUILD_* are placeholders).
# Env: WORKLOAD (intune|entra), FORGE_TOKEN, ENSURE_PR (true|false), feature toggles (safe defaults below).
set -euo pipefail
source "$(dirname "$0")/ci.sh"
: "${WORKLOAD:?WORKLOAD is required}" "${FORGE_TOKEN:?FORGE_TOKEN is required}" "${GITHUB_SERVER_URL:?}"

export SYSTEM_ACCESSTOKEN="$FORGE_TOKEN" SYSTEM_COLLECTIONURI="$GITHUB_SERVER_URL" SYSTEM_TEAMPROJECT=forge BUILD_REPOSITORY_ID=forge
export BUILD_BUILDNUMBER="${GITHUB_RUN_NUMBER:-0}" BUILD_BUILDID="${GITHUB_RUN_ID:-0}"
unset GRAPH_TOKEN  # review never needs tenant access

export BASELINE_BRANCH="${BASELINE_BRANCH:-main}" BACKUP_FOLDER="${BACKUP_FOLDER:-tenant-state}" REPORTS_SUBDIR="${REPORTS_SUBDIR:-reports}"
export INTUNE_BACKUP_SUBDIR="${INTUNE_BACKUP_SUBDIR:-intune}" ENTRA_BACKUP_SUBDIR="${ENTRA_BACKUP_SUBDIR:-entra}"
case "$WORKLOAD" in
  intune) DRIFT_BRANCH="${DRIFT_BRANCH_INTUNE:-drift/intune}"; PR_TITLE="Intune drift review (rolling)" ;;
  entra)  DRIFT_BRANCH="${DRIFT_BRANCH_ENTRA:-drift/entra}";  PR_TITLE="Entra drift review (rolling)" ;;
  *) ci_log error "Unknown WORKLOAD '$WORKLOAD'"; exit 1 ;;
esac

# Defaults are the conservative posture: no tenant writes unless explicitly enabled.
export ENABLE_PR_REVIEW_SUMMARY="${ENABLE_PR_REVIEW_SUMMARY:-true}" ENABLE_PR_REVIEWER_DECISIONS="${ENABLE_PR_REVIEWER_DECISIONS:-true}"
export ENABLE_PR_AI_SUMMARY="${ENABLE_PR_AI_SUMMARY:-false}" AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION:-2024-12-01-preview}"
export REQUIRE_CHANGE_TICKETS="${REQUIRE_CHANGE_TICKETS:-false}" CHANGE_TICKET_REGEX="${CHANGE_TICKET_REGEX:-[A-Z][A-Z0-9]+-[0-9]+}"
export DEBUG_CHANGE_TICKET_THREADS="${DEBUG_CHANGE_TICKET_THREADS:-false}"
export ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS="${ROLLING_PR_DELAY_REVIEWER_NOTIFICATIONS:-true}" ROLLING_PR_MERGE_STRATEGY="${ROLLING_PR_MERGE_STRATEGY:-rebase}"
export AUTO_REMEDIATE_ON_PR_REJECTION="${AUTO_REMEDIATE_ON_PR_REJECTION:-false}" AUTO_REMEDIATE_AFTER_MERGE="${AUTO_REMEDIATE_AFTER_MERGE:-false}"
export AUTO_REMEDIATE_AFTER_MERGE_LOOKBACK_HOURS="${AUTO_REMEDIATE_AFTER_MERGE_LOOKBACK_HOURS:-168}"
export AUTO_REMEDIATE_RESTORE_PIPELINE_ID="${AUTO_REMEDIATE_RESTORE_WORKFLOW:-astral-restore.yml}"
export AUTO_REMEDIATE_DRY_RUN="${AUTO_REMEDIATE_DRY_RUN:-true}" AUTO_REMEDIATE_UPDATE_ASSIGNMENTS="${AUTO_REMEDIATE_UPDATE_ASSIGNMENTS:-true}"
export AUTO_REMEDIATE_REMOVE_OBJECTS="${AUTO_REMEDIATE_REMOVE_OBJECTS:-false}" AUTO_REMEDIATE_MAX_WORKERS="${AUTO_REMEDIATE_MAX_WORKERS:-10}"

common=(--drift-branch "$DRIFT_BRANCH" --baseline-branch "$BASELINE_BRANCH")

if [ "${ENSURE_PR:-false}" = "true" ]; then
  AUTO_REMEDIATE_INCLUDE_ENTRA_UPDATE=false python3 scripts/ensure_rolling_pr.py \
    --repo-root "$PWD" --workload "$WORKLOAD" "${common[@]}" --pr-title "$PR_TITLE"
fi
if [ "$ENABLE_PR_AI_SUMMARY" = "true" ]; then
  python3 scripts/diagnostics/precheck_azure_openai_availability.py || ci_log warning "Azure OpenAI precheck failed."
fi
if [ "$ENABLE_PR_REVIEWER_DECISIONS" = "true" ]; then
  python3 scripts/apply_reviewer_rejections.py --repo-root "$PWD" --workload "$WORKLOAD" "${common[@]}"
fi
if [ "$ENABLE_PR_REVIEW_SUMMARY" = "true" ]; then
  python3 scripts/update_pr_review_summary.py --repo-root "$PWD" --workload "$WORKLOAD" \
    --backup-folder "$BACKUP_FOLDER" --reports-subdir "$REPORTS_SUBDIR" "${common[@]}"
fi
if [ "$AUTO_REMEDIATE_AFTER_MERGE" = "true" ]; then
  AUTO_REMEDIATE_INCLUDE_ENTRA_UPDATE=$([ "$WORKLOAD" = entra ] && echo true || echo false) \
    python3 scripts/queue_post_merge_restore.py --workload "$WORKLOAD" "${common[@]}"
fi
