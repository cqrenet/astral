#!/usr/bin/env bash
# Clear the Entra export folder before a new export. In light runs, categories that are only exported
# in full runs (Enterprise Applications, App Registrations) are restored from the drift branch instead.
# Env: BACKUP_FOLDER, ENTRA_BACKUP_SUBDIR, REPORTS_SUBDIR, DRIFT_BRANCH_ENTRA,
#      ENTRA_INCLUDE_ENTERPRISE_APPS(_EFFECTIVE), ENTRA_INCLUDE_APP_REGISTRATIONS(_EFFECTIVE). Run from repo root.
# Shared by azure-pipelines.yml and .github/workflows/astral-backup.yml.
set -euo pipefail

ENTRA_REL="${BACKUP_FOLDER}/${ENTRA_BACKUP_SUBDIR}"
ENTRA_ROOT="$PWD/$ENTRA_REL"
ENTRA_REPORTS_ROOT="$PWD/${BACKUP_FOLDER}/${REPORTS_SUBDIR}/entra"
mkdir -p "$ENTRA_ROOT"

PRESERVE_DIRS=()
if [ "${ENTRA_INCLUDE_ENTERPRISE_APPS:-}" = "true" ] && [ "${ENTRA_INCLUDE_ENTERPRISE_APPS_EFFECTIVE:-}" != "true" ]; then
  PRESERVE_DIRS+=("Enterprise Applications")
fi
if [ "${ENTRA_INCLUDE_APP_REGISTRATIONS:-}" = "true" ] && [ "${ENTRA_INCLUDE_APP_REGISTRATIONS_EFFECTIVE:-}" != "true" ]; then
  PRESERVE_DIRS+=("App Registrations")
fi

if [ "${#PRESERVE_DIRS[@]}" -gt 0 ]; then
  echo "Preserving Entra baseline categories in light run: ${PRESERVE_DIRS[*]}"
  if git show-ref --verify --quiet "refs/remotes/origin/${DRIFT_BRANCH_ENTRA}"; then
    for preserve_name in "${PRESERVE_DIRS[@]}"; do
      preserve_path="$ENTRA_REL/$preserve_name"
      if git cat-file -e "origin/${DRIFT_BRANCH_ENTRA}:$preserve_path" 2>/dev/null; then
        git checkout --quiet "origin/${DRIFT_BRANCH_ENTRA}" -- "$preserve_path" || true
      fi
    done
  fi
  find "$ENTRA_ROOT" -mindepth 1 -maxdepth 1 -print0 | while IFS= read -r -d '' entry; do
    entry_name="$(basename "$entry")"
    keep=0
    for preserve_name in "${PRESERVE_DIRS[@]}"; do
      if [ "$entry_name" = "$preserve_name" ]; then
        keep=1
        break
      fi
    done
    if [ "$keep" = "0" ]; then
      rm -rf "$entry"
    fi
  done
else
  rm -rf "$ENTRA_ROOT"
fi
rm -rf "$ENTRA_REPORTS_ROOT"
mkdir -p "$ENTRA_ROOT"
