from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.services.graph.nodes.command_interpreter import command_interpreter_node
from app.services.graph.nodes.context_loader import context_loader_node
from app.services.graph.nodes.executor import execute_workers_node
from app.services.graph.nodes.guard import guard_node, route_after_guard
from app.services.graph.nodes.planner import planner_node
from app.services.graph.nodes.reply import clarification_node, reply_node
from app.services.graph.nodes.reviewer import reviewer_node, route_after_review
from app.services.graph.nodes.shortcut import route_after_shortcut, shortcut_node


def build_workspace_graph(workflow: Any, *, execute_workers: bool = False):
    builder = StateGraph(dict)
    builder.add_node("shortcut", shortcut_node(workflow))
    builder.add_node("command_interpreter", command_interpreter_node(workflow))
    builder.add_node("planner", planner_node())
    if execute_workers:
        builder.add_node("context_loader", context_loader_node(workflow))
        builder.add_node("guard", guard_node(workflow))
        builder.add_node("execute_workers", execute_workers_node(workflow))
        builder.add_node("reviewer", reviewer_node())
        builder.add_node("reply", reply_node())
        builder.add_node("clarification", clarification_node())

    builder.add_edge(START, "shortcut")
    builder.add_conditional_edges(
        "shortcut",
        route_after_shortcut,
        {"planned": "context_loader", "interpret": "command_interpreter"}
        if execute_workers
        else {"planned": "planner", "interpret": "command_interpreter"},
    )
    if not execute_workers:
        builder.add_edge("command_interpreter", "planner")
        builder.add_edge("planner", END)
        return builder.compile()

    builder.add_edge("command_interpreter", "context_loader")
    builder.add_edge("context_loader", "guard")
    builder.add_conditional_edges(
        "guard",
        route_after_guard,
        {
            "clarify": "clarification",
            "execute": "planner",
        },
    )
    builder.add_edge("planner", "execute_workers")
    builder.add_edge("execute_workers", "reviewer")
    builder.add_conditional_edges(
        "reviewer",
        route_after_review,
        {
            "clarify": "clarification",
            "reply": "reply",
        },
    )
    builder.add_edge("reply", END)
    builder.add_edge("clarification", END)
    return builder.compile()
