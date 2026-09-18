"""End-to-end graph spec, with fakes. No network, no database.

test_no_edge_reaches_send_except_from_the_gate is the load-bearing one. The
supervisor test proves the property over the routing table; this proves it over
the compiled graph, which is what actually executes. Both must hold — a correct
table wired up wrongly still sends without a gate.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from winch.compose import ContractorProfile
from winch.events import EventType
from winch.graph import AWAIT_GATE, Entry, build_graph
from winch.nodes import Deps
from winch.protocols import SendResult
from winch.state import ChannelState, Intent, InboundMessage, QuoteDraft, QuoteStatus
from winch.supervisor import Node

DRAFT = QuoteDraft(
    customer_name="Mark Henderson", customer_phone="447700900412",
    customer_email=None, project_title="2km stock fencing",
    scope_summary="Supply and install", quote_total=24504.0,
    currency="GBP", expiry_date="2026-10-11",
)

# Missing the customer's phone - unlike DRAFT, this one legitimately needs the
# collect_contact interrupt (see nodes.await_contact, which otherwise skips it).
DRAFT_NO_PHONE = DRAFT.model_copy(update={"customer_phone": None})


class FakeLLM:
    def __init__(self, draft=DRAFT, intent=Intent.QUESTION_ON_TIMELINE, fail=False):
        self._draft, self._intent, self._fail = draft, intent, fail

    async def extract_quote(self, media, mime_type):
        if self._fail:
            raise RuntimeError("boom")
        return self._draft

    async def classify_intent(self, text):
        return self._intent

    async def compose_reply(self, quote, customer_message):
        return "ok"


class FakeChannel:
    name = "fake"

    def __init__(self, result=None):
        self.result = result or SendResult(ok=True, provider_message_id="wamid.1")
        self.templates, self.freeforms = [], []

    async def send_template(self, to, template_name, variables):
        self.templates.append((to, template_name, variables))
        return self.result

    async def send_freeform(self, to, body):
        self.freeforms.append((to, body))
        return SendResult(ok=True, provider_message_id="wamid.2")


class FakeEvents:
    def __init__(self):
        self.rows = []

    async def write(self, quote_id, event_type, payload=None, at=None):
        self.rows.append((quote_id, event_type, payload or {}))

    def types(self):
        return [t for _, t, _ in self.rows]


class FakeQueue:
    def __init__(self):
        self.scheduled, self.marks = [], []

    async def schedule(self, quote_id, touchpoints):
        self.scheduled.append((quote_id, touchpoints))

    async def claim_due(self, now, limit=20):
        return []

    async def mark(self, quote_id, index, status):
        self.marks.append((quote_id, index, status))


CONTRACTOR = ContractorProfile(
    contractor_id="c1", first_name="Dave", business_name="Henderson Fencing",
    wa_id="447700900001", timezone="Europe/London",
)


def make(llm=None, channel=None):
    ch = channel or FakeChannel()
    deps = Deps(
        llm=llm or FakeLLM(), channel=ch, contractor_channel=ch,
        events=FakeEvents(), queue=FakeQueue(), contractor=CONTRACTOR,
        media_fetch=_fake_media,
    )
    return deps, build_graph(deps, checkpointer=MemorySaver())


async def _fake_media(media_id):
    return b"%PDF-fake", "application/pdf"


def cfg(thread):
    return {"configurable": {"thread_id": thread}}


class TestSafetyProperties:
    def test_no_edge_reaches_send_except_from_the_gate(self):
        """Proven over the compiled graph, not just the routing table."""
        _, graph = make()
        edges = graph.get_graph().edges
        into_send = {e.source for e in edges if e.target == Node.SEND.value}
        assert into_send == {AWAIT_GATE}, (
            f"SEND is reachable from {into_send - {AWAIT_GATE}} without a gate"
        )

    def test_gate_is_always_preceded_by_compose(self):
        _, graph = make()
        edges = graph.get_graph().edges
        into_gate = {e.source for e in edges if e.target == Node.GATE.value}
        assert into_gate == {Node.COMPOSE.value}
        # and the interrupt node is only reachable from the gate that prompts
        into_await = {e.source for e in edges if e.target == AWAIT_GATE}
        assert into_await == {Node.GATE.value}

    def test_every_supervisor_node_except_idle_exists_in_the_graph(self):
        """Stops the table and the wiring drifting apart."""
        _, graph = make()
        present = set(graph.get_graph().nodes)
        expected = {n.value for n in Node if n is not Node.IDLE}
        assert expected <= present, f"missing nodes: {expected - present}"


class TestLangGraphGotchas:
    async def test_an_empty_resume_payload_is_ignored(self):
        """Command(resume={}) does NOT resume - the node interrupts again.

        Pinned because it is silent: the graph looks stuck rather than erroring,
        and app.py must therefore never construct an empty resume payload.
        """
        _, graph = make()
        state = {"quote_id": "q0", "media_id": "m1", "status": QuoteStatus.DRAFT,
                 "_entry": Entry.INTAKE}
        await graph.ainvoke(state, cfg("tg"))
        again = await graph.ainvoke(Command(resume={}), cfg("tg"))
        assert again["__interrupt__"][0].value["kind"] == "confirm_quote"

    async def test_an_empty_resume_payload_is_ignored_at_collect_contact(self):
        """Same gotcha, but at the earlier gate - only reachable when something
        is genuinely missing (see nodes.await_contact)."""
        _, graph = make(llm=FakeLLM(draft=DRAFT_NO_PHONE))
        state = {"quote_id": "q0b", "media_id": "m1", "status": QuoteStatus.DRAFT,
                 "_entry": Entry.INTAKE}
        await graph.ainvoke(state, cfg("tgb"))
        again = await graph.ainvoke(Command(resume={}), cfg("tgb"))
        assert again["__interrupt__"][0].value["kind"] == "collect_contact"

    async def test_side_effects_before_an_interrupt_are_not_duplicated(self):
        """LangGraph replays a node from the top on resume, so the prompt event
        lives in a node that commits BEFORE the interrupt node runs. If that
        split is undone, contact_requested fires on every resume and the
        time-to-approve metric is measured off a duplicated event."""
        deps, graph = make()
        state = {"quote_id": "q9", "media_id": "m1", "status": QuoteStatus.DRAFT,
                 "_entry": Entry.INTAKE}
        await graph.ainvoke(state, cfg("td"))
        await graph.ainvoke(Command(resume={}), cfg("td"))          # ignored resume
        await graph.ainvoke(Command(resume={}), cfg("td"))          # ignored again
        await graph.ainvoke(Command(resume={"customer_phone": "44770"}), cfg("td"))
        assert deps.events.types().count(EventType.CONTACT_REQUESTED) == 1
        assert deps.events.types().count(EventType.APPROVAL_PROMPT_SENT) == 1


class TestIntakeFlow:
    async def test_a_complete_draft_skips_straight_to_confirm(self):
        """A quote with nothing missing must not pause at collect_contact at
        all - see nodes.await_contact. Pausing to ask a no-op question was a
        real, observed exchange: the contractor was told 'reply anything to
        continue' for a quote that already had every field."""
        deps, graph = make()
        state = {"quote_id": "q1", "media_id": "m1", "status": QuoteStatus.DRAFT,
                 "_entry": Entry.INTAKE}

        result = await graph.ainvoke(state, cfg("t1"))
        assert result["__interrupt__"][0].value["kind"] == "confirm_quote"

        final = await graph.ainvoke(Command(resume={"approved": True}), cfg("t1"))
        assert final["status"] is QuoteStatus.ACTIVE
        assert final["quote"].frozen is True
        assert final["quote"].quote_total == 24504.0
        assert len(deps.queue.scheduled) == 1

    async def test_an_incomplete_draft_still_parks_at_collect_contact_first(self):
        deps, graph = make(llm=FakeLLM(draft=DRAFT_NO_PHONE))
        state = {"quote_id": "q1b", "media_id": "m1", "status": QuoteStatus.DRAFT,
                 "_entry": Entry.INTAKE}

        result = await graph.ainvoke(state, cfg("t1b"))
        assert result["__interrupt__"][0].value["kind"] == "collect_contact"
        assert result["__interrupt__"][0].value["missing"] == ["customer_phone"]

        result = await graph.ainvoke(Command(resume={"customer_phone": "447700900412"}), cfg("t1b"))
        assert result["__interrupt__"][0].value["kind"] == "confirm_quote"

        final = await graph.ainvoke(Command(resume={"approved": True}), cfg("t1b"))
        assert final["quote"].customer_phone == "447700900412"

    async def test_declining_closes_without_scheduling(self):
        deps, graph = make()
        state = {"quote_id": "q2", "media_id": "m1", "status": QuoteStatus.DRAFT,
                 "_entry": Entry.INTAKE}
        await graph.ainvoke(state, cfg("t2"))
        final = await graph.ainvoke(Command(resume={"approved": False}), cfg("t2"))
        assert final["status"] is QuoteStatus.CLOSED
        assert deps.channel.freeforms == [], (
            "an unsubscribe must not ping the contractor like a normal reply"
        )
        assert deps.queue.scheduled == []

    async def test_extraction_failure_alerts_instead_of_dying_quietly(self):
        deps, graph = make(llm=FakeLLM(fail=True))
        state = {"quote_id": "q3", "media_id": "m1", "status": QuoteStatus.DRAFT,
                 "_entry": Entry.INTAKE}
        final = await graph.ainvoke(state, cfg("t3"))
        assert EventType.PARSE_FAILED in deps.events.types()
        assert final["status"] is QuoteStatus.HALTED


class TestTickFlow:
    async def _approved_quote(self, graph, thread, qid):
        """DRAFT has every field, so the graph skips collect_contact entirely
        and interrupts at confirm_quote on the first invoke."""
        state = {"quote_id": qid, "media_id": "m1", "status": QuoteStatus.DRAFT,
                 "_entry": Entry.INTAKE}
        await graph.ainvoke(state, cfg(thread))
        return await graph.ainvoke(Command(resume={"approved": True}), cfg(thread))

    async def test_gate_hold_sends_nothing(self):
        """A held touchpoint must produce zero outbound messages."""
        deps, graph = make()
        await self._approved_quote(graph, "t4", "q4")
        await graph.ainvoke(
            {"_entry": Entry.TICK, "pending_gate_index": 0}, cfg("t4")
        )
        final = await graph.ainvoke(Command(resume={"approved": False}), cfg("t4"))
        assert deps.channel.templates == [], "a held gate still sent a message"
        assert EventType.GATE_HELD in deps.events.types()
        assert EventType.TOUCHPOINT_SENT not in deps.events.types()

    async def test_gate_approval_sends_the_template(self):
        deps, graph = make()
        await self._approved_quote(graph, "t5", "q5")
        await graph.ainvoke({"_entry": Entry.TICK, "pending_gate_index": 0}, cfg("t5"))
        await graph.ainvoke(Command(resume={"approved": True}), cfg("t5"))
        assert len(deps.channel.templates) == 1
        to, template, variables = deps.channel.templates[0]
        assert to == "447700900412"
        assert template == "checkin_soft"
        assert "GBP 24,504.00" in variables
        assert EventType.TOUCHPOINT_SENT in deps.events.types()

    async def test_unreachable_customer_falls_back_to_relay(self):
        """No email channel in v1; the contractor is handed the text instead."""
        channel = FakeChannel(SendResult(ok=False, error_code=131026, unreachable=True))
        deps, graph = make(channel=channel)
        await self._approved_quote(graph, "t6", "q6")
        await graph.ainvoke({"_entry": Entry.TICK, "pending_gate_index": 0}, cfg("t6"))
        final = await graph.ainvoke(Command(resume={"approved": True}), cfg("t6"))
        assert final["channel"] is ChannelState.WHATSAPP_UNREACHABLE
        assert EventType.CHANNEL_UNAVAILABLE in deps.events.types()
        assert EventType.RELAYED_TO_CONTRACTOR in deps.events.types()
        assert deps.channel.freeforms, "contractor was not given the text to send"


class TestInboundFlow:
    async def test_customer_reply_halts_and_alerts(self):
        """Every reply halts. A sequence that keeps running after the customer
        engages is the behaviour this audience calls sleazy."""
        deps, graph = make()
        inbound = InboundMessage(
            provider_message_id="wamid.in1", from_customer=True,
            text="Does that include site clearing?",
            received_at=datetime.now(timezone.utc),
        )
        final = await graph.ainvoke(
            {"quote_id": "q7", "status": QuoteStatus.ACTIVE, "_entry": Entry.INBOUND,
             "inbound": [inbound]},
            cfg("t7"),
        )
        assert final["status"] is QuoteStatus.HALTED
        assert final["inbound"][-1].intent is Intent.QUESTION_ON_TIMELINE
        assert EventType.CUSTOMER_REPLIED in deps.events.types()
        assert deps.channel.freeforms, "contractor was not alerted"

    async def test_unsubscribe_closes_the_quote(self):
        deps, graph = make(llm=FakeLLM(intent=Intent.UNSUBSCRIBE))
        inbound = InboundMessage(
            provider_message_id="wamid.in2", from_customer=True, text="STOP",
            received_at=datetime.now(timezone.utc),
        )
        final = await graph.ainvoke(
            {"quote_id": "q8", "status": QuoteStatus.ACTIVE, "_entry": Entry.INBOUND,
             "inbound": [inbound]},
            cfg("t8"),
        )
        assert final["status"] is QuoteStatus.CLOSED
        assert deps.channel.freeforms == [], (
            "an unsubscribe must not ping the contractor like a normal reply"
        )
