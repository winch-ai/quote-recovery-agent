"""The event log. CONTRACT FILE — not delegated.

This is the instrument the demo exists to produce. Every state transition writes a
row; a transition that does not write one is invisible, and invisible transitions
are how we end up unable to answer the two questions the pilot is for.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class EventType(StrEnum):
    QUOTE_RECEIVED = "quote_received"
    PARSE_COMPLETED = "parse_completed"
    PARSE_FAILED = "parse_failed"
    CONTACT_REQUESTED = "contact_requested"
    CONTACT_COLLECTED = "contact_collected"
    APPROVAL_PROMPT_SENT = "approval_prompt_sent"
    APPROVAL_RECEIVED = "approval_received"
    GATE_PROMPT_SENT = "gate_prompt_sent"
    GATE_APPROVED = "gate_approved"
    GATE_HELD = "gate_held"
    TOUCHPOINT_SENT = "touchpoint_sent"
    DELIVERY_CONFIRMED = "delivery_confirmed"
    CHANNEL_UNAVAILABLE = "channel_unavailable"   # drives the build-email-or-not decision
    TOUCHPOINT_SEND_FAILED = "touchpoint_send_failed"  # generic (non-131026) send failure
    RELAYED_TO_CONTRACTOR = "relayed_to_contractor"
    CUSTOMER_REPLIED = "customer_replied"
    SEQUENCE_HALTED = "sequence_halted"
    QUOTE_CLOSED = "quote_closed"
    LLM_CALL = "llm_call"                          # token tracing


@runtime_checkable
class EventSink(Protocol):
    async def write(
        self,
        quote_id: str,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> None: ...
