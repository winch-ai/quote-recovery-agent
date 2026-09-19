# Winch

Pre-code stage. The repo currently holds source research only — no application code yet.

## What this is

A proposed product: a **LangGraph-powered agent that lives in WhatsApp** and recovers dormant
high-ticket quotes for regional trades (shed builders, fencing, borehole drilling, ag fabrication).
Quotes run GBP 8k–60k / EUR 10k–70k / AUD 15k–110k. The hypothesis is that a large share die from
zero follow-up rather than from losing on price — treat the "35%" figure from v1 as **unvalidated**
and measure it in the first pilot instead of quoting it.

**Markets: UK & Ireland and Australia (primary), EU (staged), wider EMEA (opportunistic).
US and Canada are excluded** — trades there live on SMS and phone, so the WhatsApp control plane
does not exist.

**The thesis is non-adoption, not absence.** Jobber, ServiceTitan and ServiceM8 all ship quote
follow-up automation today. It sits switched off because configuring it is desk work a field owner
never does. Never pitch "nobody solves this." Our moat is the zero-onboarding surface, not the
capability.

Positioning: *"the virtual estimating assistant that lives in your WhatsApp"* — never "an AI agent
app". Indicative price GBP 250–350 / EUR 300–400 / AUD 500–650 per month plus setup; flat retainer
first, recovered-revenue share deferred (attribution is unwinnable early).

## Architecture the research settles on (dual-sided, do not collapse these)

- **Side A — contractor ↔ agent: WhatsApp.** Contractor forwards a PDF / photo of a handwritten
  work order / voice note. Agent parses, proposes a follow-up cadence, asks for one-tap approval.
  The contractor always initiates, which keeps this inside Meta's 24-hour service window.
- **Side B — agent ↔ end customer: WhatsApp primary, email fallback.** No SMS. The customer's
  number and email are collected **from the contractor** during confirmation (most quote PDFs have
  no mobile). There is no way to pre-check if a number is on WhatsApp — Meta removed the `contacts`
  endpoint — so resolution is **send-and-observe**: attempt WhatsApp, and on error `131026` or no
  `delivered` webhook inside the timeout, fall back to email permanently for that quote.
- **Cold touches must be pre-approved templates** with fixed button labels (max 3). The agent
  selects from a small approved catalogue; it cannot generate buttons per message. Once the customer
  taps or replies, the 24-hour window opens and free-form interactive messages become available.
- **No email channel in v1.** It needs a domain and a settled project name, for a path whose
  necessity is unmeasured. Instead the unreachable case is **human relay**: the agent hands the
  contractor the drafted text and asks if they want to send it themselves, and logs a
  `channel_unavailable` event. That event rate is the trigger condition for building email at all.
  The full email design (send from own domain → OAuth `gmail.send` only, never a read scope, app
  passwords rejected) is recorded in `docs/DESIGN.md` §3 so it need not be re-derived.

## Hard rules

1. **Never let the LLM generate or alter a dollar figure, line item, or delivery date.** Quote
   totals and scope are immutable literals carried through state. Hallucinated pricing sent to a
   customer is contractual/legal exposure for the contractor.
2. **Strict human-in-the-loop, front-loaded.** Nothing reaches a customer unapproved. But the
   approval is taken **once at ingestion** (covering the whole sequence), with a veto window before
   each send ("sending Thursday's nudge at 4pm — reply HOLD"), and a hard gate only on exceptions.
   A gate on every single message rebuilds the original fatigue and stalls silently. This is the
   core interaction decision and is pending validation (Hypothesis B).
3. **The customer never chats with an unconstrained LLM.** Any inbound customer reply that is a
   scope/price/technical question halts the sequence and alerts the contractor to take over.
4. **Timing is deterministic, not agentic.** Wait states, cadences and cron intervals live in the
   DB/queue with LangGraph checkpointers. The LLM does extraction, tone, and intent classification
   only — it never "decides" to wait three days.
5. **STOP/unsubscribe handling is mandatory** on both customer channels before any real pilot.

## Orchestration

**The supervisor is deterministic** — routing is `(state, event_type)`, a truth table. No LLM router.
This governs the quote lifecycle only: intake, confirm, gate, send, triage. LLM work there sits in
four specialists: Extractor (PDF/photo → schema), Composer (tone + template selection), Triage
(inbound intent), and a Channel Resolver that is itself mostly code.

**The Concierge (`winch/concierge.py`) is a separate, fifth specialist, and it is agentic on
purpose.** It answers a contractor's free-text message when nothing is pending on the deterministic
path — status questions like "what's pending" or "how many went out this week" — via a small
LangGraph tool-calling loop instead of the single hardcoded acknowledgement that path used to return
regardless of what was asked. It is scoped tightly and stays outside the truth table above: its
tools are read-only (list awaiting quotes, count recent event-log activity) and it has no way to
send to a customer, approve anything, change a price, schedule a touchpoint, or create a quote —
creating one still requires forwarding the actual document, because extraction depends on it. Hard
rules 1, 2 and 4 are about the quote lifecycle's supervisor and remain untouched; the Concierge was
deliberately kept out of that lifecycle rather than exempted from those rules.

## LangGraph shape

- Multimodal extraction sub-agent → strict Pydantic schema
  (`customer_name`, `customer_phone`, `project_title`, `quote_total`, `expiry_date`).
- State machine with checkpointed persistence — quote lifecycles run 2–30+ days across restarts.
- `interrupt()` HITL gate: **hard gate on every outbound for v1, deliberately.** The demo exists to
  measure whether the gate stalls; relaxing it later is trivial, observing the honest version fail
  happens once. The veto-window design in hard rule 2 is what we expect to end up at, not what v1
  ships.
- Triage sub-agent classifying inbound replies:
  `QUESTION_ON_TIMELINE | PRICE_OBJECTION | ACCEPTED | TECHNICAL_SCOPE_QUERY | UNSUBSCRIBE`.

## Reference

- `Evaluating Trade Quote Recovery Opportunity.pdf` — **v2.0, authoritative.** Corrected opportunity
  assessment: graded pain points, the non-adoption correction, architecture, risks, verdict.
- `Validation Plan - 5 Contractor Interviews.pdf` — the pre-build validation sequence.
- `docs/research/ARCHIVED-v1-original-research-gemini.txt` — superseded v1 source. Do not cite;
  it contains claims corrected in v2.0.

## Stack

Python · FastAPI · LangGraph (Postgres checkpointer) · Meta WhatsApp Cloud API · Claude via
**Amazon Bedrock** (`AnthropicBedrockMantle`, `anthropic.`-
prefixed model IDs) behind one `LLMClient` wrapper.

Deploy: Cloud Run (free managed HTTPS, Meta accepts `*.run.app`) · Cloud SQL Postgres ·
Cloud Scheduler → `/internal/tick` (drives due touchpoints) · Secret Manager. Runs on existing GCP
credits. **v1 costs nothing** — Meta test number, no domain, no messaging provider.

"Winch" is a placeholder name. It appears nowhere customer-facing in v1, so it blocks nothing.

## Status

Design agreed — see `docs/DESIGN.md`. Building the demo as a **measurement instrument**: one
contractor, real quotes, hardcoded. The validation plan's five interviews are replaced by putting a
working agent in front of real contractors; the two hypotheses are unchanged and are now answered
from the event log rather than from a script.

Lead-time item to start now: **WhatsApp template catalogue approval** — three templates live as JSON
in `templates/whatsapp/`, submitted with `scripts/submit_templates.py`. This does **not** require
Meta Business Verification; verification only gates moving off the test number, and there is no
registered entity yet, so do not block on it. Google OAuth verification is not needed for v1.
