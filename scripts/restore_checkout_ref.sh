#!/usr/bin/env bash
# Check out the baseline snapshot to restore. Only commits in the approved branch history are
# accepted, so an unreviewed branch or tag can never be restored to the tenant.
# Env: TARGET_REF_RAW (optional historical branch/tag/commit), BASELINE_BRANCH_PARAM,
#      APPROVED_BRANCH (trusted pipeline variable, not a run input), RESTORE_DATA_DIR (must exist after checkout).
# Sets RESTORE_BASELINE_REF / RESTORE_BASELINE_COMMIT. Shared by azure-pipelines-restore.yml and
# .github/workflows/astral-restore.yml. Inputs arrive via env, never interpolated into script text.
set -euo pipefail
source "$(dirname "$0")/ci.sh"

TARGET_REF="$(printf '%s' "${TARGET_REF_RAW:-}" | xargs)"
TARGET_REF_LOWER="$(echo "$TARGET_REF" | tr '[:upper:]' '[:lower:]')"
if echo "$TARGET_REF" | grep -Eq '^\$\([^)]+\)$'; then
  TARGET_REF=""   # unresolved Azure DevOps macro
elif [ "$TARGET_REF_LOWER" = "none" ] || [ "$TARGET_REF_LOWER" = "null" ] || [ "$TARGET_REF_LOWER" = "n/a" ] || [ "$TARGET_REF_LOWER" = "-" ] || [ "$TARGET_REF_LOWER" = "_none_" ]; then
  TARGET_REF=""
fi
if [ -z "$TARGET_REF" ]; then
  TARGET_REF="${BASELINE_BRANCH_PARAM:-}"
fi
: "${APPROVED_BRANCH:?APPROVED_BRANCH is required}"

# Plain ref names/SHAs only: blocks git option injection (e.g. --upload-pack=...).
for ref in "$TARGET_REF" "$APPROVED_BRANCH"; do
  if ! [[ "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
    ci_log error "Invalid git ref '$ref'."
    exit 1
  fi
done

# Full history is needed for the ancestry check (Azure DevOps checkouts are shallow by default).
# The ancestry check is a security boundary. Never continue with a stale or
# missing approved-branch ref when refresh failed.
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  if ! git fetch --quiet --unshallow origin; then
    ci_log error "Unable to fetch the full repository history; refusing to restore."
    exit 1
  fi
fi
if ! git fetch --quiet --tags origin; then
  ci_log error "Unable to refresh repository tags; refusing to restore."
  exit 1
fi
if ! git fetch --quiet origin "$APPROVED_BRANCH"; then
  ci_log error "Unable to refresh approved branch '$APPROVED_BRANCH'; refusing to restore."
  exit 1
fi

RESOLVED_REF=""
if git rev-parse --verify --quiet "origin/$TARGET_REF^{commit}" >/dev/null; then
  RESOLVED_REF="origin/$TARGET_REF"
elif git rev-parse --verify --quiet "$TARGET_REF^{commit}" >/dev/null; then
  RESOLVED_REF="$TARGET_REF"
elif git fetch --quiet origin "$TARGET_REF" >/dev/null 2>&1; then
  RESOLVED_REF="FETCH_HEAD"
fi

if [ -z "$RESOLVED_REF" ]; then
  ci_log error "Unable to resolve baseline ref '$TARGET_REF'."
  echo "Checked local ref, origin/<ref>, and direct fetch origin <ref>."
  exit 1
fi

RESOLVED_COMMIT="$(git rev-parse "$RESOLVED_REF^{commit}")"
if ! git merge-base --is-ancestor "$RESOLVED_COMMIT" "origin/$APPROVED_BRANCH"; then
  ci_log error "Ref '$TARGET_REF' ($RESOLVED_COMMIT) is not in approved branch '$APPROVED_BRANCH' history; refusing to restore."
  exit 1
fi

git checkout --quiet --force --detach "$RESOLVED_COMMIT"
echo "Restore baseline snapshot selected: requested=$TARGET_REF resolved=$RESOLVED_REF commit=$RESOLVED_COMMIT"
ci_set_env RESTORE_BASELINE_REF "$TARGET_REF"
ci_set_env RESTORE_BASELINE_COMMIT "$RESOLVED_COMMIT"
test -d "${RESTORE_DATA_DIR:-.}"
