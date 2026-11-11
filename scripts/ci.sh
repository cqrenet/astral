# shellcheck shell=bash
# Shared CI helpers for ASTRAL bash scripts (Azure DevOps, GitHub Actions, Gitea Actions). Source it; don't run it.

# True on GitHub/Gitea Actions. Mirrors common.scm_platform(): ASTRAL_SCM overrides detection.
ci_is_actions() {
  case "${ASTRAL_SCM:-}" in
    github | gitea) return 0 ;;
    ado) return 1 ;;
  esac
  [ "${GITHUB_ACTIONS:-}" = "true" ]
}

# Values end up in line-oriented CI commands; a newline would inject extra commands.
_ci_one_line() {
  printf '%s' "$*" | tr '\r\n' '  '
}

# ci_log warning|error message
ci_log() {
  local level="$1" message
  shift
  message="$(_ci_one_line "$*")"
  if ci_is_actions; then
    echo "::${level}::${message}"
  else
    echo "##vso[task.logissue type=${level}]${message}"
  fi
}

# ci_set_env NAME value  -> available to later steps of the same job
ci_set_env() {
  local value
  value="$(_ci_one_line "$2")"
  if ci_is_actions; then
    printf '%s=%s\n' "$1" "$value" >> "$GITHUB_ENV"
  else
    echo "##vso[task.setvariable variable=$1]${value}"
  fi
}

# ci_is_full_run -> success when FORCE_FULL_RUN=true, or the current hour in BACKUP_TIMEZONE equals
# FULL_RUN_HOUR (default 02). Hours compare as numbers, so "2" and "02" both work; a non-numeric
# FULL_RUN_HOUR never matches.
ci_is_full_run() {
  [ "${FORCE_FULL_RUN:-false}" = "true" ] && return 0
  local now target
  now="$(TZ="${BACKUP_TIMEZONE:-UTC}" date +%H)"
  target="${FULL_RUN_HOUR:-02}"
  [[ "$target" =~ ^[0-9]{1,2}$ ]] || return 1
  [ "$((10#$now))" -eq "$((10#$target))" ]
}
