"""Graph node implementations. CONTRACT-ADJACENT — the gate semantics here are
the human-in-the-loop guarantee, so this file is not delegated.

Every node takes (state, deps) and returns a partial state update. Nodes do no
I/O of their own; collaborators arrive through `Deps`, which is what makes the
whole graph testable with fakes and no network.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from langgraph.types import interrupt

from winch.compose import ContractorProfile, build_template_variables
from winch.events import EventSink, EventType
from winch.guards import freeze_quote
from winch.protocols import ChannelAdapter, LLMClient, TouchpointQueue
from winch.scheduler import build_sequence
from winch.state import (
    ChannelState,
    ContractorReplyIntent,
    GraphState,
    Intent,
    Quote,
    QuoteStatus,
    TouchpointStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class Deps:
    """Everything a node needs from the outside world."""

    llm: LLMClient
    channel: ChannelAdapter
    contractor_channel: ChannelAdapter   # WhatsApp to the contractor
    events: EventSink
    queue: TouchpointQueue
    contractor: ContractorProfile
    media_fetch: object | None = None    # async (media_id) -> (bytes, mime)


async def ingest(state: GraphState, deps: Deps) -> dict:
    """Fetch the forwarded media from Meta."""
    await deps.events.write(state["quote_id"], EventType.QUOTE_RECEIVED,
                            {"media_id": state.get("media_id")})
    media, mime = await deps.media_fetch(state["media_id"])  # type: ignore[misc]
    return {"_media": media, "media_mime": mime}


async def extract(state: GraphState, deps: Deps) -> dict:
    """Multimodal extraction into the strict draft schema."""
    try:
        draft = await deps.llm.extract_quote(state["_media"], state["media_mime"])
    except Exception as exc:
        logger.exception("extraction failed for %s", state["quote_id"])
        await deps.events.write(state["quote_id"], EventType.PARSE_FAILED,
                                {"error": type(exc).__name__})
        return {"status": QuoteStatus.DRAFT, "draft": None}

    await deps.events.write(state["quote_id"], EventType.PARSE_COMPLETED,
                            {"has_phone": draft.customer_phone is not None,
                             "total": draft.quote_total})
    return {"draft": draft, "status": QuoteStatus.AWAITING_CONTACT}


async def collect_contact(state: GraphState, deps: Deps) -> dict:
    """Ask the contractor for whatever the quote did not contain.

    Quote PDFs usually omit the customer's mobile. Asking is more reliable than
    parsing, and it is one message in a thread the contractor is already in.

    NOTE: this node COMMITS before the interrupt happens in await_contact.
    LangGraph re-executes a node from the top on every resume, so a side effect
    placed before interrupt() fires again on each one - the contractor would be
    prompted repeatedly and the event log would be wrong.
    """
    draft = state["draft"]
    missing = [f for f in ("customer_phone", "customer_name", "quote_total")
               if getattr(draft, f, None) is None]
    await deps.events.write(state["quote_id"], EventType.CONTACT_REQUESTED,
                            {"missing": missing})
    return {"_missing": missing}


async def await_contact(state: GraphState, deps: Deps) -> dict:
    """Interrupt only, and only if something is actually missing.

    Previously this always interrupted, even with nothing to ask - which
    produced a real, observed exchange where the contractor was told "reply
    anything to continue" for a quote that already had every field, replied
    with a confused "anything or", and the bot pointlessly waited on it. A
    quote with nothing missing has no reason to pause the graph at all.
    """
    if not state.get("_missing"):
        return {"status": QuoteStatus.AWAITING_APPROVAL}

    missing = state.get("_missing", [])
    answer = interrupt({
        "kind": "collect_contact",
        "missing": missing,
        "draft": state["draft"].model_dump() if state.get("draft") else None,
    })
    draft = state["draft"]
    allowed = set(type(draft).model_fields)
    clean = {k: v for k, v in (answer or {}).items() if k in allowed}

    # The contractor answers with the value itself ("07700 900412"), never
    # with {"customer_phone": "07700 900412"} - nothing upstream produces that
    # shape. _parse_contractor_reply wraps unrecognised text as {"text": ...},
    # which the field-name filter above silently drops, discarding the
    # contractor's answer entirely. When exactly one field was asked for,
    # plain text unambiguously means "here is that field's value".
    if not clean and answer and answer.get("text") and len(missing) == 1:
        clean = {missing[0]: answer["text"].strip()}

    await deps.events.write(state["quote_id"], EventType.CONTACT_COLLECTED,
                            {"provided": sorted(clean)})
    merged = draft.model_copy(update=clean) if clean else draft
    return {"draft": merged, "status": QuoteStatus.AWAITING_APPROVAL}


async def confirm(state: GraphState, deps: Deps) -> dict:
    """Send the approval prompt. Commits before await_confirm interrupts.

    approval_prompt_sent is timestamped HERE, once. It is the start of the
    time-to-approve measurement that the whole pilot exists to produce, so it
    must not be re-emitted on replay.
    """
    draft = state["draft"]
    await deps.events.write(state["quote_id"], EventType.APPROVAL_PROMPT_SENT,
                            {"total": draft.quote_total})
    return {}


async def await_confirm(state: GraphState, deps: Deps) -> dict:
    """Interrupt, classifying the reply with an LLM rather than keyword
    matching, and re-asking when it is genuinely unclear.

    Previously any reply other than the literal string "yes"/"approve"/etc.
    was treated as an implicit decline - a real, natural reply ("check in
    now") silently closed the quote outright, with no interrupt payload to
    notify on and therefore zero feedback to the contractor. Understanding
    what a contractor meant is exactly the kind of judgment call that belongs
    to an LLM worker, the same way nodes.triage already classifies customer
    replies rather than pattern-matching them - deps.llm.classify_contractor_
    reply is that worker for this decision. classify_contractor_reply is
    documented to never raise; UNCLEAR is its own failure-safe fallback.

    Looping interrupt() calls within one node is the documented LangGraph
    pattern for validating human input: each call is resumed independently
    from the checkpoint, so a prior satisfied call returns instantly on replay
    and only the newest one actually pauses. No side effects occur before a
    call, so replay is harmless - consistent with the rest of this file.
    """
    draft = state["draft"]
    payload = {
        "kind": "confirm_quote",
        "draft": draft.model_dump(),
        "cadence_days": [2, 5, 9],
    }
    while True:
        decision = interrupt(payload)

        if decision.get("edits"):
            return {"_decision": decision}

        reply_text = (decision or {}).get("text", "")
        intent = await deps.llm.classify_contractor_reply(reply_text)

        if intent is ContractorReplyIntent.APPROVED:
            await deps.events.write(state["quote_id"], EventType.APPROVAL_RECEIVED, {})
            quote = freeze_quote(Quote(
                quote_id=state["quote_id"],
                customer_name=draft.customer_name or "",
                customer_phone=draft.customer_phone,
                customer_email=draft.customer_email,
                project_title=draft.project_title,
                scope_summary=draft.scope_summary,
                quote_total=draft.quote_total or 0.0,
                currency=draft.currency or "GBP",
                expiry_date=draft.expiry_date,
            ))
            return {"quote": quote, "status": QuoteStatus.ACTIVE, "_decision": {}}

        if intent is ContractorReplyIntent.DECLINED:
            await deps.events.write(state["quote_id"], EventType.QUOTE_CLOSED,
                                    {"reason": "not_approved"})
            return {"status": QuoteStatus.CLOSED, "_decision": {}}

        # UNCLEAR - re-ask rather than guessing on the contractor's behalf.
        payload = {
            "kind": "confirm_quote_unclear",
            "draft": draft.model_dump(),
            "heard": reply_text,
        }


async def apply_edits(state: GraphState, deps: Deps) -> dict:
    """Merge the contractor's corrections into the draft, then re-confirm.

    Only fields present in the edit are touched; an edit is a correction, not a
    replacement, and silently dropping unmentioned fields would lose data the
    contractor never asked to change.
    """
    edits = (state.get("_decision") or {}).get("edits") or {}
    draft = state["draft"]
    allowed = set(type(draft).model_fields)
    clean = {k: v for k, v in edits.items() if k in allowed}
    if ignored := set(edits) - allowed:
        logger.warning("ignoring unknown edit fields for %s: %s",
                       state["quote_id"], sorted(ignored))
    await deps.events.write(state["quote_id"], EventType.CONTACT_COLLECTED,
                            {"edited": sorted(clean)})
    return {"draft": draft.model_copy(update=clean), "_decision": {}}


async def schedule(state: GraphState, deps: Deps) -> dict:
    """Write the durable plan. Timing is arithmetic, never a model decision."""
    touchpoints = build_sequence(datetime.now(timezone.utc), deps.contractor.timezone)
    await deps.queue.schedule(state["quote_id"], touchpoints)
    return {"touchpoints": touchpoints, "status": QuoteStatus.ACTIVE}


async def compose(state: GraphState, deps: Deps) -> dict:
    """Fill the approved template's variables. Pure code — no model involved."""
    index = state["pending_gate_index"]
    tp = state["touchpoints"][index]
    variables = build_template_variables(state["quote"], tp.template_name, deps.contractor)
    preview = f"[{tp.template_name}] " + " | ".join(variables)
    updated = list(state["touchpoints"])
    updated[index] = tp.model_copy(update={"body_preview": preview,
                                           "status": TouchpointStatus.AWAITING_GATE})
    return {"touchpoints": updated, "_variables": variables}


async def gate(state: GraphState, deps: Deps) -> dict:
    """Send the send-approval prompt. Commits before await_gate interrupts.

    gate_prompt_sent is the second half of the time-to-approve metric. Emitted
    once, here, for the same reason as approval_prompt_sent.
    """
    index = state["pending_gate_index"]
    tp = state["touchpoints"][index]
    await deps.events.write(state["quote_id"], EventType.GATE_PROMPT_SENT,
                            {"index": index, "template": tp.template_name})
    return {}


async def await_gate(state: GraphState, deps: Deps) -> dict:
    """THE human-in-the-loop gate. Nothing reaches a customer without passing here.

    v1 is deliberately a hard interrupt on every single outbound, even though the
    assessment argues a front-loaded approval with a veto window is what this
    audience will actually tolerate. The demo exists to MEASURE whether the gate
    stalls; relaxing it later is trivial, and observing the honest version fail
    happens once. See docs/DESIGN.md section 4.

    Re-asks on any reply that is not a clear yes or no, for the same reason as
    await_confirm: treating an unrecognised reply as an implicit HOLD used to
    silently park the touchpoint with zero feedback telling the contractor
    their reply was not understood. See await_confirm's docstring for why the
    interrupt-in-a-loop pattern used here is safe under LangGraph's replay.
    """
    index = state["pending_gate_index"]
    tp = state["touchpoints"][index]
    payload = {
        "kind": "approve_send",
        "index": index,
        "template": tp.template_name,
        "preview": tp.body_preview,
    }

    while True:
        decision = interrupt(payload)
        reply_text = (decision or {}).get("text", "")
        intent = await deps.llm.classify_contractor_reply(reply_text)

        if intent is ContractorReplyIntent.APPROVED:
            await deps.events.write(state["quote_id"], EventType.GATE_APPROVED,
                                    {"index": index})
            return {}

        if intent is ContractorReplyIntent.DECLINED:
            await deps.events.write(state["quote_id"], EventType.GATE_HELD,
                                    {"index": index})
            updated = list(state["touchpoints"])
            updated[index] = tp.model_copy(update={"status": TouchpointStatus.HELD})
            await deps.queue.mark(state["quote_id"], index, TouchpointStatus.HELD)
            return {"touchpoints": updated}

        # UNCLEAR - re-ask rather than silently holding.
        payload = {
            "kind": "approve_send_unclear",
            "index": index,
            "template": tp.template_name,
            "preview": tp.body_preview,
            "heard": reply_text,
        }


async def send(state: GraphState, deps: Deps) -> dict:
    """Deliver an approved template. Only reachable from gate — see supervisor."""
    index = state["pending_gate_index"]
    tp = state["touchpoints"][index]
    quote = state["quote"]
    result = await deps.channel.send_template(
        quote.customer_phone or "", tp.template_name, state["_variables"]
    )

    updated = list(state["touchpoints"])
    if result.unreachable:
        await deps.events.write(state["quote_id"], EventType.CHANNEL_UNAVAILABLE,
                                {"index": index, "error_code": result.error_code})
        updated[index] = tp.model_copy(update={"status": TouchpointStatus.FAILED})
        await deps.queue.mark(state["quote_id"], index, TouchpointStatus.FAILED)
        return {"touchpoints": updated, "channel": ChannelState.WHATSAPP_UNREACHABLE}

    if not result.ok:
        # A generic failure (wrong/unapproved template name, rate limit, auth,
        # network) previously marked FAILED and returned silently - no event
        # recorded why, no message to the contractor. From their side this is
        # indistinguishable from the message having actually gone out: they
        # approved a send, believed it happened, and heard nothing to say
        # otherwise. Exactly the trust-destroying silence this whole product
        # exists to prevent for THEIR customers, happening to the contractor
        # instead. Found in production: a 404 from an unapproved template name
        # (checkin_soft had never been submitted) produced total silence.
        await deps.events.write(state["quote_id"], EventType.TOUCHPOINT_SEND_FAILED,
                                {"index": index, "template": tp.template_name,
                                 "error_code": result.error_code})
        updated[index] = tp.model_copy(update={"status": TouchpointStatus.FAILED})
        await deps.queue.mark(state["quote_id"], index, TouchpointStatus.FAILED)
        try:
            await deps.contractor_channel.send_freeform(
                deps.contractor.wa_id,
                f"Couldn't send the {tp.template_name} message to "
                f"{quote.customer_name if quote else 'the customer'} - something "
                f"failed on my end (not a 'not on WhatsApp' issue, a real error). "
                f"I've logged it; you may need to check the template or try again.",
            )
        except Exception:
            logger.exception("also failed to notify contractor of send failure "
                             "for %s/%s", state["quote_id"], index)
        return {"touchpoints": updated}

    await deps.events.write(state["quote_id"], EventType.TOUCHPOINT_SENT,
                            {"index": index, "message_id": result.provider_message_id})
    updated[index] = tp.model_copy(update={
        "status": TouchpointStatus.SENT,
        "sent_at": datetime.now(timezone.utc),
        "provider_message_id": result.provider_message_id,
    })
    await deps.queue.mark(state["quote_id"], index, TouchpointStatus.SENT)
    return {"touchpoints": updated, "channel": ChannelState.WHATSAPP}


async def relay(state: GraphState, deps: Deps) -> dict:
    """v1 fallback when the customer is not reachable on WhatsApp.

    No email channel in v1 — see docs/DESIGN.md section 3. Hand the drafted text
    to the contractor and log it, because the rate of this event is what decides
    whether an email channel is worth building at all.
    """
    index = state["pending_gate_index"]
    tp = state["touchpoints"][index]
    await deps.contractor_channel.send_freeform(
        deps.contractor.wa_id,
        f"{state['quote'].customer_name} isn't reachable on WhatsApp. "
        f"Here's the follow-up I'd send - want to send it yourself?\n\n{tp.body_preview}",
    )
    await deps.events.write(state["quote_id"], EventType.RELAYED_TO_CONTRACTOR,
                            {"index": index})
    updated = list(state["touchpoints"])
    updated[index] = tp.model_copy(update={"status": TouchpointStatus.RELAYED})
    await deps.queue.mark(state["quote_id"], index, TouchpointStatus.RELAYED)
    return {"touchpoints": updated}


async def triage(state: GraphState, deps: Deps) -> dict:
    """Classify an inbound customer reply and halt the sequence.

    Every reply halts. The contractor decides what happens next — an automated
    sequence that keeps running after a customer has engaged is the exact
    behaviour this audience calls sleazy.
    """
    inbound = state["inbound"][-1]
    intent = await deps.llm.classify_intent(inbound.text)
    await deps.events.write(state["quote_id"], EventType.CUSTOMER_REPLIED,
                            {"intent": intent.value})

    messages = list(state["inbound"])
    messages[-1] = inbound.model_copy(update={"intent": intent})
    status = QuoteStatus.CLOSED if intent is Intent.UNSUBSCRIBE else QuoteStatus.HALTED
    return {"inbound": messages, "status": status}


async def alert(state: GraphState, deps: Deps) -> dict:
    """Hand control to the contractor with a tap-to-call link."""
    quote = state.get("quote")
    phone = quote.customer_phone if quote else None
    latest = state["inbound"][-1] if state.get("inbound") else None
    body = (f"{quote.customer_name if quote else 'Customer'} replied"
            + (f": \"{latest.text}\"" if latest else "")
            + (f"\n\nTap to call: tel:{phone}" if phone else ""))
    await deps.contractor_channel.send_freeform(deps.contractor.wa_id, body)
    await deps.events.write(state["quote_id"], EventType.SEQUENCE_HALTED, {})
    return {"status": QuoteStatus.HALTED}


async def archive(state: GraphState, deps: Deps) -> dict:
    await deps.events.write(state["quote_id"], EventType.QUOTE_CLOSED, {})
    for i, tp in enumerate(state.get("touchpoints", [])):
        if tp.status is TouchpointStatus.PENDING:
            await deps.queue.mark(state["quote_id"], i, TouchpointStatus.SKIPPED)
    return {"status": QuoteStatus.CLOSED}


def new_quote_id() -> str:
    return f"q_{uuid.uuid4().hex[:12]}"
