"""Deterministic routing. CONTRACT FILE — not delegated.

The supervisor is a truth table, not a model. Routing here is fully determined by
`(status, trigger)` and has exactly one right answer for each pair, so putting an
LLM in front of it would add nondeterminism and cost to a solved problem.

The safety property this file exists to guarantee: **no path reaches SEND without
passing through GATE.** That is checked by a reachability test over the table
itself rather than by reading the code, so it cannot rot as the table grows.
"""
from __future__ import annotations

from enum import StrEnum

from winch.state import QuoteStatus


class Trigger(StrEnum):
    """Something that happened. Triggers come from webhooks, the tick, or a node."""

    QUOTE_MEDIA_RECEIVED = "quote_media_received"
    EXTRACTION_OK = "extraction_ok"
    EXTRACTION_FAILED = "extraction_failed"
    CONTACT_PROVIDED = "contact_provided"
    CONTRACTOR_APPROVED = "contractor_approved"
    CONTRACTOR_EDITED = "contractor_edited"
    CONTRACTOR_REJECTED = "contractor_rejected"
    TOUCHPOINT_DUE = "touchpoint_due"
    GATE_APPROVED = "gate_approved"
    GATE_HELD = "gate_held"
    SEND_OK = "send_ok"
    SEND_UNREACHABLE = "send_unreachable"
    CUSTOMER_REPLIED = "customer_replied"
    UNSUBSCRIBE = "unsubscribe"
    SEQUENCE_EXHAUSTED = "sequence_exhausted"


class Node(StrEnum):
    INGEST = "ingest"
    EXTRACT = "extract"
    COLLECT_CONTACT = "collect_contact"
    CONFIRM = "confirm"
    APPLY_EDITS = "apply_edits"
    SCHEDULE = "schedule"
    COMPOSE = "compose"
    GATE = "gate"                  # the strict human-in-the-loop interrupt
    SEND = "send"
    RELAY = "relay"                # v1 fallback: contractor sends it themselves
    TRIAGE = "triage"
    ALERT = "alert"
    ARCHIVE = "archive"
    IDLE = "idle"                  # nothing to do; graph parks here


class RoutingError(Exception):
    """A (status, trigger) pair with no defined route.

    Raised rather than defaulted. A silent fallback is how an unhandled state
    becomes a quote that quietly stops being followed up.
    """


# Any state may be terminated by an unsubscribe. Checked before the table.
_ALWAYS: dict[Trigger, Node] = {
    Trigger.UNSUBSCRIBE: Node.ARCHIVE,
}

# (status, trigger) -> next node. Exhaustive by construction; see tests.
_TABLE: dict[tuple[QuoteStatus, Trigger], Node] = {
    (QuoteStatus.DRAFT, Trigger.QUOTE_MEDIA_RECEIVED): Node.INGEST,
    (QuoteStatus.DRAFT, Trigger.EXTRACTION_OK): Node.COLLECT_CONTACT,
    (QuoteStatus.DRAFT, Trigger.EXTRACTION_FAILED): Node.ALERT,

    (QuoteStatus.AWAITING_CONTACT, Trigger.CONTACT_PROVIDED): Node.CONFIRM,
    (QuoteStatus.AWAITING_CONTACT, Trigger.CONTRACTOR_REJECTED): Node.ARCHIVE,

    (QuoteStatus.AWAITING_APPROVAL, Trigger.CONTRACTOR_APPROVED): Node.SCHEDULE,
    (QuoteStatus.AWAITING_APPROVAL, Trigger.CONTRACTOR_EDITED): Node.APPLY_EDITS,
    (QuoteStatus.AWAITING_APPROVAL, Trigger.CONTRACTOR_REJECTED): Node.ARCHIVE,

    (QuoteStatus.ACTIVE, Trigger.TOUCHPOINT_DUE): Node.COMPOSE,
    (QuoteStatus.ACTIVE, Trigger.GATE_APPROVED): Node.SEND,
    (QuoteStatus.ACTIVE, Trigger.GATE_HELD): Node.IDLE,
    (QuoteStatus.ACTIVE, Trigger.SEND_OK): Node.IDLE,
    (QuoteStatus.ACTIVE, Trigger.SEND_UNREACHABLE): Node.RELAY,
    (QuoteStatus.ACTIVE, Trigger.CUSTOMER_REPLIED): Node.TRIAGE,
    (QuoteStatus.ACTIVE, Trigger.SEQUENCE_EXHAUSTED): Node.ARCHIVE,
    (QuoteStatus.ACTIVE, Trigger.CONTRACTOR_REJECTED): Node.ARCHIVE,

    # Halted: the contractor has been handed control. Nothing automated resumes.
    # There is deliberately no TOUCHPOINT_DUE route out of HALTED — a halted
    # sequence must not start sending again on a timer.
    (QuoteStatus.HALTED, Trigger.CUSTOMER_REPLIED): Node.TRIAGE,
    (QuoteStatus.HALTED, Trigger.CONTRACTOR_REJECTED): Node.ARCHIVE,
}

# Nodes that may send to a customer. Reached only via GATE — enforced by test.
SENDING_NODES: frozenset[Node] = frozenset({Node.SEND})


def route(status: QuoteStatus, trigger: Trigger) -> Node:
    """Resolve the next node. Raises RoutingError on an undefined pair."""
    if trigger in _ALWAYS:
        return _ALWAYS[trigger]
    try:
        return _TABLE[(status, trigger)]
    except KeyError:
        raise RoutingError(
            f"no route for status={status} trigger={trigger}. "
            "Add it to _TABLE explicitly — do not add a default."
        ) from None


def defined_routes() -> dict[tuple[QuoteStatus, Trigger], Node]:
    """The full table, for tests and for documentation generation."""
    return dict(_TABLE)
