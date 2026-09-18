# Task: compose-triage

## Goal
Two things: pure code that fills approved-template variables, and the LLM intent
classifier for inbound customer replies.

## Context you need
Template variables are the customer's name, the contractor's name, the business
name, the project title and the quote total. **Every one of those already exists
in frozen state or config, so none of them needs a model.** Filling them in code
removes an entire hallucination surface — a model cannot invent a price it is
never asked to produce.

The classifier is different: it reads free text a customer wrote and must map it
to a fixed set of intents. That is genuine LLM work.

## Contract — DO NOT CHANGE ANY OF THIS

From `winch.state`: `Quote`, `Intent`. From `winch.guards`:
`assert_no_stray_numbers`, `GuardViolation`. Import them; do not redefine.

Implement in `src/winch/compose.py`:

```python
class ContractorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contractor_id: str
    first_name: str
    business_name: str
    wa_id: str
    timezone: str = "Europe/London"


def format_money(amount: float, currency: str) -> str:
    """'GBP 24,504.00' style. Two decimals, thousands separators, no symbol
    guessing — the currency code prefixes it."""


def build_template_variables(
    quote: Quote, template_name: str, contractor: ContractorProfile
) -> list[str]:
    """Ordered variables for an approved template. Pure — no model, no I/O.

    checkin_soft   -> [customer first name, contractor first name,
                       business name, project title, formatted total]
    schedule_nudge -> [customer first name, contractor first name,
                       business name, project title]
    soft_close     -> [customer first name, contractor first name,
                       business name, project title]

    Raises ValueError on an unknown template name. Customer first name is the
    first whitespace-separated token of quote.customer_name.
    """


def guard_freeform(body: str, quote: Quote) -> None:
    """Run assert_no_stray_numbers over a model-drafted reply.

    The allowlist is exactly the numbers the system legitimately knows: the
    formatted total, the raw total, and the year/month/day parts of
    quote.expiry_date if set. Anything else the model produced is a fabrication
    and must raise GuardViolation.
    """
```

Implement in `src/winch/llm/azure.py` — **add to the existing file, do not
restructure it, do not touch `AzureExtractor.extract_quote`**:

```python
class AzureTextClient:
    """classify_intent and compose_reply. Same Azure deployment as extraction."""

    def __init__(self, endpoint: str, api_key: str, deployment: str,
                 api_version: str = "2024-10-21",
                 http_client: httpx.AsyncClient | None = None) -> None: ...

    async def classify_intent(self, text: str) -> Intent: ...
    async def compose_reply(self, quote: Quote, customer_message: str) -> str: ...
```

## Classifier rules
Use a strict `json_schema` response format with a single `intent` field whose
enum is exactly the `Intent` values. Map an unparseable or unexpected answer to
`Intent.UNCLEAR` — never guess, and never raise, because an unclassifiable reply
must still reach the contractor.

Use exactly this system prompt:
```
You classify a customer's reply to a trade quote follow-up. Return one intent.

ACCEPTED - they agree, want to proceed, or confirm dates.
PRICE_OBJECTION - they mention cost, budget, a cheaper alternative, or say it is
  too expensive.
QUESTION_ON_TIMELINE - they ask about dates, scheduling, lead time or duration.
TECHNICAL_SCOPE_QUERY - they ask what is included, materials, dimensions,
  methods, or anything requiring the contractor's expertise.
UNSUBSCRIBE - they ask to stop being contacted, or reply STOP.
UNCLEAR - anything else, or you are not confident.

Prefer UNCLEAR over a wrong guess. A misrouted reply is worse than an
unclassified one, because the contractor reads every unclassified reply anyway.
```

`compose_reply` drafts a short, plain reply in the contractor's voice. It must
never state a price, a date or a commitment — the caller runs `guard_freeform`
over the result and a fabricated number raises.

## Required behaviour
1. The API key never appears in a log line or exception message.
2. Retry 429/5xx, 3 attempts, exponential backoff. Never retry other 4xx.
3. `classify_intent` returns `Intent.UNCLEAR` rather than raising, for any
   response it cannot map — including an HTTP failure after retries.

## Files you may edit
- `src/winch/compose.py`      (new)
- `src/winch/llm/azure.py`    (ADD to; do not restructure existing code)
- `tests/test_compose.py`     (new)
- `tests/test_triage.py`      (new)

## Files you must NOT touch
Everything else, in particular `protocols.py`, `state.py`, `guards.py`,
`events.py`, `webhook.py`, `supervisor.py`, `media.py`, `repository.py`,
`db.py`, `channels/`, and every existing test file — including
`tests/test_extractor.py`.

## Tests you must write
`tests/test_compose.py` (pure, no HTTP):
- each template name returns the right number of variables in the right order
- customer first name is extracted from a full name
- a single-word customer name still works
- unknown template name raises ValueError
- `format_money` renders 24504.0/GBP as "GBP 24,504.00"
- `format_money` handles a whole number and a value under 1000
- `guard_freeform` passes text containing the quote total
- `guard_freeform` raises GuardViolation on an invented price
- `guard_freeform` raises on an invented date
- variables built for `checkin_soft` pass `guard_freeform` when joined

`tests/test_triage.py` (httpx.MockTransport, no network):
- each intent value round-trips from a well-formed response
- an unknown intent string maps to UNCLEAR
- malformed JSON maps to UNCLEAR, does not raise
- persistent HTTP 500 maps to UNCLEAR, does not raise
- a 400 is not retried
- the api key is absent from every exception message

## Definition of done
```
cd /home/zaibaki/github_projects/wt-compose
PYTHONPATH=src /home/zaibaki/github_projects/Winch/.venv/bin/python -m pytest tests/test_compose.py tests/test_triage.py tests/test_extractor.py -q
```
must exit 0 — note `test_extractor.py` is included to prove you did not break it.

## Forbidden
- New dependencies.
- Any network call in a test.
- Restructuring, reformatting or renaming anything already in `llm/azure.py`.
- Changing, weakening or deleting any assertion to make a test pass.
- Editing files not listed above.
