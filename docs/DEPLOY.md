# Deploying to Cloud Run

Runs on existing GCP credits. Everything below is one-time except the deploy.

## 1. Project and services

```bash
gcloud config set project <PROJECT_ID>
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
    secretmanager.googleapis.com cloudscheduler.googleapis.com \
    artifactregistry.googleapis.com
```

## 2. Database

Smallest tier is ample: one contractor, tens of quotes.

```bash
gcloud sql instances create winch-db \
    --database-version=POSTGRES_16 --tier=db-f1-micro --region=europe-west2
gcloud sql databases create winch --instance=winch-db
gcloud sql users set-password postgres --instance=winch-db --password=<PASSWORD>
```

## 3. Secrets

Never passed as plain env vars on the service — they end up in deploy logs and
in `gcloud run services describe` output.

```bash
for name in AZURE_OPENAI_API_KEY META_ACCESS_TOKEN META_APP_SECRET \
            META_VERIFY_TOKEN DATABASE_URL; do
  gcloud secrets create $name --replication-policy=automatic
done
# then add versions interactively - do not echo values into shell history:
gcloud secrets versions add AZURE_OPENAI_API_KEY --data-file=-
```

## 4. Deploy

```bash
gcloud run deploy winch \
  --source . --region=europe-west2 --allow-unauthenticated \
  --add-cloudsql-instances=<PROJECT_ID>:europe-west2:winch-db \
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
    --location=europe-west2 --schedule="*/5 * * * *" \
    --uri="https://<SERVICE_URL>/internal/tick" --http-method=POST \
    --oidc-service-account-email=<SA>@<PROJECT_ID>.iam.gserviceaccount.com
```

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
