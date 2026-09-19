# Winch — System Design v1

_Agreed 18 September 2026. Supersedes the SMS-based customer channel in the v2.0 assessment._

## Scope of this build

A **measurement instrument**, not a product. One contractor, real quotes, real customers. Its job is
to settle two questions: does the contractor answer the approval gate, and does a real customer reply
to a revived quote. Everything that does not serve those two questions is out of scope.

**Out of scope:** auth, onboarding UI, dashboard, billing, multi-tenancy. Contractor identity is an
allow-listed `wa_id` in config.

## 1. Channels

### Contractor ↔ agent — WhatsApp (control plane)

Contractor always initiates, so this stays inside Meta's 24-hour service window and free-form
interactive messages are available throughout. Ingests **PDF and photo** of a written quote.

### Agent ↔ customer — WhatsApp primary, email fallback

The customer's WhatsApp number and email are **collected from the contractor during confirmation**,
not scraped from the PDF. Most trade quotes do not contain a mobile number; asking is more reliable
than parsing.

**There is no way to pre-check whether a number is on WhatsApp.** Meta removed the `contacts`
endpoint from the Cloud API, and delivery errors are deliberately vague bucket errors so businesses
cannot infer block/inactive/not-registered. Channel resolution is therefore **send-and-observe**:

1. Attempt the WhatsApp template send.
2. On error `131026`, **or** no `delivered` status webhook inside the timeout window → mark the
   customer `WHATSAPP_UNREACHABLE` and fall back to email for this and all later touchpoints.
3. Record the resolution on the quote so the fallback is sticky, not re-attempted per touchpoint.
4. In v1 the fallback is **human relay**, not email — see section 3.

## 2. The template catalogue (lead-time dependency — submit early)

The 5-day follow-up is business-initiated and outside the 24-hour window, so it **must** be a
pre-approved template. Button labels are frozen at approval time and capped at three — they cannot
be generated per message.

The agent therefore performs **dynamic selection from a fixed catalogue**, not dynamic generation:

| Template | Buttons | Used when |
| `checkin_soft` | `Any questions?` · `Book a call` · `Not right now` | Touchpoint 1, neutral tone |
| `schedule_nudge` | `Confirm dates` · `Need to delay` | Touchpoint 2, quote has milestones or crew dates |
| `soft_close` | `Still interested` · `Not going ahead` | Touchpoint 3, final |

URL buttons may carry a per-quote dynamic suffix (e.g. a booking deep link scoped to the job).

**Category risk:** Utility requires the message to relate to an existing transaction; a pending quote
is arguable. Word every template to lean hard on the existing-job framing. A Marketing
classification costs more and carries stricter rules.

**Once the customer taps a button or replies, the 24-hour window opens** — from that point the agent
may send free-form interactive messages with dynamically generated buttons.

## 3. Email path

**Not built in v1.** Email requires a domain for sender identity and inbound routing, and the project
name is still a placeholder — buying one now would commit to a name and spend money on a path whose
necessity has not been measured.

### v1 — human relay

When channel resolution marks a customer `WHATSAPP_UNREACHABLE`, the agent does not fail silently.
It messages the contractor with the drafted follow-up text and asks whether they want to send it
themselves. Covers the case, costs nothing, roughly an hour of work.

Every occurrence writes a `channel_unavailable` event. **That number is the trigger condition:** it
tells us what share of real customers are unreachable on WhatsApp, and therefore whether a built
email channel is worth a domain, a name decision and a verification queue. Neither of us knows that
number today.

### v2 — build it when the data says so

If the fallback rate justifies it, the design is settled and recorded here so it need not be
re-litigated:

- **Send** from the project domain with the From-name set to the contractor's business, and a
  per-quote `Reply-To` token. Inbound via Cloudflare Email Routing (free) → webhook. Contractor
  effort: none.
- **Later upgrade** to OAuth **`gmail.send` only** — a *sensitive* scope needing verification but
  **no CASA audit** — so mail comes from the contractor's real address. Contractor effort: one
  consent tap.
- **Never request a Gmail read scope.** `gmail.readonly` / `gmail.modify` are *restricted* and
  trigger a paid CASA Tier 2 third-party audit. Replies return to our own `Reply-To`, so send-only
  is always sufficient.
- **Not usable before verification completes:** Google expires refresh tokens after 7 days for apps
  in Testing, and a quote lifecycle runs 2-30+ days.
- **App passwords are rejected** on UX grounds — they push exactly the desk work onto the contractor
  that the product exists to remove.

## 4. Orchestration

**The supervisor is deterministic.** Routing is fully determined by `(current_state, event_type)` —
a truth table. An LLM router would add nondeterminism and cost to something with exactly one right
answer. LangGraph conditional edges, not a model.

LLM work concentrates in four specialist sub-agents:

| Sub-agent | Input | Output | Deterministic? |
| Extractor | PDF or photo | strict `QuoteDraft` schema | no — LLM |
| Composer | quote + touchpoint slot | tone + chosen template id + variables | no — LLM |
| Triage | inbound message (either channel) | structured intent | no — LLM |
| Channel Resolver | send result / status webhook | channel state transition | yes — code |

### Graph nodes

| Node | Does | LLM |
| `ingest` | Fetch media from Meta by id, sniff PDF vs image | — |
| `extract` | Document/image → `QuoteDraft` | yes |
| `collect_contact` | Ask contractor for customer WhatsApp number + email | — |
| `confirm` | Parsed summary + proposed cadence → `interrupt()` | — |
| `apply_edits` | Contractor correction → re-extract → back to `confirm` | yes |
| `schedule` | Write absolute UTC touchpoint times to `due_touchpoints` | **never** |
| `compose` | Select template, fill variables, set tone | yes |
| `gate` | Preview to contractor → `interrupt()` → approve / hold | — |
| `send` | Dispatch via `ChannelAdapter` | — |
| `observe` | Delivery webhook or timeout → channel resolution | — |
| `relay` | v1 fallback: hand drafted text to contractor to send themselves | — |

**Interrupt nodes are split from the nodes that prompt.** LangGraph re-executes a
node from the top on every resume, so a side effect placed before `interrupt()`
fires again on each one. Each gate is therefore two nodes: one that performs the
side effect and *commits*, and one that only interrupts —
`collect_contact`/`await_contact`, `confirm`/`await_confirm`, `gate`/`await_gate`.

This is not cosmetic. `approval_prompt_sent` and `gate_prompt_sent` are the start
of the time-to-approve measurement the pilot exists to produce; emitting them on
replay would silently corrupt the only number that matters.

**`Command(resume={})` is ignored.** An empty payload does not resume — the node
interrupts again and the graph looks stuck rather than erroring. Nothing may
construct an empty resume payload; pinned by `test_app_wiring.py`.
| `triage` | Inbound customer reply → intent | yes |
| `alert` | Halt sequence, tap-to-call / reply link to contractor | — |
| `archive` | Terminal state | — |

Intents: `QUESTION_ON_TIMELINE | PRICE_OBJECTION | ACCEPTED | TECHNICAL_SCOPE_QUERY |
UNSUBSCRIBE | UNCLEAR`. The classifier prefers `UNCLEAR` to a wrong guess — a
misrouted reply is worse than an unclassified one, because the contractor reads
every unclassified reply anyway.

An unsubscribe routes to `archive`, not `alert`: it must not ping the contractor
as though the customer asked a question, and it must skip the remaining
touchpoints.

### The concierge — outside the graph, on purpose

Before a contractor's non-media message resumed a pending interrupt or was
recorded, a plain-text message with nothing pending got one hardcoded
acknowledgement regardless of content. Real usage showed the gap: "what's
pending", "how many went out this week", "create an invoice" all produced the
same unhelpful reply.

`winch/concierge.py` fixes this without touching the table above: it is a
small, uncheckpointed LangGraph ReAct loop (`agent` ⇄ `tools`, capped at
`MAX_TOOL_TURNS`) invoked only when `_route_event` finds no pending thread for
a contractor message. Its tools are read-only — `list_pending_quotes` (from
`quote_threads` + live graph state) and `count_recent_activity` (from the
`events` log) — and it has no tool that can send, approve, schedule, or
create a quote. Creating one still requires forwarding the document; the
concierge's job is only to say so when asked otherwise. Any failure (LLM
error, runaway loop, tool exception) degrades to the original safe
acknowledgement string rather than crashing message handling.

### Gate design — deliberately strict for v1

`gate` is a **hard `interrupt()` on every outbound**, even though the assessment argues front-loaded
approval will prove necessary. This is intentional: the demo exists to *measure* whether the gate
stalls. Relaxing a gate later is trivial; observing the honest version fail happens once.

## 5. Guards enforced in code, not prompts

**Immutable money.** `quote` is frozen after approval. `compose` returns text containing only
`{total}` / `{client}` placeholders; code substitutes the literals afterwards. A validator then
rejects any composed message containing a digit run not on the allowlist. A hallucinated price
cannot physically reach a customer.

**Deterministic timing.** `schedule` writes absolute UTC timestamps at approval time, shifted into
09:00–17:00 local weekday hours (`Europe/London`, `Europe/Dublin` or `Australia/Sydney` from
contractor config). Cloud Scheduler polls `/internal/tick`; the poller uses
`SELECT … FOR UPDATE SKIP LOCKED` so Cloud Run scaling out cannot double-send.

**Idempotency.** Meta redelivers webhooks. Dedupe on `message.id` before touching the graph.

**Unsubscribe.** STOP/opt-out handling on both channels before the first real customer message.

## 6. The instrument

`events(id, quote_id, type, payload_json, ts)` written on every transition:
`quote_received`, `parse_completed`, `contact_collected`, `approval_prompt_sent`,
`approval_received`, `touchpoint_sent`, `delivery_confirmed`, `channel_fallback`,
`customer_replied`.

Headline metric: `approval_received.ts − approval_prompt_sent.ts`, plus the ratio of gates
approved / vetoed / never answered. This is the reason we are building rather than interviewing.

## 7. Deployment (GCP, on existing credits)

| Piece | Service | Note |
| API + webhooks | Cloud Run | Free managed HTTPS on `*.run.app`; Meta accepts it, no domain needed |
| State | Cloud SQL Postgres, smallest tier | LangGraph checkpointer + app tables + event log |
| Timing | Cloud Scheduler → `POST /internal/tick` | 5-minute cadence, free tier |
| Secrets | Secret Manager | Meta token, Bedrock creds |

Scale-to-zero is fine — the scheduler wakes the service.

## 7b. Template variables are filled in code

Every variable an approved template takes — customer name, contractor name,
business name, project title, total — already exists in frozen state or config.
None of them needs a model, so `winch.compose.build_template_variables` is pure.
A model cannot hallucinate a price it is never asked to produce.

The LLM composer (`compose_reply`) exists only for free-form replies inside an
open 24-hour window, and its output passes `guard_freeform` before it can be
sent.

## 8. Models

**Azure OpenAI**, `gpt-4-1-mini`, via the existing deployment. Verified empirically before building
on it: chat, vision, and strict `json_schema` all work, and the extraction eval passes every trap
fixture (`scripts/probe_azure.py`, `scripts/eval_extraction.py`).

**Azure OpenAI accepts images, not PDFs**, so quotes are rasterised with `pdftoppm` first. This is a
pipeline dependency the Anthropic API would not have needed, and it is why `poppler-utils` is
installed in the Dockerfile.

Both Extractor and Triage go through one `LLMClient` wrapper, so a provider change lands in a single
place.

## 9. Open items

- WhatsApp template catalogue: submit for approval via `POST /v{version}/{WABA_ID}/message_templates`
  so the three templates live in the repo as versioned JSON (lead time, rejection risk).
- Meta Business Verification — required to move off the test number onto a real one and to raise
  messaging limits. Needs company documents, and **there is no registered entity yet**. Gates the
  *second* pilot, not the first — do not block on it.
- **v1 costs nothing.** Meta test number, existing GCP credits, no domain, no messaging provider.
- Project name is a placeholder. It appears nowhere customer-facing in v1 — the WhatsApp sender is
  the test number and there is no email — so it blocks nothing technical. Settle it before buying a
  domain, not before building.
- Bedrock config from user.
- Meta test number covers build + first real test free: 5 allow-listed recipients
  (contractor + test phone + one real customer). Identity is a test number, not the contractor's
  brand — a real customer reply is therefore weaker evidence than it will be later.
