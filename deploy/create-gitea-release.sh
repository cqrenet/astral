#!/usr/bin/env bash
# Create a proper Gitea release from an existing Git tag.
#
# Usage:
#   GITEA_TOKEN=xxx ./deploy/create-gitea-release.sh <tag-name> [gitea-url]
#
# Example:
#   GITEA_TOKEN=ghp_xxx ./deploy/create-gitea-release.sh v1.3.0 https://git.cqre.net

set -euo pipefail

TAG_NAME="${1:-}"
GITEA_URL="${2:-https://git.cqre.net}"
REPO_OWNER="cqrenet"
REPO_NAME="astral"

if [ -z "$TAG_NAME" ]; then
  echo "Usage: GITEA_TOKEN=xxx $0 <tag-name> [gitea-url]"
  exit 1
fi

if [ -z "${GITEA_TOKEN:-}" ]; then
  echo "Error: GITEA_TOKEN environment variable is required."
  echo "Create one at: $GITEA_URL/user/settings/applications"
  exit 1
fi

DEV_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Get the tag annotation message
TAG_MESSAGE="$(git -C "$DEV_ROOT" tag -n10 "$TAG_NAME" | tail -n +2 | sed 's/^    //')"

# Get commits since the previous tag (or all if first tag)
PREV_TAG="$(git -C "$DEV_ROOT" tag --sort=-version:refname | grep -v "$TAG_NAME" | head -1 || true)"
if [ -n "$PREV_TAG" ]; then
  COMMIT_RANGE="${PREV_TAG}..${TAG_NAME}"
else
  COMMIT_RANGE="$TAG_NAME"
fi

CHANGELOG="$(git -C "$DEV_ROOT" log --pretty=format:'- %s' "$COMMIT_RANGE" | grep -v '^- Merge' | grep -v '^- Sync from dev')"

BODY=$(cat <<EOF
## What's Changed

${TAG_MESSAGE}

### Commits
${CHANGELOG}

---
*Full changelog: [$PREV_TAG...$TAG_NAME]($GITEA_URL/$REPO_OWNER/$REPO_NAME/compare/$PREV_TAG...$TAG_NAME)*
EOF
)

# Create release via Gitea API
RESPONSE=$(curl -s -X POST "${GITEA_URL}/api/v1/repos/${REPO_OWNER}/${REPO_NAME}/releases" \
  -H "accept: application/json" \
  -H "Content-Type: application/json" \
  -H "Authorization: token ${GITEA_TOKEN}" \
  -d "$(jq -n \
    --arg tag "$TAG_NAME" \
    --arg name "ASTRAL $TAG_NAME" \
    --arg body "$BODY" \
    '{tag_name: $tag, name: $name, body: $body, draft: false, prerelease: false}')")

# Check response
RELEASE_ID=$(echo "$RESPONSE" | jq -r '.id // empty')
if [ -z "$RELEASE_ID" ] || [ "$RELEASE_ID" = "null" ]; then
  echo "Error creating release:"
  echo "$RESPONSE" | jq .
  exit 1
fi

HTML_URL=$(echo "$RESPONSE" | jq -r '.html_url')
echo "✅ Release created: $HTML_URL"
