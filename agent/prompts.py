"""
The agent loop.

    mirror -> audit_original -> cluster -> [ per cluster:
                                              generate -> apply -> verify
                                              -> (repair, max 2) ]
           -> audit_final -> finalise

The loop is per cluster on purpose. Verifying one rule at a time means a
regression is attributable to a specific set of edits, which is both better
engineering and a much better story in the demo.

The feedback signal is real: axe re-run output plus the applier's rejection
reasons go straight back into the next prompt. That is what makes this agentic
rather than a chain of prompts.
"""

from __future__ import annotations

import logging
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

from agent.applier import apply_edits
from agent.bedrock_client import TokenUsage, request_edits
from agent.clustering import ViolationCluster, build_clusters
from agent.prompts import SYSTEM_PROMPT, build_fix_prompt, build_repair_prompt
from audit.runner import audit_html
from audit.scorer import compare, score_from_violations

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 2
MAX_CLUSTERS_PER_RUN = 8


class RunState(TypedDict, total=False):
    run_id: str
    page_url: str
    page_title: str

    original_html: str
    working_html: str

    original_violations: list[dict]
    final_violations: list[dict]

    clusters: list[ViolationCluster]
    cluster_index: int
    repair_attempts: int
    last_rejection_feedback: str
    last_newly_introduced: list[str]

    accepted_edits: list[dict]
    deferred_items: list[dict]
    unfixed_rules: list[str]
    cluster_reports: list[dict]

    score_before: int
    score_after: int
    token_usage: TokenUsage
    log: Annotated[list[str], lambda left, right: left + right]


def node_audit_original(state: RunState) -> RunState:
    result = audit_html(state["original_html"], take_screenshot=False)
    score = score_from_violations(result.violations)
    return {
        "original_violations": result.violations,
        "working_html": state["original_html"],
        "score_before": score,
        "log": [f"audit_original: {len(result.violations)} rules failing, score={score}"],
    }


def node_cluster(state: RunState) -> RunState:
    clusters = build_clusters(state["original_violations"])[:MAX_CLUSTERS_PER_RUN]
    return {
        "clusters": clusters,
        "cluster_index": 0,
        "repair_attempts": 0,
        "accepted_edits": [],
        "deferred_items": [],
        "unfixed_rules": [],
        "cluster_reports": [],
        "last_rejection_feedback": "",
        "last_newly_introduced": [],
        "token_usage": TokenUsage(),
        "log": [f"cluster: {len(clusters)} cluster(s) queued: "
                + ", ".join(cluster.rule_id for cluster in clusters)],
    }


def node_generate_and_apply(state: RunState) -> RunState:
    cluster = state["clusters"][state["cluster_index"]]
    is_repair = state["repair_attempts"] > 0

    if is_repair:
        remaining = _remaining_nodes_for_rule(state["working_html"], cluster.rule_id)
        prompt = build_repair_prompt(
            cluster=cluster,
            rejection_feedback=state.get("last_rejection_feedback", ""),
            remaining_violations_for_rule=remaining,
            newly_introduced_rules=state.get("last_newly_introduced", []),
        )
    else:
        prompt = build_fix_prompt(cluster, state.get("page_title", ""), state["page_url"])

    model_output = request_edits(SYSTEM_PROMPT, prompt)

    usage = state.get("token_usage") or TokenUsage()
    usage.add(model_output.usage)

    apply_result = apply_edits(state["working_html"], model_output.edits)

    return {
        "working_html": apply_result.html,
        "accepted_edits": state.get("accepted_edits", [])
        + [applied.edit for applied in apply_result.applied],
        "deferred_items": state.get("deferred_items", []) + model_output.deferred,
        "last_rejection_feedback": apply_result.rejection_feedback(),
        "token_usage": usage,
        "log": [
            f"{'repair' if is_repair else 'fix'} {cluster.rule_id}: "
            f"model proposed {len(model_output.edits)} edit(s), "
            f"{apply_result.applied_count} applied, "
            f"{len(apply_result.rejected)} rejected, "
            f"{len(model_output.deferred)} deferred"
        ],
    }


def _remaining_nodes_for_rule(html_text: str, rule_id: str) -> int:
    result = audit_html(html_text, take_screenshot=False)
    for violation in result.violations:
        if violation["id"] == rule_id:
            return violation.get("totalNodes", len(violation.get("nodes", [])))
    return 0


def node_verify(state: RunState) -> RunState:
    cluster = state["clusters"][state["cluster_index"]]
    result = audit_html(state["working_html"], take_screenshot=False)

    delta = compare(state["original_violations"], result.violations)
    rule_resolved = cluster.rule_id not in {v["id"] for v in result.violations}

    return {
        "final_violations": result.violations,
        "score_after": delta.score_after,
        "last_newly_introduced": delta.introduced_rules,
        "log": [
            f"verify {cluster.rule_id}: resolved={rule_resolved} "
            f"score={delta.score_before}->{delta.score_after} "
            f"introduced={delta.introduced_rules or 'none'}"
        ],
    }


def route_after_verify(state: RunState) -> str:
    cluster = state["clusters"][state["cluster_index"]]
    current_rule_ids = {violation["id"] for violation in state["final_violations"]}

    rule_resolved = cluster.rule_id not in current_rule_ids
    introduced_regressions = bool(state.get("last_newly_introduced"))
    had_rejections = bool(state.get("last_rejection_feedback"))

    if rule_resolved and not introduced_regressions:
        return "next_cluster"
    if state["repair_attempts"] >= MAX_REPAIR_ATTEMPTS:
        return "give_up"
    if introduced_regressions or had_rejections or not rule_resolved:
        return "repair"
    return "next_cluster"


def node_repair(state: RunState) -> RunState:
    return {"repair_attempts": state["repair_attempts"] + 1}


def node_give_up(state: RunState) -> RunState:
    cluster = state["clusters"][state["cluster_index"]]
    return {
        "unfixed_rules": state.get("unfixed_rules", []) + [cluster.rule_id],
        "cluster_reports": state.get("cluster_reports", [])
        + [{"rule": cluster.rule_id, "status": "unfixed",
            "attempts": state["repair_attempts"] + 1}],
        "log": [f"give_up {cluster.rule_id}: exhausted {MAX_REPAIR_ATTEMPTS} repair attempt(s)"],
    }


def node_next_cluster(state: RunState) -> RunState:
    cluster = state["clusters"][state["cluster_index"]]
    reports = state.get("cluster_reports", [])
    if not any(report["rule"] == cluster.rule_id for report in reports):
        reports = reports + [{"rule": cluster.rule_id, "status": "fixed",
                              "attempts": state["repair_attempts"] + 1}]
    return {
        "cluster_index": state["cluster_index"] + 1,
        "repair_attempts": 0,
        "last_rejection_feedback": "",
        "last_newly_introduced": [],
        "cluster_reports": reports,
    }


def route_after_next_cluster(state: RunState) -> str:
    if state["cluster_index"] >= len(state["clusters"]):
        return "finalise"
    return "generate"


def node_finalise(state: RunState) -> RunState:
    result = audit_html(state["working_html"], take_screenshot=False)
    delta = compare(state["original_violations"], result.violations)
    return {
        "final_violations": result.violations,
        "score_after": delta.score_after,
        "log": [f"finalise: {delta.summary_line()}"],
    }


def build_graph():
    graph = StateGraph(RunState)

    graph.add_node("audit_original", node_audit_original)
    graph.add_node("cluster", node_cluster)
    graph.add_node("generate", node_generate_and_apply)
    graph.add_node("verify", node_verify)
    graph.add_node("repair", node_repair)
    graph.add_node("give_up", node_give_up)
    graph.add_node("next_cluster", node_next_cluster)
    graph.add_node("finalise", node_finalise)

    graph.set_entry_point("audit_original")
    graph.add_edge("audit_original", "cluster")

    graph.add_conditional_edges(
        "cluster",
        lambda state: "generate" if state["clusters"] else "finalise",
        {"generate": "generate", "finalise": "finalise"},
    )

    graph.add_edge("generate", "verify")
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {"repair": "repair", "give_up": "give_up", "next_cluster": "next_cluster"},
    )
    graph.add_edge("repair", "generate")
    graph.add_edge("give_up", "next_cluster")

    graph.add_conditional_edges(
        "next_cluster",
        route_after_next_cluster,
        {"generate": "generate", "finalise": "finalise"},
    )
    graph.add_edge("finalise", END)

    return graph.compile()
