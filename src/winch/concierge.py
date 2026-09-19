"""The concierge: an LLM-with-tools agent for contractor free text that is not
answering a pending prompt.

This is deliberately NOT part of `winch.graph` / `winch.supervisor`. Those stay
a deterministic (status, trigger) truth table because a quote's lifecycle has
exactly one right route and nothing here should touch money, timing, or a
customer send - see supervisor.py's own docstring and CLAUDE.md hard rules
1, 2 and 4.

What this covers is the gap those rules were never meant to cover: a
contractor asking an open-ended question ("what's pending", "how many went
out this week", "can you create an invoice") when no interrupt is waiting for
their answer. Before this module existed, that path returned one hardcoded
string regardless of what was asked (see git history on app.py). This module
replaces that with a small LangGraph ReAct loop that can call read-only tools
and ground its answer in real data - and it is read-only BY CONSTRUCTION: no
tool here can send a message, approve anything, schedule a touchpoint, or
create a quote. Creating a quote still requires forwarding the document,
because extraction quality depends on the actual PDF/photo - that path is
unchanged and this module never touches it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from winch.compose import ContractorProfile
from winch.events import EventType
from winch.repository import PostgresReporting, PostgresThreadIndex

logger = logging.getLogger(__name__)

MAX_TOOL_TURNS = 4  # hard cap so a confused loop can't run away on cost/latency

FALLBACK_REPLY = (
    "Nothing outstanding right now - forward a quote whenever you're ready "
    "and I'll take it from there."
)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_pending_quotes",
            "description": (
                "List quotes currently waiting on a contractor answer "
                "(confirmation or approval). Use for questions like "
                "'what's pending', 'what's outstanding', 'what do you need "
                "from me'."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_quote",
            "description": (
                "Look up a specific quote by customer name or project keyword "
                "- e.g. 'Jamie Doyle', 'fencing'. Returns who it's for, the "
                "project, the total, whether it can still be changed, and (for "
                "an active quote) when its next check-in is actually "
                "scheduled. Use this before answering 'who was it addressed "
                "to', 'what's the status of X', 'what time will it go out', "
                "or a request to edit/change a quote."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Customer name or project keyword to search for.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_recent_activity",
            "description": (
                "Count quotes received and follow-up messages sent in the "
                "last N days. Use for 'how many' / 'this week' questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Lookback window in days, e.g. 7 for 'this week'.",
                    },
                },
                "required": ["days"],
            },
        },
    },
]


def _system_prompt(contractor: ContractorProfile) -> str:
    return (
        f"You are Winch, {contractor.first_name}'s virtual estimating assistant, "
        "replying inside their private WhatsApp control chat - this is never "
        "seen by a customer.\n\n"
        "You answer status questions about quotes already in the system. You "
        "have NO ability to message a customer, approve anything, change a "
        "price, or schedule a follow-up - those only happen through the "
        "existing confirm/approve flow, which you are not part of.\n\n"
        "If asked to edit or change a quote: look it up with find_quote first. "
        "A quote still awaiting approval is edited by replying directly to my "
        "original confirmation message for it (not here) - tell them that. A "
        "quote already approved is frozen and cannot be changed at all - "
        "changing the price on an approved quote is exactly what this system "
        "exists to prevent, since a wrong number sent to a customer is the "
        "contractor's legal exposure, not yours to fix by editing text. If the "
        "real quote changed, the fix is forwarding the corrected document as "
        "a new quote, never rewriting the number here.\n\n"
        "A quote is only created by the contractor forwarding the PDF or "
        "photo of it - that is the one way one enters the system, because "
        "extraction depends on the real document. You cannot 'create' or "
        "'make' one from a description or a request alone. If asked to, say "
        "so plainly and ask them to forward the document instead.\n\n"
        "You do not do invoicing, billing, or paperwork generation - only "
        "follow-up tracking for quotes already forwarded.\n\n"
        "Use your tools to ground any factual answer in real data. Never "
        "invent a number, name, or status. If nothing you have covers the "
        "question, say so plainly rather than guessing, and remind them they "
        "can forward a quote or ask what's pending.\n\n"
        "Keep replies short - this is WhatsApp, not email."
    )


class _ConciergeState(TypedDict):
    messages: Annotated[list[dict[str, Any]], lambda old, new: old + new]
    turns: int


async def _list_pending_quotes(threads: PostgresThreadIndex, graph: Any) -> str:
    quote_ids = await threads.list_awaiting()
    if not quote_ids:
        return "Nothing is currently awaiting a contractor answer."

    lines = []
    for quote_id in quote_ids[:20]:
        try:
            snapshot = await graph.aget_state({"configurable": {"thread_id": quote_id}})
            values = snapshot.values if snapshot is not None else {}
        except Exception:
            logger.exception("concierge: failed to read state for %s", quote_id)
            values = {}
        quote = values.get("quote")
        draft = values.get("draft")
        status = values.get("status")
        if quote is not None:
            lines.append(
                f"- {quote.customer_name} - {quote.project_title} "
                f"({quote.currency} {quote.quote_total:,.2f}) - {status}"
            )
        elif draft is not None:
            lines.append(f"- {draft.project_title} - awaiting your reply ({status})")
        else:
            lines.append(f"- quote {quote_id} - awaiting your reply ({status})")
    return "Awaiting your answer:\n" + "\n".join(lines)


_EDITABLE_NOTE = {
    "DRAFT": "still being set up - reply to my message about it to confirm details.",
    "AWAITING_CONTACT": "still waiting on the customer's contact details from you.",
    "AWAITING_APPROVAL": (
        "still awaiting your approval - reply directly to my confirmation "
        "message for this one (not this chat) to approve it or ask for a "
        "change; that reopens it for editing before anything is locked in."
    ),
    "ACTIVE": (
        "already approved and locked in - the price and scope can't be "
        "changed here. If it changed on paper, forward the corrected "
        "document and I'll treat it as a new quote."
    ),
    "HALTED": "paused because the customer replied - that needs your direct attention.",
    "CLOSED": "closed.",
}


async def _find_quote(threads: PostgresThreadIndex, reporting: PostgresReporting,
                       graph: Any, query: Any) -> str:
    query = str(query or "").strip().lower()
    if not query:
        return "Give me a customer name or project keyword to search for."

    quote_ids = await threads.list_open()
    matches = []
    for quote_id in quote_ids:
        try:
            snapshot = await graph.aget_state({"configurable": {"thread_id": quote_id}})
            values = snapshot.values if snapshot is not None else {}
        except Exception:
            logger.exception("concierge: failed to read state for %s", quote_id)
            continue
        quote = values.get("quote")
        draft = values.get("draft")
        status = str(values.get("status") or "")
        name = quote.customer_name if quote is not None else None
        project = (quote.project_title if quote is not None
                   else draft.project_title if draft is not None else None)
        haystack = " ".join(filter(None, [name, project])).lower()
        if query not in haystack:
            continue
        note = _EDITABLE_NOTE.get(status, "status unclear.")
        if status == "ACTIVE":
            try:
                due = await reporting.next_touchpoint_due(quote_id)
            except Exception:
                logger.exception("concierge: failed to read next touchpoint for %s", quote_id)
                due = None
            if due is not None:
                note += f" Next check-in scheduled for {due.isoformat()}."
        if quote is not None:
            matches.append(
                f"- {quote.customer_name} - {quote.project_title} "
                f"({quote.currency} {quote.quote_total:,.2f}). {note}"
            )
        elif draft is not None:
            matches.append(f"- {draft.project_title} (details not yet confirmed). {note}")
        if len(matches) >= 5:
            break

    if not matches:
        return f"No quote found matching '{query}'."
    return "\n".join(matches)


async def _count_recent_activity(reporting: PostgresReporting, days: Any) -> str:
    try:
        window = max(1, min(int(days), 90))
    except (TypeError, ValueError):
        window = 7
    since = datetime.now(timezone.utc) - timedelta(days=window)
    received = await reporting.count_events_since(EventType.QUOTE_RECEIVED, since)
    sent = await reporting.count_events_since(EventType.TOUCHPOINT_SENT, since)
    return (
        f"In the last {window} day(s): {received} quote(s) forwarded, "
        f"{sent} follow-up message(s) sent to customers."
    )


def _build_graph(threads: PostgresThreadIndex, reporting: PostgresReporting,
                  graph: Any, llm: Any) -> Any:
    async def agent(state: _ConciergeState) -> dict:
        if state["turns"] >= MAX_TOOL_TURNS:
            return {
                "messages": [{
                    "role": "assistant",
                    "content": (
                        "I'm not able to pin that down right now - ask me "
                        "again in a moment, or forward a quote if you're "
                        "ready to start one."
                    ),
                }],
                "turns": state["turns"] + 1,
            }
        assistant_msg = await llm.chat_with_tools(state["messages"], TOOLS)
        return {"messages": [assistant_msg], "turns": state["turns"] + 1}

    async def tools(state: _ConciergeState) -> dict:
        last = state["messages"][-1]
        results = []
        for call in last.get("tool_calls") or []:
            name = call.get("function", {}).get("name")
            raw_args = call.get("function", {}).get("arguments") or "{}"
            try:
                import json
                args = json.loads(raw_args)
            except Exception:
                args = {}
            try:
                if name == "list_pending_quotes":
                    content = await _list_pending_quotes(threads, graph)
                elif name == "find_quote":
                    content = await _find_quote(threads, reporting, graph, args.get("query"))
                elif name == "count_recent_activity":
                    content = await _count_recent_activity(reporting, args.get("days", 7))
                else:
                    content = f"Unknown tool: {name}"
            except Exception:
                logger.exception("concierge: tool %s failed", name)
                content = "That lookup failed - answer without it."
            results.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": content,
            })
        return {"messages": results}

    def route(state: _ConciergeState) -> str:
        last = state["messages"][-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            return "tools"
        return END

    builder = StateGraph(_ConciergeState)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    return builder.compile()


async def handle_general_message(
    *,
    text: str,
    contractor: ContractorProfile,
    threads: PostgresThreadIndex,
    reporting: PostgresReporting,
    graph: Any,
    llm: Any,
) -> str:
    """Answer a contractor's free-text message that has no pending interrupt.

    Never raises: on any failure this returns the same safe acknowledgement
    the dead branch used to hardcode, so a concierge bug degrades to the old
    (safe, if unhelpful) behaviour rather than crashing message handling.
    """
    try:
        compiled = _build_graph(threads, reporting, graph, llm)
        result = await compiled.ainvoke({
            "messages": [
                {"role": "system", "content": _system_prompt(contractor)},
                {"role": "user", "content": text},
            ],
            "turns": 0,
        })
        final = result["messages"][-1]
        content = final.get("content")
        return content.strip() if isinstance(content, str) and content.strip() else FALLBACK_REPLY
    except Exception:
        logger.exception("concierge failed to answer contractor message")
        return FALLBACK_REPLY
