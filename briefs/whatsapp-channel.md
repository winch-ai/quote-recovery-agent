# Task: whatsapp-channel

## Goal
Implement the Meta WhatsApp Cloud API outbound channel and media download.

## Context you need
This is the only path by which a message reaches a real customer under the
contractor's name. Two behaviours are safety-critical:

- A **free-form** message is only legal inside Meta's 24-hour service window,
  which opens when the customer messages us. Outside it, Meta requires an
  approved template. The adapter must **fail closed**: if it cannot confirm the
  window is open, it refuses rather than sending.
- Error `131026` means undeliverable, which usually means the number is not on
  WhatsApp. That must surface as `unreachable=True` so the graph falls back to
  human relay, not as a generic failure.

## Contract — DO NOT CHANGE ANY OF THIS

Already in `src/winch/protocols.py`. Import; do not redefine:

```python
class SendResult(BaseModel):
    ok: bool
    provider_message_id: str | None = None
    error_code: int | None = None
    unreachable: bool = False

class ChannelAdapter(Protocol):
    name: str
    async def send_template(self, to: str, template_name: str,
                            variables: list[str]) -> SendResult: ...
    async def send_freeform(self, to: str, body: str) -> SendResult: ...
```

Implement in `src/winch/channels/whatsapp.py`:

```python
class WhatsAppChannel:
    """Satisfies ChannelAdapter. Meta Cloud API."""

    name = "whatsapp"

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        language_code: str = "en_GB",
        window_checker: Callable[[str], Awaitable[bool]] | None = None,
        http_client: httpx.AsyncClient | None = None,
        graph_version: str = "v23.0",
    ) -> None:
        """window_checker(to) -> True when a 24-hour window is open for that
        recipient. When None, send_freeform ALWAYS refuses — fail closed."""

    async def send_template(self, to: str, template_name: str,
                            variables: list[str]) -> SendResult: ...
    async def send_freeform(self, to: str, body: str) -> SendResult: ...
    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Two-step Meta flow: GET /{media_id} for the URL, then GET that URL
        with the bearer token. Returns (bytes, mime_type). Raises ChannelError."""


class ChannelError(Exception): ...
```

## Request shapes (Meta Cloud API)
Template send, `POST /{graph_version}/{phone_number_id}/messages`:
```json
{"messaging_product":"whatsapp","to":"<to>","type":"template",
 "template":{"name":"<name>","language":{"code":"<language_code>"},
 "components":[{"type":"body","parameters":[{"type":"text","text":"<v1>"}]}]}}
```
Omit `components` entirely when `variables` is empty.

Free-form send:
```json
{"messaging_product":"whatsapp","to":"<to>","type":"text",
 "text":{"preview_url":false,"body":"<body>"}}
```

A success response carries `messages[0].id` — that is `provider_message_id`.
An error response carries `error.code`.

## Required behaviour
1. `send_freeform` with no `window_checker`, or a checker returning False,
   returns `SendResult(ok=False)` **without making an HTTP request**. Assert this
   in a test by counting transport calls.
2. Error code `131026` yields `unreachable=True`.
3. The access token must never appear in a log line or an exception message.
   Scrub it from any echoed error body.
4. Retry on 429 and 5xx with exponential backoff, 3 attempts max. Never retry
   other 4xx.
5. 30-second request timeout.

## Files you may edit
- `src/winch/channels/__init__.py` (new)
- `src/winch/channels/whatsapp.py` (new)
- `tests/test_whatsapp.py`         (new)

## Files you must NOT touch
Everything else, in particular `src/winch/protocols.py`, `state.py`, `guards.py`,
`events.py`, `webhook.py`, `supervisor.py`, `media.py`, `llm/`, `repository.py`,
`db.py`, and every existing test file.

## Tests you must write
`tests/test_whatsapp.py`, using `httpx.MockTransport`. **No network calls.** Cover:
- template send builds the exact JSON shape above; `provider_message_id` extracted
- empty `variables` omits `components`
- error 131026 -> `ok=False, unreachable=True`
- another error code -> `ok=False, unreachable=False`
- `send_freeform` with no window_checker makes ZERO HTTP calls and returns ok=False
- `send_freeform` with a checker returning False makes ZERO HTTP calls
- `send_freeform` with a checker returning True does send
- 429 then 200 succeeds; three 429s gives ok=False
- a 400 is not retried (assert exactly one transport call)
- access token appears in no exception message
- `download_media` performs the two-step flow and returns bytes plus mime type
- `download_media` raises ChannelError on a failed lookup

## Definition of done
```
cd /home/zaibaki/github_projects/wt-whatsapp
PYTHONPATH=src /home/zaibaki/github_projects/Winch/.venv/bin/python -m pytest tests/test_whatsapp.py -q
```
must exit 0.

## Forbidden
- New dependencies. httpx, pydantic, pytest, pytest-asyncio only.
- Any network call in a test.
- Sending a free-form message when the window cannot be confirmed open.
- Changing, weakening or deleting any assertion to make a test pass.
- Editing files not listed above.
