"""Interfaces. CONTRACT FILE — not delegated.

Workers implement against these. They do not change them.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from datetime import datetime

from winch.state import Intent, Quote, QuoteDraft, Touchpoint, TouchpointStatus


class SendResult(BaseModel):
    ok: bool
    provider_message_id: str | None = None
    error_code: int | None = None   # Meta error code, e.g. 131026 (undeliverable)
    unreachable: bool = False       # caller should transition to WHATSAPP_UNREACHABLE


@runtime_checkable
class ChannelAdapter(Protocol):
    """A way to reach the customer. v1 has exactly one: WhatsApp."""

    name: str

    async def send_template(
        self, to: str, template_name: str, variables: list[str]
    ) -> SendResult: ...

    async def send_freeform(self, to: str, body: str) -> SendResult:
        """Only legal inside an open 24-hour window. Implementations MUST refuse
        otherwise rather than silently falling back to a template."""
        ...


class DueTouchpoint(BaseModel):
    """A touchpoint the scheduler has claimed for sending."""

    quote_id: str
    touchpoint_index: int
    due_at: datetime


@runtime_checkable
class TouchpointQueue(Protocol):
    """Durable schedule of pending touchpoints.

    This is deliberately a database table, not in-graph state: send timing must
    survive restarts and must never be an LLM decision. See guards.next_business_window.
    """

    async def schedule(self, quote_id: str, touchpoints: list[Touchpoint]) -> None:
        """Persist the plan. Idempotent on (quote_id, index)."""
        ...

    async def claim_due(self, now: datetime, limit: int = 20) -> list[DueTouchpoint]:
        """Atomically claim touchpoints due at or before `now`.

        MUST be safe under concurrent callers — Cloud Run may run several
        instances and the tick fires on all of them. Two callers must never
        receive the same touchpoint, or the customer gets the message twice.
        """
        ...

    async def mark(self, quote_id: str, index: int, status: TouchpointStatus) -> None:
        """Record a terminal status for a claimed touchpoint."""
        ...


@runtime_checkable
class MessageDeduplicator(Protocol):
    """Meta redelivers webhooks on any non-2xx, and on its own schedule.

    Dedupe happens BEFORE state is touched, never after. `seen` must be atomic:
    it records the id and reports whether it was already present, in one step.
    A check-then-set pair races under concurrent delivery.
    """

    async def seen(self, provider_message_id: str) -> bool:
        """Claim the id. Return True if it had already been claimed."""
        ...

    async def release(self, provider_message_id: str) -> None:
        """Un-claim an id whose handler failed, so redelivery can retry it.

        Without this, a handler that raises loses the event permanently: Meta
        redelivers, the id is already claimed, and we silently drop it. The
        send path has its own idempotency guard, so retrying is the safer
        failure mode than losing a contractor's quote.
        """
        ...


@runtime_checkable
class LLMClient(Protocol):
    """Every model call goes through here, so tracing and cost live in one place."""

    async def extract_quote(self, media: bytes, mime_type: str) -> QuoteDraft: ...

    async def classify_intent(self, text: str) -> Intent: ...

    async def compose_touchpoint(
        self, quote: Quote, template_name: str, contractor_name: str, business_name: str
    ) -> list[str]:
        """Return the ordered template variables.

        MUST NOT return a monetary figure it generated; money comes from the frozen
        quote and is substituted by the caller. See guards.assert_no_stray_numbers.
        """
        ...
