#!/usr/bin/env bash
# Push secret values from .env into Google Secret Manager, then roll the Cloud
# Run service so it picks them up.
#
# Values are piped straight from the env file into gcloud. They are never
# echoed, never written to a temp file, and never enter this shell's output.
# Only NAMES are printed.
#
#   scripts/sync_secrets.sh                 # sync secrets, no restart
#   scripts/sync_secrets.sh --restart       # sync, then roll a new revision
#   scripts/sync_secrets.sh --dry-run       # show what would sync
#
# Cloud Run resolves SECRET:latest when a container starts, so a running
# instance keeps the old value until it is replaced. --restart forces a new
# revision, which is the only reliable way to make a rotated secret take effect.

set -euo pipefail
set +x
PS4=''

PROJECT="${GCP_PROJECT:-my-winch-project}"
SERVICE="${CLOUD_RUN_SERVICE:-winch}"
REGION="${CLOUD_RUN_REGION:-europe-west2}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${WINCH_ENV_FILE:-$ROOT/.env}"

# Only these are treated as secrets. Everything else in .env stays local, so a
# stray key in the file is never silently shipped to production.
SECRET_NAMES=(
  AZURE_OPENAI_API_KEY
  META_ACCESS_TOKEN
  META_APP_SECRET
  META_VERIFY_TOKEN
  TICK_SECRET
)

RESTART=0
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart) RESTART=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "usage: $0 [--restart] [--dry-run]" >&2; exit 2 ;;
  esac
done

[[ -f "$ENV_FILE" ]] || { echo "error: env file not found: $ENV_FILE" >&2; exit 1; }
PERMS="$(stat -c '%a' "$ENV_FILE")"
[[ "${PERMS: -2}" == "00" ]] || {
  echo "error: $ENV_FILE is mode $PERMS; run: chmod 600 $ENV_FILE" >&2; exit 1; }

# Read one value without printing it. Returns empty if absent.
read_value() {
  local want="$1" line name value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    line="${line#export }"
    [[ "$line" != *=* ]] && continue
    name="${line%%=*}"; name="${name%"${name##*[![:space:]]}"}"
    [[ "$name" == "$want" ]] || continue
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "$value" == \"*\" && ${#value} -ge 2 ]]; then value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && ${#value} -ge 2 ]]; then value="${value:1:${#value}-2}"; fi
    printf '%s' "$value"
    return 0
  done < "$ENV_FILE"
  return 1
}

synced=() missing=()
for name in "${SECRET_NAMES[@]}"; do
  if ! read_value "$name" >/dev/null 2>&1; then
    missing+=("$name"); continue
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  would sync  $name"; synced+=("$name"); continue
  fi

  if ! gcloud secrets describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud secrets create "$name" --project="$PROJECT" \
      --replication-policy=automatic >/dev/null
    echo "  created     $name"
  fi

  # The value goes down a pipe. It is never assigned to a shell variable that
  # could show up in `set -x`, an error trace, or a core dump.
  if read_value "$name" | gcloud secrets versions add "$name" \
        --project="$PROJECT" --data-file=- >/dev/null 2>&1; then
    echo "  synced      $name"
    synced+=("$name")
  else
    echo "  FAILED      $name" >&2
  fi
done

for name in "${missing[@]}"; do
  echo "  missing     $name  (not in $ENV_FILE - skipped)"
done

if [[ ${#synced[@]} -eq 0 ]]; then
  echo "nothing synced"
  exit 0
fi

if [[ "$RESTART" == "1" && "$DRY_RUN" == "0" ]]; then
  if gcloud run services describe "$SERVICE" --project="$PROJECT" \
       --region="$REGION" >/dev/null 2>&1; then
    echo "rolling a new revision of $SERVICE so the new versions are picked up..."
    gcloud run services update "$SERVICE" --project="$PROJECT" --region="$REGION" \
      --update-env-vars="SECRETS_SYNCED_AT=$(date -u +%Y%m%dT%H%M%SZ)" --quiet \
      >/dev/null
    echo "  rolled      $SERVICE"
  else
    echo "  $SERVICE not deployed yet in $REGION - nothing to restart"
  fi
fi
