"""Tests for winch.concierge - the read-only tool-calling agent that answers
a contractor's free text when nothing is pending on the deterministic path.

Fully offline: the LLM, thread index, reporting and graph are all fakes, so
these exercise the tool-call loop and its safety boundary (read-only, no
send/approve/schedule capability) without a live DB or model.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from winch.compose import ContractorProfile
from winch.concierge import FALLBACK_REPLY, MAX_TOOL_TURNS, handle_general_message
from winch.state import Quote


def _profile() -> ContractorProfile:
    return ContractorProfile(
        contractor_id="pilot",
        first_name="Dave",
        business_name="Dave's Fencing",
        wa_id="447700900000",
        timezone="Europe/London",
    )


class _FakeThreads:
    def __init__(self, awaiting: list[str], open_: list[str] | None = None) -> None:
        self._awaiting = awaiting
        self._open = open_ if open_ is not None else list(awaiting)

    async def list_awaiting(self) -> list[str]:
        return list(self._awaiting)

    async def list_open(self) -> list[str]:
        return list(self._open)


class _FakeReporting:
    def __init__(self, counts: dict[str, int], next_due: dict[str, datetime] | None = None) -> None:
        self._counts = counts
        self._next_due = next_due or {}

    async def count_events_since(self, event_type, since) -> int:
        return self._counts.get(event_type.value, 0)

    async def next_touchpoint_due(self, quote_id: str):
        return self._next_due.get(quote_id)


class _FakeSnapshot:
    def __init__(self, values: dict) -> None:
        self.values = values


class _FakeGraph:
    def __init__(self, states: dict[str, dict]) -> None:
        self._states = states

    async def aget_state(self, config: dict) -> _FakeSnapshot:
        thread_id = config["configurable"]["thread_id"]
        return _FakeSnapshot(self._states.get(thread_id, {}))


def _tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _ScriptedLLM:
    """Replays a fixed sequence of assistant messages, one per turn."""

    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self.calls: list[list[dict]] = []

    async def chat_with_tools(self, messages, tools):
        self.calls.append(messages)
        return self._script.pop(0)


@pytest.mark.anyio
async def test_plain_answer_needs_no_tool_call(anyio_backend):
    llm = _ScriptedLLM([
        {"role": "assistant", "content": "I can't do that - only forwarding a quote starts one."},
    ])
    reply = await handle_general_message(
        text="can you create an invoice for me",
        contractor=_profile(),
        threads=_FakeThreads([]),
        reporting=_FakeReporting({}),
        graph=_FakeGraph({}),
        llm=llm,
    )
    assert "forward" in reply.lower() or "quote" in reply.lower()
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_tool_call_result_reaches_final_answer(anyio_backend):
    quote = Quote(
        quote_id="q1", customer_name="Jamie Doyle", customer_phone=None,
        customer_email=None, project_title="Perimeter fencing", scope_summary=None,
        quote_total=2340.0, currency="GBP", expiry_date=None,
    )
    llm = _ScriptedLLM([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("list_pending_quotes", {})],
        },
        {"role": "assistant", "content": "Jamie Doyle's fencing quote is still awaiting your reply."},
    ])
    reply = await handle_general_message(
        text="whats pending now",
        contractor=_profile(),
        threads=_FakeThreads(["q1"]),
        reporting=_FakeReporting({}),
        graph=_FakeGraph({"q1": {"quote": quote, "status": "AWAITING_APPROVAL"}}),
        llm=llm,
    )
    assert "Jamie Doyle" in reply
    # Second call's messages must include the tool result so the model was
    # actually grounded, not guessing.
    tool_messages = [m for m in llm.calls[1] if m.get("role") == "tool"]
    assert tool_messages and "Jamie Doyle" in tool_messages[0]["content"]


@pytest.mark.anyio
async def test_count_tool_uses_reporting(anyio_backend):
    llm = _ScriptedLLM([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("count_recent_activity", {"days": 7})],
        },
        {"role": "assistant", "content": "3 quotes forwarded this week."},
    ])
    reply = await handle_general_message(
        text="how many invoices this week",
        contractor=_profile(),
        threads=_FakeThreads([]),
        reporting=_FakeReporting({"quote_received": 3, "touchpoint_sent": 5}),
        graph=_FakeGraph({}),
        llm=llm,
    )
    assert "3" in reply


@pytest.mark.anyio
async def test_runaway_tool_loop_is_capped(anyio_backend):
    # The model keeps asking for tools forever; the loop must still terminate.
    script = [
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("list_pending_quotes", {})]}
        for _ in range(MAX_TOOL_TURNS + 2)
    ]
    llm = _ScriptedLLM(script)
    reply = await handle_general_message(
        text="whats pending",
        contractor=_profile(),
        threads=_FakeThreads([]),
        reporting=_FakeReporting({}),
        graph=_FakeGraph({}),
        llm=llm,
    )
    assert reply  # terminates with *some* string rather than hanging/crashing
    assert len(llm.calls) <= MAX_TOOL_TURNS + 1


@pytest.mark.anyio
async def test_find_quote_reports_frozen_status_not_editable(anyio_backend):
    quote = Quote(
        quote_id="q2", customer_name="Jamie Doyle", customer_phone=None,
        customer_email=None, project_title="Perimeter fencing", scope_summary=None,
        quote_total=2340.0, currency="GBP", expiry_date=None,
    )
    llm = _ScriptedLLM([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("find_quote", {"query": "Peter"})],
        },
        {"role": "assistant", "content": "Jamie Doyle's quote is approved and locked in - can't change the price here."},
    ])
    reply = await handle_general_message(
        text="can I change Peter's quote",
        contractor=_profile(),
        threads=_FakeThreads(awaiting=[], open_=["q2"]),
        reporting=_FakeReporting({}),
        graph=_FakeGraph({"q2": {"quote": quote, "status": "ACTIVE"}}),
        llm=llm,
    )
    assert "locked in" in reply.lower() or "can't change" in reply.lower()
    tool_messages = [m for m in llm.calls[1] if m.get("role") == "tool"]
    assert "locked in" in tool_messages[0]["content"].lower()


@pytest.mark.anyio
async def test_find_quote_active_reports_scheduled_touchpoint_time(anyio_backend):
    quote = Quote(
        quote_id="q3", customer_name="Jamie Doyle", customer_phone=None,
        customer_email=None, project_title="Perimeter fencing", scope_summary=None,
        quote_total=2340.0, currency="GBP", expiry_date=None,
    )
    due = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)
    llm = _ScriptedLLM([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("find_quote", {"query": "Peter"})],
        },
        {"role": "assistant", "content": "Peter's next check-in goes out at 14:00 UTC."},
    ])
    reply = await handle_general_message(
        text="what time will you check in with him today",
        contractor=_profile(),
        threads=_FakeThreads(awaiting=[], open_=["q3"]),
        reporting=_FakeReporting({}, next_due={"q3": due}),
        graph=_FakeGraph({"q3": {"quote": quote, "status": "ACTIVE"}}),
        llm=llm,
    )
    assert reply
    tool_messages = [m for m in llm.calls[1] if m.get("role") == "tool"]
    assert due.isoformat() in tool_messages[0]["content"]


@pytest.mark.anyio
async def test_find_quote_no_match(anyio_backend):
    llm = _ScriptedLLM([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("find_quote", {"query": "nobody"})],
        },
        {"role": "assistant", "content": "I couldn't find a quote matching that."},
    ])
    reply = await handle_general_message(
        text="what about the Smith quote",
        contractor=_profile(),
        threads=_FakeThreads(awaiting=[], open_=[]),
        reporting=_FakeReporting({}),
        graph=_FakeGraph({}),
        llm=llm,
    )
    assert reply
    tool_messages = [m for m in llm.calls[1] if m.get("role") == "tool"]
    assert "no quote found" in tool_messages[0]["content"].lower()


@pytest.mark.anyio
async def test_llm_failure_falls_back_to_safe_message(anyio_backend):
    class _BrokenLLM:
        async def chat_with_tools(self, messages, tools):
            raise RuntimeError("boom")

    reply = await handle_general_message(
        text="whats pending",
        contractor=_profile(),
        threads=_FakeThreads([]),
        reporting=_FakeReporting({}),
        graph=_FakeGraph({}),
        llm=_BrokenLLM(),
    )
    assert reply == FALLBACK_REPLY
