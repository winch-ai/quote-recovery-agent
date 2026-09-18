# Task: meta-webhook

## Goal
Implement the Meta WhatsApp Cloud API webhook endpoint: GET verification, POST
receipt with signature verification and atomic deduplication.

## Context you need
Meta calls this endpoint. Two things must be true or the system is unsafe:
signature verification must FAIL CLOSED (a missing, malformed or wrong signature
is a rejection, never a pass), and duplicate deliveries must be dropped BEFORE
any state is touched, because Meta redelivers on any non-2xx response.

## Contract — DO NOT CHANGE ANY OF THIS

Already exists in `src/winch/protocols.py`. Import it. Do not redefine it:

```python
@runtime_checkable
class MessageDeduplicator(Protocol):
    async def seen(self, provider_message_id: str) -> bool:
        """Record the id. Return True if it had already been recorded."""
```

Implement exactly these, in `src/winch/webhook.py`:

```python
class InMemoryDeduplicator:
    """Satisfies MessageDeduplicator. Process-local; Postgres impl comes later."""
    def __init__(self, max_entries: int = 10_000) -> None: ...
    async def seen(self, provider_message_id: str) -> bool: ...


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 header.

    Header format is 'sha256=<hex>'. Returns False for None, empty, malformed,
    wrong-prefix, or mismatched. MUST use hmac.compare_digest. MUST NOT raise.
    """


class ParsedEvent(BaseModel):
    """One normalised inbound item. A single POST may contain several."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["message", "status"]
    provider_message_id: str
    from_wa_id: str | None = None
    text: str | None = None
    media_id: str | None = None
    media_mime: str | None = None
    button_payload: str | None = None
    status: str | None = None          # sent|delivered|read|failed
    error_code: int | None = None      # e.g. 131026
    timestamp: datetime


def parse_webhook(payload: dict) -> list[ParsedEvent]:
    """Flatten Meta's entry[].changes[].value[] envelope.

    Handles: text messages, document messages, image messages, interactive
    button_reply, and status callbacks. Unknown types are SKIPPED, never raised
    on — Meta adds new ones without notice and a 500 causes redelivery storms.
    """


def build_router(
    app_secret: str,
    verify_token: str,
    deduplicator: MessageDeduplicator,
    on_event: Callable[[ParsedEvent], Awaitable[None]],
) -> APIRouter:
    """Router with:
       GET  /webhook/meta  -> hub.challenge echo when hub.verify_token matches,
                              403 otherwise
       POST /webhook/meta  -> 403 on bad signature; otherwise parse, drop
                              duplicates, await on_event for each new event,
                              return 200
    """
```

## Required behaviour
1. `verify_signature` uses `hmac.compare_digest`. Never `==`.
2. POST returns **403** before parsing if the signature fails.
3. POST returns **200** even when the body contains nothing useful. A non-2xx
   makes Meta redeliver.
4. Deduplication happens before `on_event` is called. A duplicate id must not
   invoke `on_event` a second time.
5. `on_event` raising must not produce a 500 — log it and still return 200,
   otherwise one bad event causes Meta to redeliver the whole batch forever.
6. No secret, token or signature value may appear in any log line.

## Files you may edit
- `src/winch/webhook.py`  (new)
- `tests/test_webhook.py` (new)

## Files you must NOT touch
Everything else. In particular `src/winch/protocols.py`, `src/winch/state.py`,
`src/winch/guards.py`, `src/winch/events.py`, and any existing test file.

## Tests you must write
In `tests/test_webhook.py`, covering at minimum:
- valid signature accepted; wrong signature 403; missing header 403; malformed
  header ('sha256=', 'garbage', '') 403
- GET verification: correct token echoes `hub.challenge`; wrong token 403
- duplicate `provider_message_id` invokes `on_event` exactly once
- parse handles text, document, image, interactive button_reply, and status
- unknown message type is skipped without raising
- `on_event` raising still yields 200
- a POST with an empty `entry` list yields 200

No network calls. No real credentials. Build payloads as dict literals.

## Definition of done
```
cd /home/zaibaki/github_projects/wt-meta-webhook
PYTHONPATH=src /home/zaibaki/github_projects/Winch/.venv/bin/python -m pytest tests/test_webhook.py -q
```
must exit 0.

## Forbidden
- New dependencies. fastapi, pydantic, pytest, httpx are available; nothing else.
- Changing, weakening or deleting any assertion to make a test pass.
- Refactoring, renaming or reformatting anything outside the two allowed files.
- Editing files not listed above, for any reason.
- Catching an exception and silently continuing without logging it.
