"""LangGraph wiring. CONTRACT FILE — not delegated.

Three entry paths, one per kind of external event:

    INTAKE   ingest -> extract -> collect_contact* -> confirm* -> schedule
    TICK     compose -> gate* -> send -> (relay if unreachable)
    INBOUND  triage -> alert

(* = interrupt; execution parks in the checkpointer until a webhook resumes it.)

Edges here must agree with `winch.supervisor`, which is the single source of
truth for routing. `test_graph.py` asserts that agreement rather than trusting
this file to stay in step by hand.
"""
from __future__ import annotations

import functools
from enum import StrEnum

from langgraph.graph import END, START, StateGraph

from winch import nodes
from winch.nodes import Deps
from winch.state import ChannelState, GraphState, TouchpointStatus
from winch.supervisor import Node


class Entry(StrEnum):
    """Which chain an invocation runs. Chosen by the caller from the event."""

    INTAKE = "intake"
    TICK = "tick"
    INBOUND = "inbound"


_NODE_FUNCS = {
    Node.INGEST: nodes.ingest,
    Node.EXTRACT: nodes.extract,
    Node.COLLECT_CONTACT: nodes.collect_contact,
    Node.CONFIRM: nodes.confirm,
    Node.SCHEDULE: nodes.schedule,
    Node.COMPOSE: nodes.compose,
    Node.GATE: nodes.gate,
    Node.SEND: nodes.send,
    Node.RELAY: nodes.relay,
    Node.TRIAGE: nodes.triage,
    Node.ALERT: nodes.alert,
    Node.ARCHIVE: nodes.archive,
}


def _entry_for(state: GraphState) -> str:
    entry = state.get("_entry")
    if entry == Entry.TICK:
        return Node.COMPOSE.value
    if entry == Entry.INBOUND:
        return Node.TRIAGE.value
    return Node.INGEST.value


def _after_extract(state: GraphState) -> str:
    """A failed parse alerts the contractor rather than dying quietly."""
    return Node.COLLECT_CONTACT.value if state.get("draft") else Node.ALERT.value


def _after_gate(state: GraphState) -> str:
    """The gate is the only way into SEND. A hold ends the run without sending."""
    index = state.get("pending_gate_index")
    if index is None:
        return END
    tp = state["touchpoints"][index]
    return END if tp.status is TouchpointStatus.HELD else Node.SEND.value


def _after_send(state: GraphState) -> str:
    """Unreachable on WhatsApp falls back to human relay (v1 has no email)."""
    return (Node.RELAY.value
            if state.get("channel") is ChannelState.WHATSAPP_UNREACHABLE
            else END)


def build_graph(deps: Deps, checkpointer=None):
    """Compile the graph. `deps` is bound into every node via partial, which is
    what keeps nodes testable with fakes and no network."""
    builder = StateGraph(GraphState)
    for node, func in _NODE_FUNCS.items():
        builder.add_node(node.value, functools.partial(func, deps=deps))

    builder.add_conditional_edges(START, _entry_for, {
        Node.INGEST.value: Node.INGEST.value,
        Node.COMPOSE.value: Node.COMPOSE.value,
        Node.TRIAGE.value: Node.TRIAGE.value,
    })

    # INTAKE
    builder.add_edge(Node.INGEST.value, Node.EXTRACT.value)
    builder.add_conditional_edges(Node.EXTRACT.value, _after_extract, {
        Node.COLLECT_CONTACT.value: Node.COLLECT_CONTACT.value,
        Node.ALERT.value: Node.ALERT.value,
    })
    builder.add_edge(Node.COLLECT_CONTACT.value, Node.CONFIRM.value)
    builder.add_edge(Node.CONFIRM.value, Node.SCHEDULE.value)
    builder.add_edge(Node.SCHEDULE.value, END)

    # TICK — note there is no edge into SEND except from GATE.
    builder.add_edge(Node.COMPOSE.value, Node.GATE.value)
    builder.add_conditional_edges(Node.GATE.value, _after_gate, {
        Node.SEND.value: Node.SEND.value, END: END,
    })
    builder.add_conditional_edges(Node.SEND.value, _after_send, {
        Node.RELAY.value: Node.RELAY.value, END: END,
    })
    builder.add_edge(Node.RELAY.value, END)

    # INBOUND
    builder.add_edge(Node.TRIAGE.value, Node.ALERT.value)
    builder.add_edge(Node.ALERT.value, END)
    builder.add_edge(Node.ARCHIVE.value, END)

    return builder.compile(checkpointer=checkpointer)


def edges_into(graph_builder_edges, target: str) -> set[str]:
    """Source nodes with an edge into `target`. Used by the safety test."""
    return {src for src, dst in graph_builder_edges if dst == target}
