#!/usr/bin/env python3
"""Submit the WhatsApp template catalogue to Meta, or poll approval status.

Templates live as JSON in templates/whatsapp/ so they are versioned and diffable.
Resubmitting after a rejection is a wording change plus a rerun.

    export WABA_ID=...
    export META_ACCESS_TOKEN=...

    python scripts/submit_templates.py            # submit all
    python scripts/submit_templates.py --status   # poll approval status
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v23.0")
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "whatsapp"


def _env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} is not set. See templates/whatsapp/README.md")
    return value


def _call(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {"error": json.loads(exc.read() or b"{}").get("error", {"message": str(exc)})}


def submit():
    waba_id, token = _env("WABA_ID"), _env("META_ACCESS_TOKEN")
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{waba_id}/message_templates"
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        payload = json.loads(path.read_text())
        result = _call("POST", url, token, payload)
        if "error" in result:
            print(f"  FAILED  {payload['name']}: {result['error'].get('message')}")
        else:
            print(f"  sent    {payload['name']} -> {result.get('status', 'SUBMITTED')}")


def status():
    waba_id, token = _env("WABA_ID"), _env("META_ACCESS_TOKEN")
    query = urllib.parse.urlencode({"fields": "name,status,category,language", "limit": 100})
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{waba_id}/message_templates?{query}"
    result = _call("GET", url, token)
    if "error" in result:
        sys.exit(f"  FAILED  {result['error'].get('message')}")
    local = {json.loads(p.read_text())["name"] for p in TEMPLATE_DIR.glob("*.json")}
    for row in result.get("data", []):
        if row["name"] in local:
            print(f"  {row['status']:<10} {row['name']:<16} {row['category']:<10} {row['language']}")


if __name__ == "__main__":
    status() if "--status" in sys.argv else submit()
