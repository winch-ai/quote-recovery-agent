"""Spec for the routing truth table.

test_send_is_unreachable_without_the_gate is the load-bearing one: it proves the
human-in-the-loop property over the table rather than asserting it in prose.
Do not weaken it.
"""
import pytest

from winch.state import QuoteStatus
from winch.supervisor import (
    SENDING_NODES,
    Node,
    RoutingError,
    Trigger,
    defined_routes,
    route,
)


class TestSafetyProperties:
    def test_send_is_unreachable_without_the_gate(self):
        """No (status, trigger) pair reaches a sending node except via GATE_APPROVED.

        This is the whole human-in-the-loop guarantee. If a new route into SEND
        is added without a gate, this fails.
        """
        for (status, trigger), node in defined_routes().items():
            if node in SENDING_NODES:
                assert trigger is Trigger.GATE_APPROVED, (
                    f"({status}, {trigger}) reaches {node} without the gate"
                )

    def test_unsubscribe_terminates_from_every_state(self):
        for status in QuoteStatus:
            assert route(status, Trigger.UNSUBSCRIBE) is Node.ARCHIVE

    def test_halted_does_not_resume_on_a_timer(self):
        """A halted sequence must not start sending again because a timer fired."""
        with pytest.raises(RoutingError):
            route(QuoteStatus.HALTED, Trigger.TOUCHPOINT_DUE)

    def test_closed_quotes_accept_no_triggers(self):
        for trigger in Trigger:
            if trigger is Trigger.UNSUBSCRIBE:
                continue
            with pytest.raises(RoutingError):
                route(QuoteStatus.CLOSED, trigger)


class TestRouting:
    def test_media_received_starts_ingestion(self):
        assert route(QuoteStatus.DRAFT, Trigger.QUOTE_MEDIA_RECEIVED) is Node.INGEST

    def test_extraction_success_asks_for_contact_details(self):
        """Quote PDFs usually lack the customer's mobile; we ask rather than guess."""
        assert route(QuoteStatus.DRAFT, Trigger.EXTRACTION_OK) is Node.COLLECT_CONTACT

    def test_extraction_failure_alerts_rather_than_silently_dropping(self):
        assert route(QuoteStatus.DRAFT, Trigger.EXTRACTION_FAILED) is Node.ALERT

    def test_approval_schedules_the_sequence(self):
        assert route(QuoteStatus.AWAITING_APPROVAL, Trigger.CONTRACTOR_APPROVED) is Node.SCHEDULE

    def test_edit_loops_back_through_confirmation(self):
        assert route(QuoteStatus.AWAITING_APPROVAL, Trigger.CONTRACTOR_EDITED) is Node.APPLY_EDITS

    def test_due_touchpoint_composes_but_does_not_send(self):
        assert route(QuoteStatus.ACTIVE, Trigger.TOUCHPOINT_DUE) is Node.COMPOSE

    def test_gate_hold_parks_without_sending(self):
        assert route(QuoteStatus.ACTIVE, Trigger.GATE_HELD) is Node.IDLE

    def test_unreachable_customer_falls_back_to_human_relay(self):
        assert route(QuoteStatus.ACTIVE, Trigger.SEND_UNREACHABLE) is Node.RELAY

    def test_customer_reply_triages(self):
        assert route(QuoteStatus.ACTIVE, Trigger.CUSTOMER_REPLIED) is Node.TRIAGE

    def test_customer_reply_while_halted_still_triages(self):
        assert route(QuoteStatus.HALTED, Trigger.CUSTOMER_REPLIED) is Node.TRIAGE


class TestUndefinedPairs:
    def test_undefined_pair_raises_rather_than_defaulting(self):
        """A silent default is how a quote quietly stops being followed up."""
        with pytest.raises(RoutingError):
            route(QuoteStatus.DRAFT, Trigger.GATE_APPROVED)

    def test_error_names_the_pair(self):
        with pytest.raises(RoutingError, match="status=DRAFT"):
            route(QuoteStatus.DRAFT, Trigger.SEND_OK)

    def test_every_table_entry_is_reachable_by_route(self):
        for (status, trigger), expected in defined_routes().items():
            assert route(status, trigger) is expected
