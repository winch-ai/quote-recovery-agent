#!/usr/bin/env bash
# Report Meta webhook/subscription state. Secrets are used but never printed.
#
#   scripts/with_env.sh -- scripts/check_meta_config.sh
#   scripts/with_env.sh -- scripts/check_meta_config.sh --subscribe
#
# There are TWO separate subscriptions and both are required:
#   1. App level  - App Dashboard > Webhooks > whatsapp_business_account object,
#                   callback URL + the `messages` field.
#   2. WABA level - this app attached to that specific WhatsApp Business
#                   Account, i.e. POST /{WABA_ID}/subscribed_apps.
# Doing only (1) leaves a verified endpoint that silently receives nothing,
# which is indistinguishable from a broken deployment.
set -uo pipefail
set +x

V="${META_GRAPH_VERSION:-v25.0}"
: "${META_ACCESS_TOKEN:?not loaded - run via scripts/with_env.sh}"

if [[ "${1:-}" == "--subscribe" ]]; then
  echo "=== subscribing this app to the WABA ==="
  curl -s -X POST "https://graph.facebook.com/$V/${WABA_ID}/subscribed_apps" \
    -H "Authorization: Bearer $META_ACCESS_TOKEN" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print("  ", d.get("error",{}).get("message","ok")[:200])'
  echo
fi

echo "=== is the app subscribed to the WABA? ==="
curl -s "https://graph.facebook.com/$V/${WABA_ID}/subscribed_apps" \
  -H "Authorization: Bearer $META_ACCESS_TOKEN" \
  | python3 -c '
import json,sys
d=json.load(sys.stdin)
if "error" in d:
    print("  ERROR:", d["error"].get("message","?")[:160]); raise SystemExit
apps=d.get("data",[])
print("  subscribed apps:", len(apps))
for a in apps:
    print("   -", a.get("whatsapp_business_api_data",{}).get("name","?"),
          "| fields:", ",".join(a.get("subscribed_fields",[])) or "NONE")
'

echo
echo "=== phone number status ==="
curl -s "https://graph.facebook.com/$V/${META_PHONE_NUMBER_ID}?fields=display_phone_number,verified_name,quality_rating,platform_type" \
  -H "Authorization: Bearer $META_ACCESS_TOKEN" \
  | python3 -c '
import json,sys
d=json.load(sys.stdin)
if "error" in d:
    print("  ERROR:", d["error"].get("message","?")[:160]); raise SystemExit
for k in ("display_phone_number","verified_name","quality_rating","platform_type"):
    print(f"  {k}: {d.get(k)}")
'
