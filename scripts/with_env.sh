#!/usr/bin/env bash
# Load credentials from an env file into a CHILD process only.
#
# Values are never echoed, never written to a log, and never enter the calling
# shell's environment. The only thing this script will ever print is variable
# NAMES. Use it to wrap any command that needs credentials:
#
#   scripts/with_env.sh -- python -m pytest tests/
#   scripts/with_env.sh -- python -m winch.cli
#   scripts/with_env.sh --names              # audit what is configured
#
# Why a wrapper and not `source`: shell state does not persist between tool
# invocations, and exporting into an interactive session leaves secrets sitting
# in an environment that later gets dumped by something careless. Passing them
# to one child process is the narrower blast radius.

set -euo pipefail
set +x                      # never trace; tracing would print values
PS4=''

ENV_FILE="${WINCH_ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env}"
NAMES_ONLY=0
declare -a CMD=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --names)    NAMES_ONLY=1; shift ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --)         shift; CMD=("$@"); break ;;
    *)          echo "usage: $0 [--env-file PATH] [--names] [-- COMMAND...]" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "error: env file not found: $ENV_FILE" >&2
  exit 1
fi

# A world- or group-readable secrets file is a finding, not a warning.
PERMS="$(stat -c '%a' "$ENV_FILE")"
if [[ "${PERMS: -2}" != "00" ]]; then
  echo "error: $ENV_FILE is mode $PERMS; secrets must not be group/world readable." >&2
  echo "       fix with: chmod 600 $ENV_FILE" >&2
  exit 1
fi

declare -a NAMES=()

# Parsed line by line rather than sourced: sourcing executes whatever is in the
# file, so a stray backtick in a pasted secret would run as a command.
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line#"${line%%[![:space:]]*}"}"          # ltrim
  [[ -z "$line" || "$line" == \#* ]] && continue
  line="${line#export }"
  [[ "$line" != *=* ]] && continue

  name="${line%%=*}"
  value="${line#*=}"
  name="${name%"${name##*[![:space:]]}"}"          # rtrim name
  [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

  value="${value#"${value%%[![:space:]]*}"}"       # ltrim value
  value="${value%"${value##*[![:space:]]}"}"       # rtrim value
  if [[ "$value" == \"*\" && ${#value} -ge 2 ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && ${#value} -ge 2 ]]; then
    value="${value:1:${#value}-2}"
  fi

  export "$name=$value"
  NAMES+=("$name")
  unset value
done < "$ENV_FILE"

if [[ "$NAMES_ONLY" == "1" ]]; then
  printf '%s\n' "${NAMES[@]}"                      # names only, never values
  exit 0
fi

if [[ ${#CMD[@]} -eq 0 ]]; then
  echo "error: no command given. Use -- COMMAND, or --names to audit." >&2
  exit 2
fi

exec "${CMD[@]}"
