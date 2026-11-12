#!/usr/bin/env bash
# Syncs a sanitized, source-only snapshot from the dev repo to the public template repo.
#
# Usage:
#   ./deploy/sync-to-public.sh <public-repo-url> [tag-name]
#
# Example:
#   ./deploy/sync-to-public.sh https://github.com/cqrenet/astral v1.2.0

set -euo pipefail

DEV_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PUBLIC_URL="${1:-}"
TAG_NAME="${2:-}"
BLACKLIST=(
  "tenant-state"
  "prod-as-built.md"
  ".astral-upstream-ref"
  "docs/assets/astral-logo.svg"
  "node_modules"
  "__pycache__"
  "*.py[cod]"
  "*\$py.class"
  ".DS_Store"
  # Dev-only tooling — not published
  "PRICING.md"
  "SUPPORT.md"
  "docs/share"
  "deploy/create-gitea-release.sh"
  "deploy/create-github-release.sh"
  "deploy/publish-public.yml"
  # Local/runtime artifacts
  "infra/change-probe/.python_packages"
  "infra/change-probe/local.settings.json"
  "infra/change-probe/scripts"
)

if [ -z "$PUBLIC_URL" ]; then
  echo "Usage: $0 <public-repo-url> [tag-name]"
  exit 1
fi

DEV_SHA="$(git -C "$DEV_ROOT" rev-parse --short HEAD)"
DEV_BRANCH="$(git -C "$DEV_ROOT" rev-parse --abbrev-ref HEAD)"

echo "Dev repo:  $DEV_ROOT"
echo "Dev SHA:   $DEV_SHA ($DEV_BRANCH)"
echo "Public:    $PUBLIC_URL"

TMP_DIR="$(mktemp -d)"
# trap removed for release

PUBLIC_CLONE="$TMP_DIR/public"

# Use gh CLI for authentication if available, otherwise fall back to GIT_USERNAME/GIT_PASSWORD
if command -v gh &>/dev/null && gh auth status &>/dev/null 2>&1; then
  export GIT_ASKPASS=""
  GH_TOKEN="$(gh auth token)"
  export GIT_CONFIG_COUNT=1
  export GIT_CONFIG_KEY_0="credential.helper"
  export GIT_CONFIG_VALUE_0="!gh auth git-credential"
  echo "Using gh CLI authentication."
fi

git clone "$PUBLIC_URL" "$PUBLIC_CLONE" 2>/dev/null || {
  echo "Public repo appears empty. Initializing clone."
  git init "$PUBLIC_CLONE"
  cd "$PUBLIC_CLONE"
  git remote add origin "$PUBLIC_URL"
}

cd "$PUBLIC_CLONE"

# Ensure we have the latest public state
git fetch origin main 2>/dev/null || true

# Remove all tracked files (keep .git)
git ls-files -z | xargs -0 rm -f 2>/dev/null || true
find . -mindepth 1 -not -path './.git/*' -not -name '.git' -delete 2>/dev/null || true

# Copy dev repo contents, excluding blacklist
cd "$DEV_ROOT"
rsync -a \
  --exclude='.git' \
  --exclude='tenant-state' \
  --exclude='prod-as-built.md' \
  --exclude='.astral-upstream-ref' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.DS_Store' \
  --exclude='PRICING.md' \
  --exclude='SUPPORT.md' \
  --exclude='docs/share' \
  --exclude='deploy/create-gitea-release.sh' \
  --exclude='deploy/create-github-release.sh' \
  --exclude='deploy/publish-public.yml' \
  --exclude='deploy/sync-to-public.sh' \
  --exclude='infra/change-probe/.python_packages' \
  --exclude='infra/change-probe/local.settings.json' \
  --exclude='infra/change-probe/scripts' \
  "$DEV_ROOT/" "$PUBLIC_CLONE/"

cd "$PUBLIC_CLONE"

# Re-create empty tenant-state structure
mkdir -p tenant-state/intune tenant-state/entra tenant-state/reports/intune tenant-state/reports/entra
touch tenant-state/intune/.gitkeep tenant-state/entra/.gitkeep tenant-state/reports/intune/.gitkeep tenant-state/reports/entra/.gitkeep
cat > tenant-state/README.md <<'EOF'
# tenant-state

This directory is populated automatically by the ASTRAL backup pipeline.
Do not place manual files here; they will be overwritten on the next export.
EOF

git add -A

if git diff --cached --quiet; then
  echo "No changes to publish."
  exit 0
fi

# Try to find the previous dev SHA recorded in the public repo's latest commit.
PREV_SHA=""
if git rev-parse --verify origin/main >/dev/null 2>&1; then
  PREV_MSG="$(git log -1 --pretty=%B origin/main 2>/dev/null || true)"
  PREV_SHA="$(printf '%s\n' "$PREV_MSG" | grep -oE 'Sync from dev @[[:space:]]+[a-f0-9]+' | awk '{print $NF}' | head -c7)"
  if [ -z "$PREV_SHA" ]; then
    PREV_SHA="$(printf '%s\n' "$PREV_MSG" | grep -oE 'Source:.*\([a-f0-9]+\)' | grep -oE '\([a-f0-9]+\)' | tr -d '()' | head -c7)"
  fi
fi

COMMIT_BODY="Source: ${DEV_BRANCH} (${DEV_SHA})"

if [ -n "$PREV_SHA" ]; then
  COMMIT_BODY="${COMMIT_BODY}
Previous-sync: ${PREV_SHA}"

  DEV_COMMITS="$(git -C "$DEV_ROOT" log --no-merges --pretty='- %s' "${PREV_SHA}..${DEV_SHA}" 2>/dev/null | head -n 20 || true)"
  if [ -n "$DEV_COMMITS" ]; then
    COMMIT_BODY="${COMMIT_BODY}

Changes since previous sync:
${DEV_COMMITS}"
  fi
fi

DIFF_STAT="$(git diff --cached --stat | sed 's/^/  /' || true)"
if [ -n "$DIFF_STAT" ]; then
  COMMIT_BODY="${COMMIT_BODY}

Published changes:
${DIFF_STAT}"
fi

COMMIT_BODY="${COMMIT_BODY}

Excluded: live tenant exports, generated artifacts, and dev-only tooling."

git commit -m "Sync from dev @ ${DEV_SHA}

${COMMIT_BODY}"

if [ -n "$TAG_NAME" ]; then
  git tag -a "$TAG_NAME" -m "Release $TAG_NAME"
fi

echo ""
echo "Ready to push. Review the commit above, then run:"
echo "  cd $PUBLIC_CLONE && git push origin main"
if [ -n "$TAG_NAME" ]; then
  echo "  git push origin $TAG_NAME"
fi
echo ""
echo "Or push now with:"
echo "  (cd $PUBLIC_CLONE && git push origin main${TAG_NAME:+ && git push origin $TAG_NAME})"
