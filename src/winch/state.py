"""Graph state and the quote schema.

CONTRACT FILE — not delegated. Nothing in here may be changed by a worker task.

The central invariant: `Quote` carries the money and the scope, and is frozen once
the contractor approves it. Every downstream node reads those values; nothing
regenerates them. See guards.py for the enforcement.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class QuoteStatus(StrEnum):
    DRAFT = "DRAFT"                        # extracted, not yet confirmed
    AWAITING_CONTACT = "AWAITING_CONTACT"  # asking contractor for customer number/email
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ACTIVE = "ACTIVE"                      # sequence running
    HALTED = "HALTED"                      # customer replied; contractor must act
    CLOSED = "CLOSED"


class ChannelState(StrEnum):
    UNKNOWN = "UNKNOWN"
    WHATSAPP = "WHATSAPP"
    WHATSAPP_UNREACHABLE = "WHATSAPP_UNREACHABLE"  # v1: falls back to human relay


class TouchpointStatus(StrEnum):
    PENDING = "PENDING"
    AWAITING_GATE = "AWAITING_GATE"
    APPROVED = "APPROVED"
    HELD = "HELD"        # contractor vetoed this one
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    RELAYED = "RELAYED"  # v1 fallback: handed to contractor to send themselves
    SKIPPED = "SKIPPED"  # sequence halted before this fired


class Intent(StrEnum):
    QUESTION_ON_TIMELINE = "QUESTION_ON_TIMELINE"
    PRICE_OBJECTION = "PRICE_OBJECTION"
    ACCEPTED = "ACCEPTED"
    TECHNICAL_SCOPE_QUERY = "TECHNICAL_SCOPE_QUERY"
    UNSUBSCRIBE = "UNSUBSCRIBE"
    UNCLEAR = "UNCLEAR"


class ContractorReplyIntent(StrEnum):
    """What the contractor meant by a reply to a confirm/gate prompt.

    A separate enum from Intent, which describes a CUSTOMER's reaction to a
    quote follow-up - a genuinely different concept. Classified by an LLM
    (see LLMClient.classify_contractor_reply), the same way nodes.triage
    already classifies customer replies - literal keyword matching
    ("yes"/"approve"/"ok") was the actual defect that let a real reply
    ("check in now") fall through as an implicit decline with zero feedback.
    """

    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    UNCLEAR = "UNCLEAR"


class QuoteDraft(BaseModel):
    """Strict extraction target. The ONLY schema the Extractor may emit.

    Every field is optional except the project title, because real quotes omit
    things. A missing value MUST come back as None — a hallucinated phone number
    is far worse than a null, since the contractor is asked to fill gaps anyway.
    """

    model_config = ConfigDict(extra="forbid")

    customer_name: str | None = Field(None, description="Customer/client, NOT the contractor")
    customer_phone: str | None = Field(
        None,
        description=(
            "Customer's mobile as printed. Return null if absent. Do NOT return the "
            "contractor's or business's own phone number — quotes usually show that too."
        ),
    )
    customer_email: str | None = Field(None, description="Customer's email. Null if absent.")
    project_title: str = Field(description="Short description of the job, e.g. '2km stock fencing'")
    scope_summary: str | None = Field(None, description="One or two sentences of scope")
    quote_total: float | None = Field(
        None,
        description=(
            "The single headline total the customer is being asked to pay. Gross of tax "
            "where a gross figure is shown. Do NOT sum stage payments, and do NOT add "
            "optional extras that are offered as alternatives."
        ),
    )
    currency: Literal["GBP", "EUR", "AUD", "USD"] | None = None
    expiry_date: str | None = Field(None, description="ISO-8601 date, or null if not stated")


class Quote(BaseModel):
    """Confirmed quote. FROZEN after contractor approval — see guards.freeze_quote."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    quote_id: str
    customer_name: str
    customer_phone: str | None
    customer_email: str | None
    project_title: str
    scope_summary: str | None
    quote_total: float
    currency: Literal["GBP", "EUR", "AUD", "USD"]
    expiry_date: str | None
    frozen: bool = False


class Touchpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    template_name: Literal["checkin_soft", "schedule_nudge", "soft_close"]
    due_at: datetime            # absolute UTC, computed in code, never by a model
    status: TouchpointStatus = TouchpointStatus.PENDING
    body_preview: str | None = None
    sent_at: datetime | None = None
    provider_message_id: str | None = None


class InboundMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_message_id: str
    from_customer: bool
    text: str
    received_at: datetime
    intent: Intent | None = None


def _last_write_wins(_old, new):
    return new


class GraphState(TypedDict, total=False):
    """LangGraph state. Keys are reduced last-write-wins unless noted."""

    quote_id: str
    contractor_id: str
    status: Annotated[QuoteStatus, _last_write_wins]
    channel: Annotated[ChannelState, _last_write_wins]
    draft: QuoteDraft | None
    quote: Quote | None
    touchpoints: list[Touchpoint]
    inbound: list[InboundMessage]
    pending_gate_index: int | None
    media_id: str | None
    media_mime: str | None

    # Transient, per-invocation. Declared because LangGraph drops any key the
    # state schema does not name, silently.
    _entry: str
    _media: bytes
    _variables: list[str]
    _decision: dict
    _missing: list[str]
