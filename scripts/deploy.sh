#!/usr/bin/env bash
# Deploy to Cloud Run. Non-secret config is read from .env and passed as plain
# env vars; secrets are referenced from Secret Manager and never touched here.
#
#   scripts/deploy.sh            # build and deploy
#   scripts/deploy.sh --dry-run  # print the config that would be sent (names only)
#
# Run scripts/sync_secrets.sh first if any secret value changed.

set -euo pipefail
set +x
PS4=''

PROJECT="${GCP_PROJECT:-my-winch-project}"
SERVICE="${CLOUD_RUN_SERVICE:-winch}"
REGION="${CLOUD_RUN_REGION:-europe-west2}"
SQL_INSTANCE="${CLOUD_SQL_INSTANCE:-my-winch-project:europe-west2:winch-db}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${WINCH_ENV_FILE:-$ROOT/.env}"

# Non-secret configuration. Anything not on this list stays local — a stray
# credential in .env can never leak into a Cloud Run env var.
PLAIN_NAMES=(
  AZURE_OPENAI_ENDPOINT
  AZURE_OPENAI_API_VERSION
  LLM_MODEL
  META_PHONE_NUMBER_ID
  META_GRAPH_VERSION
  CONTRACTOR_WA_ID
  CONTRACTOR_FIRST_NAME
  CONTRACTOR_BUSINESS_NAME
  CONTRACTOR_TIMEZONE
  PRIVACY_CONTACT_EMAIL
  PRIVACY_OPERATOR_NAME
)

SECRET_MAP="AZURE_OPENAI_API_KEY=AZURE_OPENAI_API_KEY:latest,\
META_ACCESS_TOKEN=META_ACCESS_TOKEN:latest,\
META_APP_SECRET=META_APP_SECRET:latest,\
META_VERIFY_TOKEN=META_VERIFY_TOKEN:latest,\
TICK_SECRET=TICK_SECRET:latest,\
DATABASE_URL=DATABASE_URL:latest"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

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
    printf '%s' "$value"; return 0
  done < "$ENV_FILE"
  return 1
}

ENV_ARGS=""
for name in "${PLAIN_NAMES[@]}"; do
  if value="$(read_value "$name" 2>/dev/null)" && [[ -n "$value" ]]; then
    ENV_ARGS+="${ENV_ARGS:+,}${name}=${value}"
    echo "  config      $name"
  else
    echo "  unset       $name"
  fi
done

if [[ "$DRY_RUN" == "1" ]]; then
  echo "secrets: ${SECRET_MAP//,/$'\n'         }"
  exit 0
fi

gcloud run deploy "$SERVICE" \
  --project="$PROJECT" --region="$REGION" --source="$ROOT" \
  --allow-unauthenticated \
  --add-cloudsql-instances="$SQL_INSTANCE" \
  --set-env-vars="$ENV_ARGS" \
  --set-secrets="$SECRET_MAP" \
  --memory=1Gi --cpu=1 --timeout=300 --max-instances=3 \
  --quiet

echo
echo "service URL:"
gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --format='value(status.url)'
