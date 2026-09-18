# Task: extractor

## Goal
Implement the quote Extractor: rasterise a PDF or accept an image, call Azure
OpenAI with a strict JSON schema, and return a validated `QuoteDraft`.

## Context you need
The Extractor reads real trade quotes, which are inconsistent and frequently
omit things. The single most important behaviour is **null discipline**: a value
that is not in the document must come back `None`. A hallucinated phone number
means the system later texts a stranger on the contractor's behalf. A null is
always correct; a guess is never correct.

Azure OpenAI accepts images, not PDFs. PDFs must be rasterised first.

## Contract — DO NOT CHANGE ANY OF THIS

`QuoteDraft` already exists in `src/winch/state.py`. Import it. Do not redefine
or modify it. `LLMClient` already exists in `src/winch/protocols.py`:

```python
async def extract_quote(self, media: bytes, mime_type: str) -> QuoteDraft: ...
```

Implement in `src/winch/media.py`:

```python
def pdf_to_pngs(pdf_bytes: bytes, dpi: int = 150, max_pages: int = 5) -> list[bytes]:
    """Rasterise via the pdftoppm binary. Raises MediaError on failure.
    Caps at max_pages — a 40-page spec attached to a quote must not be sent."""


class MediaError(Exception): ...
```

Implement in `src/winch/llm/azure.py`:

```python
class AzureExtractor:
    """Satisfies the extract_quote half of LLMClient."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str = "2024-10-21",
        http_client: httpx.AsyncClient | None = None,
    ) -> None: ...

    async def extract_quote(self, media: bytes, mime_type: str) -> QuoteDraft: ...
```

Accepted `mime_type`: `application/pdf`, `image/png`, `image/jpeg`. Anything else
raises `MediaError`.

## The system prompt — use exactly this text
```
You extract structured data from trade quotes for a follow-up system.

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
- The document is the only source. Do not infer, calculate or complete anything.
```

Send the request with `response_format` as a strict `json_schema` derived from
`QuoteDraft`, then validate the response with `QuoteDraft.model_validate_json`.
A response that fails validation raises `MediaError` — never return a partially
populated draft.

## Required behaviour
1. The API key must never appear in a log line, an exception message, or a
   repr. Scrub it from any echoed HTTP error body before raising.
2. Retry on HTTP 429 and 5xx with exponential backoff, 3 attempts maximum.
   Do NOT retry 4xx other than 429 — the request is wrong, repeating it wastes
   money.
3. A request timeout of 120 seconds.
4. `pdf_to_pngs` must not leave temp files behind, including on failure.

## Files you may edit
- `src/winch/media.py`      (new)
- `src/winch/llm/__init__.py` (new)
- `src/winch/llm/azure.py`  (new)
- `tests/test_extractor.py` (new)

## Files you must NOT touch
Everything else. In particular `src/winch/state.py`, `src/winch/protocols.py`,
`src/winch/guards.py`, `src/winch/events.py`, `src/winch/webhook.py`, any script
in `scripts/`, and any existing test file.

## Tests you must write
In `tests/test_extractor.py`. **No network calls** — stub the httpx client with
`httpx.MockTransport`. Cover at minimum:
- a well-formed response parses into a `QuoteDraft`
- a response with `customer_phone: null` yields `None`, not a string "null"
- a malformed / schema-violating response raises `MediaError`
- 429 then 200 succeeds; three consecutive 429s raises
- a 400 is NOT retried (assert the transport was called exactly once)
- an unsupported mime_type raises `MediaError`
- the API key does not appear in the text of any raised exception
- `pdf_to_pngs` on a real fixture returns at least one non-empty PNG
  (`fixtures/quotes/uk_fencing.pdf` — read it, do not modify it)
- `pdf_to_pngs` on garbage bytes raises `MediaError`
- `max_pages` is respected

## Definition of done
```
cd /home/zaibaki/github_projects/wt-extractor
PYTHONPATH=src /home/zaibaki/github_projects/Winch/.venv/bin/python -m pytest tests/test_extractor.py -q
```
must exit 0.

## Forbidden
- New dependencies. httpx, pydantic, pytest are available; nothing else.
- Any network call in a test.
- Changing, weakening or deleting any assertion to make a test pass.
- Refactoring, renaming or reformatting anything outside the four allowed files.
- Editing files not listed above, for any reason.
- Returning a `QuoteDraft` built from anything other than a validated response.
