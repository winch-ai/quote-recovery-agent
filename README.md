# Winch

**A LangGraph agent that lives in WhatsApp and chases dormant, high-ticket
trade quotes so the contractor doesn't have to.**

A tradesperson (fencing, sheds, groundworks — the kind of job quoted in the
thousands, not the hundreds) forwards a PDF or a photo of a handwritten
quote over WhatsApp. Winch parses it, proposes a follow-up cadence, and asks
for one-tap approval. Nothing goes out to the customer without the
contractor seeing it first — every send sits behind a hard human-in-the-loop
gate, and the LLM is never allowed to produce a price, date, or line item
that reaches a customer.

## Status

**Weekend-project pilot, now wound down.** It ran for real against one
contractor's live WhatsApp number and real quotes on Cloud Run + Postgres,
with CI/CD auto-deploying from `main`. The cloud infrastructure has since
been torn down; what's left here is the code, the tests, and the design
notes. See `docs/DESIGN.md` for the reasoning behind each decision and
`CLAUDE.md` for the hard rules the build didn't compromise on.

## How it works

```
CONTRACTOR (WhatsApp)          FASTAPI / CLOUD RUN              CUSTOMER
      │                                 │                          │
      ├─ PDF / photo ──────► POST /webhook/meta                    │
      │                      ├ verify X-Hub-Signature-256          │
      │                      ├ dedupe on message.id                │
      │                      └ resume graph ──┐                    │
      │                                       ▼                    │
      ◄─ "Parsed GBP 24,504 …      ┌──────────────────┐            │
      │   approve?" ──[interrupt]──│  LangGraph       │            │
      ├─ "yes" ────────────────────│  (Postgres       │─ template ─►│
      │                            │   checkpointer)  │            │
      ◄─ "Dave replied. tel:…" ◄───└──────────────────┘◄─ reply ───┤
                                             ▲
Cloud Scheduler ──► POST /internal/tick ─────┘
```

Side by side with that lifecycle graph is `winch/concierge.py` — a small,
strictly read-only LangGraph tool-calling agent that answers the
contractor's own free-text status questions ("what's pending", "who was
that quote for", "how many went out this week") by querying real state
instead of guessing. It cannot send, approve, schedule, or create anything;
every action that touches a customer or money still goes through the
deterministic graph above.

## Non-negotiables

These are enforced in code and proven by tests, not left to review:

| Rule | Where | Proven by |
| --- | --- | --- |
| The LLM never produces a price, date or line item that reaches a customer | `guards.assert_no_stray_numbers`, `compose.build_template_variables` | `test_guards.py`, `test_compose.py` |
| Nothing reaches a customer without a human gate | `supervisor`, `graph` | `test_supervisor.py`, `test_graph.py` — proven over the routing table **and** over the compiled graph |
| Timing is arithmetic, never a model decision | `guards.next_business_window`, `scheduler` | `test_scheduler.py` |
| The concierge cannot send, approve, schedule, or create a quote | `concierge.py` (tools are read-only by construction) | `test_concierge.py` |
| Duplicate webhooks cannot double-process | `webhook.InMemoryDeduplicator`, `repository` | `test_webhook.py` |
| Concurrent instances cannot double-send | `repository.claim_due` (`FOR UPDATE SKIP LOCKED`) | `test_repository.py` |
| A halted sequence never resumes on a timer | `supervisor` | `test_supervisor.py` |
| Secrets never reach a log, repr or transcript | `config`, `with_env.sh` | `test_config.py`, `test_env_loader.py` |

## Layout

```
src/winch/
  state.py        schema + graph state          | contract
  guards.py       money + timing guards         | contract
  supervisor.py   deterministic routing table   | contract
  protocols.py    interfaces                    | contract
  events.py       the event log                 | contract
  graph.py        LangGraph wiring              | contract
  nodes.py        node implementations          | contract
  concierge.py    read-only Q&A agent (outside the lifecycle graph)
  compose.py      template variables (pure)
  scheduler.py    touchpoint plan (pure)
  webhook.py      Meta webhook
  channels/       WhatsApp adapter
  llm/            Azure OpenAI extraction + triage
  db.py           schema
  repository.py   Postgres implementations
  app.py          FastAPI surface
```

Files marked *contract* are written by hand and never delegated; see
`docs/WORKFLOW.md`.

## Running the tests

```bash
docker run -d --name winch-pg -e POSTGRES_PASSWORD=winchtest \
    -e POSTGRES_DB=winch_test -p 55432:5432 postgres:16-alpine
python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
PYTHONPATH=src ./.venv/bin/python -m pytest tests/ -q
```

`pdftoppm` (poppler-utils) is required: Azure OpenAI accepts images, not PDFs.

## CI

`.github/workflows/ci-cd.yml` runs the full suite (including the Postgres
integration tests, against a real `postgres:16` service container) on every
push and pull request to `main`. It previously also deployed straight to a
live Cloud Run service on a passing push to `main`, authenticated via
Workload Identity Federation rather than a stored key; that deploy target
has since been decommissioned along with the rest of the pilot's cloud
footprint. `scripts/deploy.sh` and `docs/DEPLOY.md` are kept as a reference
for standing the same setup back up against your own GCP project.

## Credentials

Never read `.env`. Wrap anything that needs it:

```bash
scripts/with_env.sh -- python -m pytest tests/
scripts/with_env.sh --names          # audit what is configured, values never printed
```

## Docs

- `CLAUDE.md` — product thesis, architecture decisions, and the hard rules
  this build does not compromise on
- `docs/DESIGN.md` — the agreed system design and why each choice was made
- `docs/WORKFLOW.md` — how work is delegated and audited
- `docs/DEPLOY.md` — Cloud Run deployment
- `templates/whatsapp/` — the approved-template catalogue and how to submit it
