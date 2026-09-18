# Deploying to Cloud Run

## Provisioned state

| Thing | Value |
| GCP project | `my-winch-project` (separate from any other project) |
| Organisation | `your-gcp-org` |
| Region | `europe-west2` (London) — keeps customer data in-region for UK GDPR |
| Cloud SQL | `winch-db`, POSTGRES_16, `db-f1-micro`, 10GB, database `winch` |
| Secret | `DATABASE_URL` in Secret Manager; the password was generated and stored without ever being printed |

Steps 1 and 2 below are **already done**. They are kept for rebuilding from
scratch.

## 1. Project and services (done)

```bash
gcloud projects create my-winch-project --name="Quote Recovery Agent" \
    --organization=<ORG_ID>
gcloud billing projects link my-winch-project --billing-account=<BILLING_ID>
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
    secretmanager.googleapis.com cloudscheduler.googleapis.com \
    artifactregistry.googleapis.com cloudbuild.googleapis.com \
    --project=my-winch-project
```

## 2. Database (done)

Smallest tier is ample: one contractor, tens of quotes.

```bash
PW="$(openssl rand -base64 24 | tr -d '\n/+=' | head -c 28)"   # never echoed
gcloud sql instances create winch-db --project=my-winch-project \
    --database-version=POSTGRES_16 --tier=db-f1-micro --region=europe-west2 \
    --storage-size=10GB --storage-auto-increase --no-backup
gcloud sql databases create winch --instance=winch-db --project=my-winch-project
gcloud sql users set-password postgres --instance=winch-db \
    --project=my-winch-project --password="$PW"
CONN=$(gcloud sql instances describe winch-db --project=my-winch-project \
    --format='value(connectionName)')
printf 'postgresql://postgres:%s@/winch?host=/cloudsql/%s' "$PW" "$CONN" \
  | gcloud secrets create DATABASE_URL --project=my-winch-project \
    --data-file=- --replication-policy=automatic
unset PW
```

`--no-backup` is deliberate for a pilot: the only durable data is the event log,
which is reproducible from WhatsApp history if it is ever lost. Turn backups on
before anything you would be upset to lose.

## 3. Secrets

Never passed as plain env vars on the service — they end up in deploy logs and
in `gcloud run services describe` output.

```bash
# DATABASE_URL already exists. The rest wait on the Meta app.
for name in AZURE_OPENAI_API_KEY META_ACCESS_TOKEN META_APP_SECRET \
            META_VERIFY_TOKEN; do
  gcloud secrets create $name --project=my-winch-project \
      --replication-policy=automatic
done
# then add versions interactively - do not echo values into shell history:
gcloud secrets versions add AZURE_OPENAI_API_KEY --data-file=-
```

## 3b. IAM on a brand-new project (easy to miss)

A fresh project grants its default compute service account nothing, so the first
`gcloud run deploy --source` fails with a confusing storage 403 about the build
source bucket. Grant these once:

```bash
SA="<PROJECT_NUMBER>-compute@developer.gserviceaccount.com"
for role in roles/cloudbuild.builds.builder roles/storage.objectAdmin \
            roles/artifactregistry.writer roles/logging.logWriter \
            roles/cloudsql.client; do
  gcloud projects add-iam-policy-binding my-winch-project \
      --member="serviceAccount:$SA" --role="$role"
done

# and per-secret access for the runtime
for s in AZURE_OPENAI_API_KEY META_ACCESS_TOKEN META_APP_SECRET \
         META_VERIFY_TOKEN TICK_SECRET DATABASE_URL; do
  gcloud secrets add-iam-policy-binding "$s" --project=my-winch-project \
      --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done
```

## 4. Deploy

Use the script — it reads non-secret config from `.env` by an explicit allowlist
so a stray credential can never become a plain env var:

```bash
scripts/sync_secrets.sh      # push any changed secret values first
scripts/deploy.sh
```

Equivalent raw command:

```bash
gcloud run deploy winch \
  --source . --region=europe-west2 --allow-unauthenticated \
  --add-cloudsql-instances=my-winch-project:europe-west2:winch-db \
  --set-env-vars="AZURE_OPENAI_ENDPOINT=...,LLM_MODEL=azure_openai:gpt-4-1-mini,\
META_PHONE_NUMBER_ID=...,CONTRACTOR_WA_ID=...,CONTRACTOR_FIRST_NAME=...,\
CONTRACTOR_BUSINESS_NAME=...,CONTRACTOR_TIMEZONE=Europe/London" \
  --set-secrets="AZURE_OPENAI_API_KEY=AZURE_OPENAI_API_KEY:latest,\
META_ACCESS_TOKEN=META_ACCESS_TOKEN:latest,\
META_APP_SECRET=META_APP_SECRET:latest,\
META_VERIFY_TOKEN=META_VERIFY_TOKEN:latest,\
DATABASE_URL=DATABASE_URL:latest"
```

`--allow-unauthenticated` is required: Meta's webhook cannot present a Google
identity token. The endpoint is protected by `X-Hub-Signature-256` verification,
which fails closed — see `src/winch/webhook.py`.

## 5. The tick

```bash
gcloud scheduler jobs create http winch-tick \
    --project=my-winch-project --location=europe-west2 \
    --schedule="*/5 * * * *" --http-method=POST \
    --uri="https://<SERVICE_URL>/internal/tick" \
    --headers="X-Tick-Secret=<value of the TICK_SECRET secret>"
```

**The tick endpoint is protected by a shared header, not by IAM.** Cloud Run
authentication is per-service, not per-path, and the service must be public so
Meta can reach the webhook — which makes `/internal/tick` public too. An unset
`TICK_SECRET` rejects every request rather than accepting them, so a
misconfigured deploy is inert rather than an open DoS handle.

Scale-to-zero is fine — the tick wakes the service. `claim_due` uses
`FOR UPDATE SKIP LOCKED`, so several instances ticking at once cannot claim the
same touchpoint and double-send.

## 6. Point Meta at it

In the Meta app dashboard, set the webhook callback URL to
`https://<SERVICE_URL>/webhook/meta` and the verify token to the value stored in
`META_VERIFY_TOKEN`. Subscribe to the `messages` field.

`*.run.app` carries a valid managed certificate, so no custom domain is needed.

## Cost

At pilot volume this sits inside free tiers apart from Cloud SQL, which is a few
pounds a month on credits. WhatsApp test-number messaging is free; a real number
bills per template send (~GBP 0.02 in the UK) and needs Meta Business
Verification, which is the thing gated on having a registered entity.
