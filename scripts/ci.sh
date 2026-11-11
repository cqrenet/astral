# Shared CI helpers for ASTRAL bash scripts (Azure DevOps and GitHub Actions). Source it; don't run it.

# ci_log warning|error message
ci_log() {
  local level="$1"
  shift
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    echo "::${level}::$*"
  else
    echo "##vso[task.logissue type=${level}]$*"
  fi
}

# ci_set_env NAME value  -> available to later steps of the same job
ci_set_env() {
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    printf '%s=%s\n' "$1" "$(printf '%s' "$2" | tr '\r\n' '  ')" >> "$GITHUB_ENV"
  else
    echo "##vso[task.setvariable variable=$1]$2"
  fi
}
