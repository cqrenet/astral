#!/usr/bin/env bash
# Create a GitHub release from an existing Git tag using the gh CLI.
#
# Usage:
#   ./deploy/create-github-release.sh <tag-name> [previous-tag]
#
# Example:
#   ./deploy/create-github-release.sh v1.2.0
#
# Prerequisites:
#   gh CLI installed and authenticated (gh auth login)

set -euo pipefail

TAG_NAME="${1:-}"
REPO="cqrenet/astral"

if [ -z "$TAG_NAME" ]; then
  echo "Usage: $0 <tag-name>"
  exit 1
fi

if ! command -v gh &>/dev/null; then
  echo "Error: gh CLI not found. Install from https://cli.github.com"
  exit 1
fi

if ! gh auth status &>/dev/null 2>&1; then
  echo "Error: gh CLI not authenticated. Run: gh auth login"
  exit 1
fi

DEV_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Get the tag annotation message
TAG_MESSAGE="$(git -C "$DEV_ROOT" tag -n80 "$TAG_NAME" 2>/dev/null | tail -n +2 | sed 's/^    //' || true)"

# Get commits since the previous tag
PREV_TAG="${2:-$(git -C "$DEV_ROOT" tag --sort=-version:refname | grep -v "^${TAG_NAME}$" | head -1 || true)}"
if [ -n "$PREV_TAG" ]; then
  COMMIT_RANGE="${PREV_TAG}..${TAG_NAME}"
else
  COMMIT_RANGE="$TAG_NAME"
fi

CHANGELOG="$(git -C "$DEV_ROOT" log --pretty=format:'- %s' "$COMMIT_RANGE" | grep -v '^- Merge' | grep -v '^- Sync from dev' || true)"

if [ -n "$PREV_TAG" ]; then
  COMPARE_LINE="> Full diff: https://github.com/${REPO}/compare/${PREV_TAG}...${TAG_NAME}"
else
  COMPARE_LINE=""
fi

NOTES=$(printf '%s\n' \
  "## What's Changed" \
  "" \
  "${TAG_MESSAGE}" \
  "" \
  "### Commits" \
  "${CHANGELOG}" \
  "" \
  "${COMPARE_LINE}" \
  "" \
  "## Updating an existing deployment" \
  "" \
  "Queue the \`deploy/update-from-upstream.yml\` pipeline in your ADO project and set **Upstream ref** to \`${TAG_NAME}\`. Run with **Dry run** first to preview changes." \
  "" \
  "If you have not imported the update pipeline yet, see [Staying up to date](https://github.com/${REPO}/blob/main/deploy/onboarding-runbook.md#staying-up-to-date) in the onboarding runbook.")

gh release create "$TAG_NAME" \
  --repo "$REPO" \
  --title "ASTRAL $TAG_NAME" \
  --notes "$NOTES"
