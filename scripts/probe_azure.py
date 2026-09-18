#!/usr/bin/env python3
"""Probe the Azure OpenAI deployments for the capabilities the Extractor needs.

Run through the credential wrapper so the key never enters this shell:

    scripts/with_env.sh -- python scripts/probe_azure.py

Prints PASS/FAIL per capability. Never prints the API key; any key substring is
scrubbed from error output before display.
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
ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")


def _deployment(var, default=None):
    raw = os.environ.get(var, default) or ""
    return raw.split(":", 1)[-1] if raw else None


def _scrub(text):
    """Never let the key reach stdout, even via an echoed error."""
    if KEY and len(KEY) > 8:
        text = text.replace(KEY, "***REDACTED***")
    return text


def call(deployment, messages, response_format=None, max_tokens=300):
    url = f"{ENDPOINT}/openai/deployments/{deployment}/chat/completions?api-version={VERSION}"
    body = {"messages": messages, "max_completion_tokens": max_tokens}
    if response_format:
        body["response_format"] = response_format
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("api-key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
        return True, data["choices"][0]["message"]["content"], data.get("usage", {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        return False, f"HTTP {exc.code}: {_scrub(detail)}", {}
    except Exception as exc:  # noqa: BLE001 - probe must report, not raise
        return False, _scrub(f"{type(exc).__name__}: {exc}"), {}


def page_to_png(pdf: Path) -> bytes:
    """Azure OpenAI chat completions accept images, not PDFs — rasterise first."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["pdftoppm", "-png", "-r", "150", "-f", "1", "-l", "1", str(pdf), f"{tmp}/page"],
            check=True, capture_output=True,
        )
        return next(Path(tmp).glob("page*.png")).read_bytes()


def report(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail:
        for line in str(detail).splitlines():
            print(f"        {line}")
    return ok


def main():
    if not ENDPOINT or not KEY:
        sys.exit("AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not loaded. "
                 "Run via scripts/with_env.sh")

    main_model = _deployment("LLM_MODEL")
    alt_model = _deployment("LLM_MODEL_CASE_GENERATOR")
    print(f"endpoint     {ENDPOINT}")
    print(f"api-version  {VERSION}")
    print(f"deployments  {main_model}  |  {alt_model}\n")

    results = {}

    print("1. Basic chat completion")
    ok, out, usage = call(main_model, [{"role": "user", "content": "Reply with exactly: OK"}],
                          max_tokens=20)
    results["chat"] = report(main_model, ok, out if not ok else
                             f"{out.strip()!r}  (tokens: {usage.get('total_tokens', '?')})")

    print("\n2. Vision — rasterised quote page")
    if results["chat"]:
        png = page_to_png(ROOT / "fixtures" / "quotes" / "uk_fencing.pdf")
        b64 = base64.b64encode(png).decode()
        ok, out, usage = call(main_model, [{"role": "user", "content": [
            {"type": "text", "text": "What is the customer's mobile number on this quote? "
                                     "Reply with the number only, or NONE."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}], max_tokens=60)
        results["vision"] = report(f"{main_model} vision", ok,
                                   out if not ok else f"{out.strip()!r}  (expected 07700 900412)")
    else:
        results["vision"] = report(f"{main_model} vision", False, "skipped, chat failed")

    print("\n3. Structured output (json_schema)")
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "quote", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["customer_name", "quote_total"],
                "properties": {
                    "customer_name": {"type": ["string", "null"]},
                    "quote_total": {"type": ["number", "null"]},
                },
            },
        },
    }
    ok, out, _ = call(main_model,
                      [{"role": "user", "content": "Mark Henderson, total GBP 24,504.00"}],
                      response_format=schema, max_tokens=100)
    results["structured"] = report(f"{main_model} json_schema", ok, out.strip() if out else "")

    print(f"\n4. Second deployment ({alt_model})")
    if alt_model:
        ok, out, _ = call(alt_model, [{"role": "user", "content": "Reply with exactly: OK"}],
                          max_tokens=20)
        results["alt"] = report(alt_model, ok, out.strip() if out else "")
    else:
        results["alt"] = report("alt deployment", False, "not configured")

    print("\n" + "-" * 60)
    for k, v in results.items():
        print(f"  {k:<12} {'PASS' if v else 'FAIL'}")
    return 0 if all(results[k] for k in ("chat", "vision", "structured")) else 1


if __name__ == "__main__":
    sys.exit(main())
