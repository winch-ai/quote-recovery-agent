#!/usr/bin/env python3
"""Eval harness for the Extractor against fixtures/quotes/.

Scores field-by-field against expected.json. Two checks are weighted as blocking
because they are the ones that cause real-world harm:

  * NULL DISCIPLINE  - a fixture with no customer phone must come back null.
    A hallucinated phone number means the sequence texts a stranger.
  * TOTAL ACCURACY   - the quote total must be exact. It is the one value the
    contractor is legally exposed on.

    scripts/with_env.sh -- python scripts/eval_extraction.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures" / "quotes"
ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
DEPLOYMENT = (os.environ.get("LLM_MODEL") or "").split(":", 1)[-1]

SYSTEM = """You extract structured data from trade quotes for a follow-up system.

Rules that matter more than completeness:
- Return null for anything not present. A null is always better than a guess.
- customer_phone is the CUSTOMER's number. Quotes almost always also show the
  contractor's own phone in the letterhead - never return that one. If only the
  contractor's number appears, return null.
- quote_total is the single headline figure the customer pays. Do NOT sum stage
  payments. Do NOT add optional extras offered as alternatives. Where a gross
  (tax-inclusive) figure is shown, return the gross figure.
- project_title is ALWAYS required - never null. It is a short human description
  of the job, e.g. "2km stock fencing". If the document has no title line, derive
  it from the line items. This is the one field you must always fill.
- The document is the only source. Do not infer, calculate or complete anything."""

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "quote_draft", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "required": ["customer_name", "customer_phone", "customer_email",
                         "project_title", "quote_total", "currency", "expiry_date"],
            "properties": {
                "customer_name": {"type": ["string", "null"]},
                "customer_phone": {"type": ["string", "null"]},
                "customer_email": {"type": ["string", "null"]},
                "project_title": {"type": "string"},
                "quote_total": {"type": ["number", "null"]},
                "currency": {"type": ["string", "null"], "enum": ["GBP", "EUR", "AUD", "USD", None]},
                "expiry_date": {"type": ["string", "null"]},
            },
        },
    },
}


def pages_to_png(pdf: Path) -> list[bytes]:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-png", "-r", "150", str(pdf), f"{tmp}/p"],
                       check=True, capture_output=True)
        return [p.read_bytes() for p in sorted(Path(tmp).glob("p*.png"))]


def extract(pdf: Path):
    content = [{"type": "text", "text": f"Extract the quote. Today is 2026-09-18. File: {pdf.name}"}]
    for png in pages_to_png(pdf):
        b64 = base64.b64encode(png).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    url = f"{ENDPOINT}/openai/deployments/{DEPLOYMENT}/chat/completions?api-version={VERSION}"
    body = {"messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": content}],
            "response_format": SCHEMA, "max_completion_tokens": 600}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("api-key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return json.loads(data["choices"][0]["message"]["content"]), data.get("usage", {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if KEY:
            detail = detail.replace(KEY, "***")
        return {"_error": f"HTTP {exc.code}: {detail}"}, {}


def digits(value):
    return "".join(c for c in str(value) if c.isdigit()) if value else None


def main():
    if not KEY:
        sys.exit("credentials not loaded - run via scripts/with_env.sh")
    expected_all = json.loads((FIXTURES / "expected.json").read_text())

    blocking_failures, total_tokens = [], 0
    for name, expected in expected_all.items():
        pdf = FIXTURES / name
        got, usage = extract(pdf)
        total_tokens += usage.get("total_tokens", 0)
        print(f"\n=== {name} ===")
        print(f"    trap: {', '.join(expected['traps'])}")
        if "_error" in got:
            print(f"    ERROR {got['_error']}")
            blocking_failures.append(f"{name}: request failed")
            continue

        # BLOCKING 1 - null discipline
        exp_phone, got_phone = expected["customer_phone"], got.get("customer_phone")
        if exp_phone is None:
            ok = got_phone is None
            print(f"    {'PASS' if ok else 'FAIL'}  null discipline: phone -> {got_phone!r}")
            if not ok:
                blocking_failures.append(f"{name}: hallucinated phone {got_phone!r}")
        else:
            ok = digits(got_phone) == digits(exp_phone)
            print(f"    {'PASS' if ok else 'FAIL'}  phone -> {got_phone!r} (want {exp_phone!r})")
            if not ok:
                blocking_failures.append(f"{name}: wrong phone {got_phone!r}")

        # BLOCKING 2 - total accuracy
        exp_total, got_total = expected["quote_total"], got.get("quote_total")
        ok = got_total is not None and abs(got_total - exp_total) < 0.01
        print(f"    {'PASS' if ok else 'FAIL'}  total -> {got_total} (want {exp_total})")
        if not ok:
            blocking_failures.append(f"{name}: total {got_total} != {exp_total}")

        # informational
        for field in ("customer_name", "currency", "expiry_date"):
            e, g = expected.get(field), got.get(field)
            mark = "ok  " if (str(e).lower() == str(g).lower()) else "diff"
            print(f"    {mark}  {field} -> {g!r} (want {e!r})")
        print(f"    info  project_title -> {got.get('project_title')!r}")

    print("\n" + "=" * 62)
    if blocking_failures:
        print(f"BLOCKING FAILURES ({len(blocking_failures)}):")
        for f in blocking_failures:
            print(f"  - {f}")
    else:
        print("All blocking checks PASSED")
    print(f"tokens: {total_tokens:,} across {len(expected_all)} fixtures")
    return 1 if blocking_failures else 0


if __name__ == "__main__":
    sys.exit(main())
