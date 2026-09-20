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

Typing note: RunState is a total TypedDict -- every key exists from the moment
`initial_state()` builds it, so node bodies index it directly without the type
checker complaining about possibly-missing keys. Nodes return StateUpdate
(total=False), because a node only ever returns the slice of state it changed
and LangGraph merges that in.
"""

from __future__ import annotations

import json
import logging
import operator
import os
import time
from dataclasses import replace
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph

from agent.applier import apply_edits
from agent.bedrock_client import TokenUsage
from agent.model_provider import request_edits
from agent.clustering import (
    MAX_NODES_SENT_PER_CLUSTER,
    ViolationCluster,
    build_clusters,
)
from agent.prompts import SYSTEM_PROMPT, build_fix_prompt, build_repair_prompt
from audit.runner import audit_html
from audit.scorer import compare, score_from_violations

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 2
# A repair pass that is still reducing the failing-node count has earned another
# go. Two flat attempts is the right budget for a rule that fails on three nodes
# and far too few for one that fails on forty: the agent can only name so many
# selectors per call, so clearing a large rule is inherently iterative.
MAX_REPAIR_ATTEMPTS_WHILE_PROGRESSING = 5
MAX_CLUSTERS_PER_RUN = 8

# How long the fixing loop may run before it stops taking on new rules and goes
# to finalise with what it already has. This is not a safety net for a hung call
# -- model_provider bounds those -- it is the difference between a run that goes
# over time and still produces a before/after, and one the Lambda kills at 900s
# leaving nothing at all. Default leaves room for the final audit and the S3
# writes inside a 900s Lambda.
RUN_BUDGET_SECONDS = float(os.environ.get("RUN_BUDGET_SECONDS", "600"))

# Sentinel for "no verify has run for this rule yet". Larger than any real node
# count, so the first verify always registers as progress.
UNCOUNTED = 10 ** 9


class RunState(TypedDict):
    """Complete pipeline state. Every key is always present."""

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
    last_pass_was_empty: bool
    # Failing-node count for the current rule at the last two verifies, so the
    # router can tell "stuck" from "still chipping away".
    last_remaining_nodes: int
    prev_remaining_nodes: int
    cluster_start_html: str
    cluster_start_edit_count: int
    cluster_start_score: int

    accepted_edits: list[dict]
    applied_signatures: list[str]
    deferred_items: list[dict]
    unfixed_rules: list[str]
    deferred_rules: list[str]
    cluster_reports: list[dict]
    last_rule_status: str

    score_before: int
    score_after: int
    token_usage: TokenUsage

    # time.monotonic() value past which no new cluster is started. 0 disables.
    deadline: float

    log: Annotated[list[str], operator.add]


class StateUpdate(TypedDict, total=False):
    """The slice of RunState a node returns. Every key optional by design."""

    page_title: str
    working_html: str
    original_violations: list[dict]
    final_violations: list[dict]
    clusters: list[ViolationCluster]
    cluster_index: int
    repair_attempts: int
    last_rejection_feedback: str
    last_newly_introduced: list[str]
    last_pass_was_empty: bool
    last_remaining_nodes: int
    prev_remaining_nodes: int
    cluster_start_html: str
    cluster_start_edit_count: int
    cluster_start_score: int
    accepted_edits: list[dict]
    applied_signatures: list[str]
    deferred_items: list[dict]
    unfixed_rules: list[str]
    deferred_rules: list[str]
    cluster_reports: list[dict]
    last_rule_status: str
    score_before: int
    score_after: int
    token_usage: TokenUsage
    deadline: float
    log: list[str]


def initial_state(
    run_id: str,
    page_url: str,
    page_title: str,
    original_html: str,
    budget_seconds: float | None = None,
) -> RunState:
    """Build a complete RunState. Call this instead of writing a dict literal."""
    budget = RUN_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    return RunState(
        run_id=run_id,
        page_url=page_url,
        page_title=page_title,
        original_html=original_html,
        working_html=original_html,
        original_violations=[],
        final_violations=[],
        clusters=[],
        cluster_index=0,
        repair_attempts=0,
        last_rejection_feedback="",
        last_newly_introduced=[],
        last_pass_was_empty=False,
        last_remaining_nodes=UNCOUNTED,
        prev_remaining_nodes=UNCOUNTED,
        cluster_start_html=original_html,
        cluster_start_edit_count=0,
        cluster_start_score=0,
        accepted_edits=[],
        applied_signatures=[],
        deferred_items=[],
        unfixed_rules=[],
        deferred_rules=[],
        cluster_reports=[],
        last_rule_status="open",
        score_before=0,
        score_after=0,
        token_usage=TokenUsage(),
        deadline=(time.monotonic() + budget) if budget > 0 else 0.0,
        log=[],
    )


def out_of_time(state: RunState) -> bool:
    deadline = state.get("deadline", 0.0)
    return bool(deadline) and time.monotonic() >= deadline


def seconds_left(state: RunState) -> int:
    deadline = state.get("deadline", 0.0)
    return max(0, int(deadline - time.monotonic())) if deadline else 0


def node_audit_original(state: RunState) -> StateUpdate:
    result = audit_html(state["original_html"], take_screenshot=False)
    score = score_from_violations(result.violations)
    return {
        "original_violations": result.violations,
        "working_html": state["original_html"],
        "score_before": score,
        "score_after": score,
        "log": [f"audit_original: {len(result.violations)} rules failing, score={score}"],
    }


def node_cluster(state: RunState) -> StateUpdate:
    clusters = build_clusters(state["original_violations"])[:MAX_CLUSTERS_PER_RUN]
    queued = ", ".join(cluster.rule_id for cluster in clusters) or "(none)"
    return {
        "clusters": clusters,
        "cluster_index": 0,
        "repair_attempts": 0,
        "log": [f"cluster: {len(clusters)} cluster(s) queued: {queued}"],
    }


def edit_signature(edit: dict) -> str:
    """Identity of an edit, so a repair pass does not re-apply what already landed."""
    return "|".join(
        [
            edit.get("violation_id", ""),
            edit.get("selector", ""),
            edit.get("op", ""),
            json.dumps(edit.get("args", {}), sort_keys=True),
        ]
    )


def _remaining_for_rule(html_text: str, rule_id: str) -> tuple[int, list[dict]]:
    """How many nodes still fail this rule, AND which ones.

    Returning only the count was the single most damaging bug in this loop. The
    repair prompt was built from the ORIGINAL sample nodes, most of which the
    first pass had already fixed -- so every repair attempt was shown work that
    was already done and was blind to the nodes that still failed. On a page
    where a rule fails on forty nodes and twelve are sampled, that made repair
    passes worthless and the score never moved.
    """
    result = audit_html(html_text, take_screenshot=False)
    for violation in result.violations:
        if violation["id"] == rule_id:
            nodes = violation.get("nodes", [])
            return violation.get("totalNodes", len(nodes)), nodes
    return 0, []


def _cluster_for_repair(cluster: ViolationCluster, nodes: list[dict], remaining: int) -> ViolationCluster:
    """The same rule, re-pointed at the nodes that are still failing."""
    return replace(
        cluster,
        total_nodes=remaining,
        sample_nodes=nodes[:MAX_NODES_SENT_PER_CLUSTER],
    )


def node_generate_and_apply(state: RunState) -> StateUpdate:
    cluster = state["clusters"][state["cluster_index"]]
    is_repair = state["repair_attempts"] > 0

    # Snapshot before touching this rule, so a cluster that ends up making the
    # page worse can be undone rather than shipped.
    snapshot: StateUpdate = {}
    if not is_repair:
        snapshot = {
            "cluster_start_html": state["working_html"],
            "cluster_start_edit_count": len(state["accepted_edits"]),
            "cluster_start_score": state["score_after"],
        }

    if is_repair:
        remaining, remaining_nodes = _remaining_for_rule(
            state["working_html"], cluster.rule_id
        )
        # Re-point the cluster at what is STILL broken before prompting.
        cluster = _cluster_for_repair(cluster, remaining_nodes, remaining)
        prompt = build_repair_prompt(
            cluster=cluster,
            rejection_feedback=state["last_rejection_feedback"],
            remaining_violations_for_rule=remaining,
            newly_introduced_rules=state["last_newly_introduced"],
        )
    else:
        prompt = build_fix_prompt(cluster, state["page_title"], state["page_url"])

    model_output = request_edits(
        SYSTEM_PROMPT,
        prompt,
        rule_id=cluster.rule_id,
        nodes=cluster.sample_nodes,
    )

    usage = state["token_usage"]
    usage.add(model_output.usage)

    def with_snippets(applied) -> dict:
        """The edit plus the markup it actually changed, for the diff view."""
        return {
            **applied.edit,
            "matched_nodes": applied.matched_nodes,
            "before_snippets": applied.before_snippets,
            "after_snippets": applied.after_snippets,
        }

    already_applied = set(state["applied_signatures"])
    fresh_edits = [
        edit for edit in model_output.edits if edit_signature(edit) not in already_applied
    ]
    duplicate_count = len(model_output.edits) - len(fresh_edits)

    apply_result = apply_edits(state["working_html"], fresh_edits)
    pass_label = "repair" if is_repair else "fix"

    known_deferred = {
        (item.get("violation_id"), item.get("selector")) for item in state["deferred_items"]
    }
    fresh_deferred = [
        item
        for item in model_output.deferred
        if (item.get("violation_id"), item.get("selector")) not in known_deferred
    ]

    duplicate_note = f", {duplicate_count} already applied" if duplicate_count else ""

    # Name the backend on every pass. With a fallback chain, "which model wrote
    # these edits" is not answerable from configuration, and a demo that quietly
    # dropped to the deterministic rules would be misrepresenting itself.
    provider_note = f" [{model_output.provider}]" if model_output.provider else ""

    return {
        **snapshot,
        "working_html": apply_result.html,
        "accepted_edits": state["accepted_edits"]
        + [with_snippets(applied) for applied in apply_result.applied],
        "applied_signatures": state["applied_signatures"]
        + [edit_signature(applied.edit) for applied in apply_result.applied],
        "deferred_items": state["deferred_items"] + fresh_deferred,
        "last_rejection_feedback": apply_result.rejection_feedback(),
        # "Made no progress" covers two cases that both mean another identical
        # prompt is wasted money: the model returned nothing, or it returned
        # only edits that had already landed.
        "last_pass_was_empty": (
            not apply_result.applied
            and not fresh_deferred
        ),
        "token_usage": usage,
        "log": [
            f"{pass_label} {cluster.rule_id}{provider_note}: "
            f"model proposed {len(model_output.edits)} edit(s), "
            f"{apply_result.applied_count} applied, "
            f"{len(apply_result.rejected)} rejected, "
            f"{len(fresh_deferred)} deferred{duplicate_note}"
        ],
    }


def classify_rule_status(
    rule_id: str,
    violations: list[dict],
    deferred_items: list[dict],
) -> tuple[str, int]:
    """resolved | deferred | open, plus how many nodes still fail.

    'deferred' means every node still failing this rule is one the model
    explicitly declined to guess at. That is the correct outcome for things like
    alt text on a photograph, and it must not be retried as if it were a failure.
    """
    remaining = next(
        (violation for violation in violations if violation["id"] == rule_id), None
    )
    if remaining is None:
        return "resolved", 0

    remaining_targets = {
        node["target"][0]
        for node in remaining.get("nodes", [])
        if node.get("target")
    }
    deferred_targets = {
        item.get("selector")
        for item in deferred_items
        if item.get("violation_id") == rule_id
    }

    node_count = remaining.get("totalNodes", len(remaining.get("nodes", [])))
    if remaining_targets and remaining_targets.issubset(deferred_targets):
        return "deferred", node_count
    return "open", node_count


def node_verify(state: RunState) -> StateUpdate:
    cluster = state["clusters"][state["cluster_index"]]
    result = audit_html(state["working_html"], take_screenshot=False)

    delta = compare(state["original_violations"], result.violations)
    rule_status, remaining_nodes = classify_rule_status(
        cluster.rule_id, result.violations, state["deferred_items"]
    )
    introduced_label = ", ".join(delta.introduced_rules) or "none"

    return {
        "final_violations": result.violations,
        "score_after": delta.score_after,
        "last_newly_introduced": delta.introduced_rules,
        "last_rule_status": rule_status,
        "prev_remaining_nodes": state["last_remaining_nodes"],
        "last_remaining_nodes": remaining_nodes,
        "log": [
            f"verify {cluster.rule_id}: {rule_status} ({remaining_nodes} node(s) left) "
            f"score={delta.score_before}->{delta.score_after} "
            f"introduced={introduced_label}"
        ],
    }


def route_after_verify(state: RunState) -> str:
    rule_status = state["last_rule_status"]

    # A cluster "regressed" if it introduced a new rule OR lowered the score.
    # The second case matters: making an existing rule fail on more nodes does
    # not introduce a new rule id, but it still makes the page worse.
    introduced_regressions = (
        bool(state["last_newly_introduced"])
        or state["score_after"] < state["cluster_start_score"]
    )

    if rule_status in {"resolved", "deferred"} and not introduced_regressions:
        return "next_cluster"

    # Still reducing the failing-node count? Keep going, up to a higher ceiling.
    # Flat or rising means another identical prompt will not help.
    progressing = state["last_remaining_nodes"] < state["prev_remaining_nodes"]
    limit = MAX_REPAIR_ATTEMPTS_WHILE_PROGRESSING if progressing else MAX_REPAIR_ATTEMPTS

    if state["repair_attempts"] >= limit:
        return "give_up"

    # Out of budget: do not start another model call. give_up still rolls back
    # anything this cluster broke, so stopping here is safe, not just quick.
    if out_of_time(state):
        return "give_up"

    # The pass changed nothing -- the model either returned nothing, or only
    # repeated edits that had already landed. Another identical prompt returns
    # the same thing; spending a call to prove it is waste.
    if state["last_pass_was_empty"]:
        return "give_up"

    return "repair"


def node_repair(state: RunState) -> StateUpdate:
    return {"repair_attempts": state["repair_attempts"] + 1}


def node_give_up(state: RunState) -> StateUpdate:
    """Stop working this rule. Revert if the attempt left the page worse.

    A run must never be able to lower the score. If this cluster introduced a
    violation that repair could not clear, every edit it made is rolled back and
    the rule is reported as unfixed -- which is honest, and strictly better for
    the user than shipping a page we damaged.
    """
    cluster = state["clusters"][state["cluster_index"]]
    introduced = state["last_newly_introduced"]
    score_dropped = state["score_after"] < state["cluster_start_score"]

    update: StateUpdate = {
        "unfixed_rules": state["unfixed_rules"] + [cluster.rule_id],
    }

    if introduced or score_dropped:
        kept_edits = state["accepted_edits"][: state["cluster_start_edit_count"]]
        reverted_count = len(state["accepted_edits"]) - len(kept_edits)

        update.update(
            {
                "working_html": state["cluster_start_html"],
                "accepted_edits": kept_edits,
                "applied_signatures": [
                    edit_signature(edit) for edit in kept_edits
                ],
                "last_newly_introduced": [],
                "score_after": state["cluster_start_score"],
                "cluster_reports": state["cluster_reports"]
                + [
                    {
                        "rule": cluster.rule_id,
                        "status": "reverted",
                        "attempts": state["repair_attempts"] + 1,
                        "introduced": introduced,
                        "revertedEdits": reverted_count,
                    }
                ],
                "log": [
                    f"revert {cluster.rule_id}: "
                    + (
                        f"introduced {', '.join(introduced)}"
                        if introduced
                        else f"score fell {state['cluster_start_score']}->{state['score_after']}"
                    )
                    + f"; rolled back {reverted_count} edit(s)"
                ],
            }
        )
        return update

    update.update(
        {
            "cluster_reports": state["cluster_reports"]
            + [
                {
                    "rule": cluster.rule_id,
                    "status": "unfixed",
                    "attempts": state["repair_attempts"] + 1,
                }
            ],
            "log": [
                f"give_up {cluster.rule_id}: exhausted {MAX_REPAIR_ATTEMPTS} repair attempt(s)"
            ],
        }
    )
    return update


def node_next_cluster(state: RunState) -> StateUpdate:
    """Close out this rule, then stop early if the run is out of budget.

    Stopping early is a real outcome, not an error: every cluster that already
    finished is verified and kept, so the report still shows a genuine
    before/after. The alternative -- starting a rule we cannot finish -- risks
    the Lambda being killed mid-cluster, which produces nothing at all.
    """
    update = _close_cluster(state)

    clusters = state["clusters"]
    next_index = update.get("cluster_index", state["cluster_index"])
    remaining = len(clusters) - next_index

    if remaining > 0 and out_of_time(state):
        skipped = [cluster.rule_id for cluster in clusters[next_index:]]
        update["cluster_index"] = len(clusters)
        update["unfixed_rules"] = list(update.get("unfixed_rules", state["unfixed_rules"])) + skipped
        update["log"] = list(update.get("log", [])) + [
            f"budget: time limit reached after {next_index} of {len(clusters)} rule(s); "
            f"finalising with what has landed (skipped: {', '.join(skipped)})"
        ]

    return update


def _close_cluster(state: RunState) -> StateUpdate:
    """Close out this rule and move on -- keeping its edits only if they helped.

    A cluster that resolved its own rule but left the page scoring no better is
    not a win, it is churn. Reverting it keeps the run monotonic: the score after
    every cluster is greater than or equal to the score before it.
    """
    cluster = state["clusters"][state["cluster_index"]]
    reports = state["cluster_reports"]

    made_things_worse = state["score_after"] < state["cluster_start_score"]
    if made_things_worse:
        kept_edits = state["accepted_edits"][: state["cluster_start_edit_count"]]
        reverted_count = len(state["accepted_edits"]) - len(kept_edits)
        return {
            "cluster_index": state["cluster_index"] + 1,
            "repair_attempts": 0,
            "last_rejection_feedback": "",
            "last_newly_introduced": [],
            "last_pass_was_empty": False,
            "last_rule_status": "open",
            "last_remaining_nodes": UNCOUNTED,
            "prev_remaining_nodes": UNCOUNTED,
            "working_html": state["cluster_start_html"],
            "accepted_edits": kept_edits,
            "applied_signatures": [edit_signature(edit) for edit in kept_edits],
            "score_after": state["cluster_start_score"],
            "unfixed_rules": state["unfixed_rules"] + [cluster.rule_id],
            "cluster_reports": reports
            + [
                {
                    "rule": cluster.rule_id,
                    "status": "reverted",
                    "attempts": state["repair_attempts"] + 1,
                    "reason": "score did not improve",
                    "revertedEdits": reverted_count,
                }
            ],
            "log": [
                f"revert {cluster.rule_id}: score would have gone "
                f"{state['cluster_start_score']} -> {state['score_after']}; "
                f"rolled back {reverted_count} edit(s)"
            ],
        }

    status = "deferred" if state["last_rule_status"] == "deferred" else "fixed"

    already_reported = any(report["rule"] == cluster.rule_id for report in reports)
    if not already_reported:
        reports = reports + [
            {
                "rule": cluster.rule_id,
                "status": status,
                "attempts": state["repair_attempts"] + 1,
            }
        ]

    deferred_rules = state["deferred_rules"]
    if status == "deferred" and cluster.rule_id not in deferred_rules:
        deferred_rules = deferred_rules + [cluster.rule_id]

    return {
        "cluster_index": state["cluster_index"] + 1,
        "repair_attempts": 0,
        "last_rejection_feedback": "",
        "last_newly_introduced": [],
        "last_pass_was_empty": False,
        "last_rule_status": "open",
        "last_remaining_nodes": UNCOUNTED,
        "prev_remaining_nodes": UNCOUNTED,
        "cluster_reports": reports,
        "deferred_rules": deferred_rules,
    }


def route_after_cluster(state: RunState) -> str:
    return "generate" if state["clusters"] else "finalise"


def route_after_next_cluster(state: RunState) -> str:
    if state["cluster_index"] >= len(state["clusters"]):
        return "finalise"
    return "generate"


def node_finalise(state: RunState) -> StateUpdate:
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
        route_after_cluster,
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


# """
# The agent loop.

#     mirror -> audit_original -> cluster -> [ per cluster:
#                                               generate -> apply -> verify
#                                               -> (repair, max 2) ]
#            -> audit_final -> finalise

# The loop is per cluster on purpose. Verifying one rule at a time means a
# regression is attributable to a specific set of edits, which is both better
# engineering and a much better story in the demo.

# The feedback signal is real: axe re-run output plus the applier's rejection
# reasons go straight back into the next prompt. That is what makes this agentic
# rather than a chain of prompts.

# Typing note: RunState is a total TypedDict -- every key exists from the moment
# `initial_state()` builds it, so node bodies index it directly without the type
# checker complaining about possibly-missing keys. Nodes return StateUpdate
# (total=False), because a node only ever returns the slice of state it changed
# and LangGraph merges that in.
# """

# from __future__ import annotations

# import json
# import logging
# import operator
# import os
# import time
# from typing import Annotated, TypedDict

# from langgraph.graph import END, StateGraph

# from agent.applier import apply_edits
# from agent.bedrock_client import TokenUsage
# from agent.model_provider import request_edits
# from agent.clustering import ViolationCluster, build_clusters
# from agent.prompts import SYSTEM_PROMPT, build_fix_prompt, build_repair_prompt
# from audit.runner import audit_html
# from audit.scorer import compare, score_from_violations

# logger = logging.getLogger(__name__)

# MAX_REPAIR_ATTEMPTS = 2
# MAX_CLUSTERS_PER_RUN = 8

# # How long the fixing loop may run before it stops taking on new rules and goes
# # to finalise with what it already has. This is not a safety net for a hung call
# # -- model_provider bounds those -- it is the difference between a run that goes
# # over time and still produces a before/after, and one the Lambda kills at 900s
# # leaving nothing at all. Default leaves room for the final audit and the S3
# # writes inside a 900s Lambda.
# RUN_BUDGET_SECONDS = float(os.environ.get("RUN_BUDGET_SECONDS", "600"))


# class RunState(TypedDict):
#     """Complete pipeline state. Every key is always present."""

#     run_id: str
#     page_url: str
#     page_title: str

#     original_html: str
#     working_html: str

#     original_violations: list[dict]
#     final_violations: list[dict]

#     clusters: list[ViolationCluster]
#     cluster_index: int
#     repair_attempts: int
#     last_rejection_feedback: str
#     last_newly_introduced: list[str]
#     last_pass_was_empty: bool
#     cluster_start_html: str
#     cluster_start_edit_count: int
#     cluster_start_score: int

#     accepted_edits: list[dict]
#     applied_signatures: list[str]
#     deferred_items: list[dict]
#     unfixed_rules: list[str]
#     deferred_rules: list[str]
#     cluster_reports: list[dict]
#     last_rule_status: str

#     score_before: int
#     score_after: int
#     token_usage: TokenUsage

#     # time.monotonic() value past which no new cluster is started. 0 disables.
#     deadline: float

#     log: Annotated[list[str], operator.add]


# class StateUpdate(TypedDict, total=False):
#     """The slice of RunState a node returns. Every key optional by design."""

#     page_title: str
#     working_html: str
#     original_violations: list[dict]
#     final_violations: list[dict]
#     clusters: list[ViolationCluster]
#     cluster_index: int
#     repair_attempts: int
#     last_rejection_feedback: str
#     last_newly_introduced: list[str]
#     last_pass_was_empty: bool
#     cluster_start_html: str
#     cluster_start_edit_count: int
#     cluster_start_score: int
#     accepted_edits: list[dict]
#     applied_signatures: list[str]
#     deferred_items: list[dict]
#     unfixed_rules: list[str]
#     deferred_rules: list[str]
#     cluster_reports: list[dict]
#     last_rule_status: str
#     score_before: int
#     score_after: int
#     token_usage: TokenUsage
#     deadline: float
#     log: list[str]


# def initial_state(
#     run_id: str,
#     page_url: str,
#     page_title: str,
#     original_html: str,
#     budget_seconds: float | None = None,
# ) -> RunState:
#     """Build a complete RunState. Call this instead of writing a dict literal."""
#     budget = RUN_BUDGET_SECONDS if budget_seconds is None else budget_seconds
#     return RunState(
#         run_id=run_id,
#         page_url=page_url,
#         page_title=page_title,
#         original_html=original_html,
#         working_html=original_html,
#         original_violations=[],
#         final_violations=[],
#         clusters=[],
#         cluster_index=0,
#         repair_attempts=0,
#         last_rejection_feedback="",
#         last_newly_introduced=[],
#         last_pass_was_empty=False,
#         cluster_start_html=original_html,
#         cluster_start_edit_count=0,
#         cluster_start_score=0,
#         accepted_edits=[],
#         applied_signatures=[],
#         deferred_items=[],
#         unfixed_rules=[],
#         deferred_rules=[],
#         cluster_reports=[],
#         last_rule_status="open",
#         score_before=0,
#         score_after=0,
#         token_usage=TokenUsage(),
#         deadline=(time.monotonic() + budget) if budget > 0 else 0.0,
#         log=[],
#     )


# def out_of_time(state: RunState) -> bool:
#     deadline = state.get("deadline", 0.0)
#     return bool(deadline) and time.monotonic() >= deadline


# def seconds_left(state: RunState) -> int:
#     deadline = state.get("deadline", 0.0)
#     return max(0, int(deadline - time.monotonic())) if deadline else 0


# def node_audit_original(state: RunState) -> StateUpdate:
#     result = audit_html(state["original_html"], take_screenshot=False)
#     score = score_from_violations(result.violations)
#     return {
#         "original_violations": result.violations,
#         "working_html": state["original_html"],
#         "score_before": score,
#         "score_after": score,
#         "log": [f"audit_original: {len(result.violations)} rules failing, score={score}"],
#     }


# def node_cluster(state: RunState) -> StateUpdate:
#     clusters = build_clusters(state["original_violations"])[:MAX_CLUSTERS_PER_RUN]
#     queued = ", ".join(cluster.rule_id for cluster in clusters) or "(none)"
#     return {
#         "clusters": clusters,
#         "cluster_index": 0,
#         "repair_attempts": 0,
#         "log": [f"cluster: {len(clusters)} cluster(s) queued: {queued}"],
#     }


# def edit_signature(edit: dict) -> str:
#     """Identity of an edit, so a repair pass does not re-apply what already landed."""
#     return "|".join(
#         [
#             edit.get("violation_id", ""),
#             edit.get("selector", ""),
#             edit.get("op", ""),
#             json.dumps(edit.get("args", {}), sort_keys=True),
#         ]
#     )


# def _remaining_nodes_for_rule(html_text: str, rule_id: str) -> int:
#     result = audit_html(html_text, take_screenshot=False)
#     for violation in result.violations:
#         if violation["id"] == rule_id:
#             return violation.get("totalNodes", len(violation.get("nodes", [])))
#     return 0


# def node_generate_and_apply(state: RunState) -> StateUpdate:
#     cluster = state["clusters"][state["cluster_index"]]
#     is_repair = state["repair_attempts"] > 0

#     # Snapshot before touching this rule, so a cluster that ends up making the
#     # page worse can be undone rather than shipped.
#     snapshot: StateUpdate = {}
#     if not is_repair:
#         snapshot = {
#             "cluster_start_html": state["working_html"],
#             "cluster_start_edit_count": len(state["accepted_edits"]),
#             "cluster_start_score": state["score_after"],
#         }

#     if is_repair:
#         remaining = _remaining_nodes_for_rule(state["working_html"], cluster.rule_id)
#         prompt = build_repair_prompt(
#             cluster=cluster,
#             rejection_feedback=state["last_rejection_feedback"],
#             remaining_violations_for_rule=remaining,
#             newly_introduced_rules=state["last_newly_introduced"],
#         )
#     else:
#         prompt = build_fix_prompt(cluster, state["page_title"], state["page_url"])

#     model_output = request_edits(
#         SYSTEM_PROMPT,
#         prompt,
#         rule_id=cluster.rule_id,
#         nodes=cluster.sample_nodes,
#     )

#     usage = state["token_usage"]
#     usage.add(model_output.usage)

#     def with_snippets(applied) -> dict:
#         """The edit plus the markup it actually changed, for the diff view."""
#         return {
#             **applied.edit,
#             "matched_nodes": applied.matched_nodes,
#             "before_snippets": applied.before_snippets,
#             "after_snippets": applied.after_snippets,
#         }

#     already_applied = set(state["applied_signatures"])
#     fresh_edits = [
#         edit for edit in model_output.edits if edit_signature(edit) not in already_applied
#     ]
#     duplicate_count = len(model_output.edits) - len(fresh_edits)

#     apply_result = apply_edits(state["working_html"], fresh_edits)
#     pass_label = "repair" if is_repair else "fix"

#     known_deferred = {
#         (item.get("violation_id"), item.get("selector")) for item in state["deferred_items"]
#     }
#     fresh_deferred = [
#         item
#         for item in model_output.deferred
#         if (item.get("violation_id"), item.get("selector")) not in known_deferred
#     ]

#     duplicate_note = f", {duplicate_count} already applied" if duplicate_count else ""

#     return {
#         **snapshot,
#         "working_html": apply_result.html,
#         "accepted_edits": state["accepted_edits"]
#         + [with_snippets(applied) for applied in apply_result.applied],
#         "applied_signatures": state["applied_signatures"]
#         + [edit_signature(applied.edit) for applied in apply_result.applied],
#         "deferred_items": state["deferred_items"] + fresh_deferred,
#         "last_rejection_feedback": apply_result.rejection_feedback(),
#         # "Made no progress" covers two cases that both mean another identical
#         # prompt is wasted money: the model returned nothing, or it returned
#         # only edits that had already landed.
#         "last_pass_was_empty": (
#             not apply_result.applied
#             and not fresh_deferred
#         ),
#         "token_usage": usage,
#         "log": [
#             f"{pass_label} {cluster.rule_id}: "
#             f"model proposed {len(model_output.edits)} edit(s), "
#             f"{apply_result.applied_count} applied, "
#             f"{len(apply_result.rejected)} rejected, "
#             f"{len(fresh_deferred)} deferred{duplicate_note}"
#         ],
#     }


# def classify_rule_status(
#     rule_id: str,
#     violations: list[dict],
#     deferred_items: list[dict],
# ) -> tuple[str, int]:
#     """resolved | deferred | open, plus how many nodes still fail.

#     'deferred' means every node still failing this rule is one the model
#     explicitly declined to guess at. That is the correct outcome for things like
#     alt text on a photograph, and it must not be retried as if it were a failure.
#     """
#     remaining = next(
#         (violation for violation in violations if violation["id"] == rule_id), None
#     )
#     if remaining is None:
#         return "resolved", 0

#     remaining_targets = {
#         node["target"][0]
#         for node in remaining.get("nodes", [])
#         if node.get("target")
#     }
#     deferred_targets = {
#         item.get("selector")
#         for item in deferred_items
#         if item.get("violation_id") == rule_id
#     }

#     node_count = remaining.get("totalNodes", len(remaining.get("nodes", [])))
#     if remaining_targets and remaining_targets.issubset(deferred_targets):
#         return "deferred", node_count
#     return "open", node_count


# def node_verify(state: RunState) -> StateUpdate:
#     cluster = state["clusters"][state["cluster_index"]]
#     result = audit_html(state["working_html"], take_screenshot=False)

#     delta = compare(state["original_violations"], result.violations)
#     rule_status, remaining_nodes = classify_rule_status(
#         cluster.rule_id, result.violations, state["deferred_items"]
#     )
#     introduced_label = ", ".join(delta.introduced_rules) or "none"

#     return {
#         "final_violations": result.violations,
#         "score_after": delta.score_after,
#         "last_newly_introduced": delta.introduced_rules,
#         "last_rule_status": rule_status,
#         "log": [
#             f"verify {cluster.rule_id}: {rule_status} ({remaining_nodes} node(s) left) "
#             f"score={delta.score_before}->{delta.score_after} "
#             f"introduced={introduced_label}"
#         ],
#     }


# def route_after_verify(state: RunState) -> str:
#     rule_status = state["last_rule_status"]

#     # A cluster "regressed" if it introduced a new rule OR lowered the score.
#     # The second case matters: making an existing rule fail on more nodes does
#     # not introduce a new rule id, but it still makes the page worse.
#     introduced_regressions = (
#         bool(state["last_newly_introduced"])
#         or state["score_after"] < state["cluster_start_score"]
#     )

#     if rule_status in {"resolved", "deferred"} and not introduced_regressions:
#         return "next_cluster"

#     if state["repair_attempts"] >= MAX_REPAIR_ATTEMPTS:
#         return "give_up"

#     # Out of budget: do not start another model call. give_up still rolls back
#     # anything this cluster broke, so stopping here is safe, not just quick.
#     if out_of_time(state):
#         return "give_up"

#     # The pass changed nothing -- the model either returned nothing, or only
#     # repeated edits that had already landed. Another identical prompt returns
#     # the same thing; spending a call to prove it is waste.
#     if state["last_pass_was_empty"]:
#         return "give_up"

#     return "repair"


# def node_repair(state: RunState) -> StateUpdate:
#     return {"repair_attempts": state["repair_attempts"] + 1}


# def node_give_up(state: RunState) -> StateUpdate:
#     """Stop working this rule. Revert if the attempt left the page worse.

#     A run must never be able to lower the score. If this cluster introduced a
#     violation that repair could not clear, every edit it made is rolled back and
#     the rule is reported as unfixed -- which is honest, and strictly better for
#     the user than shipping a page we damaged.
#     """
#     cluster = state["clusters"][state["cluster_index"]]
#     introduced = state["last_newly_introduced"]
#     score_dropped = state["score_after"] < state["cluster_start_score"]

#     update: StateUpdate = {
#         "unfixed_rules": state["unfixed_rules"] + [cluster.rule_id],
#     }

#     if introduced or score_dropped:
#         kept_edits = state["accepted_edits"][: state["cluster_start_edit_count"]]
#         reverted_count = len(state["accepted_edits"]) - len(kept_edits)

#         update.update(
#             {
#                 "working_html": state["cluster_start_html"],
#                 "accepted_edits": kept_edits,
#                 "applied_signatures": [
#                     edit_signature(edit) for edit in kept_edits
#                 ],
#                 "last_newly_introduced": [],
#                 "score_after": state["cluster_start_score"],
#                 "cluster_reports": state["cluster_reports"]
#                 + [
#                     {
#                         "rule": cluster.rule_id,
#                         "status": "reverted",
#                         "attempts": state["repair_attempts"] + 1,
#                         "introduced": introduced,
#                         "revertedEdits": reverted_count,
#                     }
#                 ],
#                 "log": [
#                     f"revert {cluster.rule_id}: "
#                     + (
#                         f"introduced {', '.join(introduced)}"
#                         if introduced
#                         else f"score fell {state['cluster_start_score']}->{state['score_after']}"
#                     )
#                     + f"; rolled back {reverted_count} edit(s)"
#                 ],
#             }
#         )
#         return update

#     update.update(
#         {
#             "cluster_reports": state["cluster_reports"]
#             + [
#                 {
#                     "rule": cluster.rule_id,
#                     "status": "unfixed",
#                     "attempts": state["repair_attempts"] + 1,
#                 }
#             ],
#             "log": [
#                 f"give_up {cluster.rule_id}: exhausted {MAX_REPAIR_ATTEMPTS} repair attempt(s)"
#             ],
#         }
#     )
#     return update


# def node_next_cluster(state: RunState) -> StateUpdate:
#     """Close out this rule, then stop early if the run is out of budget.

#     Stopping early is a real outcome, not an error: every cluster that already
#     finished is verified and kept, so the report still shows a genuine
#     before/after. The alternative -- starting a rule we cannot finish -- risks
#     the Lambda being killed mid-cluster, which produces nothing at all.
#     """
#     update = _close_cluster(state)

#     clusters = state["clusters"]
#     next_index = update.get("cluster_index", state["cluster_index"])
#     remaining = len(clusters) - next_index

#     if remaining > 0 and out_of_time(state):
#         skipped = [cluster.rule_id for cluster in clusters[next_index:]]
#         update["cluster_index"] = len(clusters)
#         update["unfixed_rules"] = list(update.get("unfixed_rules", state["unfixed_rules"])) + skipped
#         update["log"] = list(update.get("log", [])) + [
#             f"budget: time limit reached after {next_index} of {len(clusters)} rule(s); "
#             f"finalising with what has landed (skipped: {', '.join(skipped)})"
#         ]

#     return update


# def _close_cluster(state: RunState) -> StateUpdate:
#     """Close out this rule and move on -- keeping its edits only if they helped.

#     A cluster that resolved its own rule but left the page scoring no better is
#     not a win, it is churn. Reverting it keeps the run monotonic: the score after
#     every cluster is greater than or equal to the score before it.
#     """
#     cluster = state["clusters"][state["cluster_index"]]
#     reports = state["cluster_reports"]

#     made_things_worse = state["score_after"] < state["cluster_start_score"]
#     if made_things_worse:
#         kept_edits = state["accepted_edits"][: state["cluster_start_edit_count"]]
#         reverted_count = len(state["accepted_edits"]) - len(kept_edits)
#         return {
#             "cluster_index": state["cluster_index"] + 1,
#             "repair_attempts": 0,
#             "last_rejection_feedback": "",
#             "last_newly_introduced": [],
#             "last_pass_was_empty": False,
#             "last_rule_status": "open",
#             "working_html": state["cluster_start_html"],
#             "accepted_edits": kept_edits,
#             "applied_signatures": [edit_signature(edit) for edit in kept_edits],
#             "score_after": state["cluster_start_score"],
#             "unfixed_rules": state["unfixed_rules"] + [cluster.rule_id],
#             "cluster_reports": reports
#             + [
#                 {
#                     "rule": cluster.rule_id,
#                     "status": "reverted",
#                     "attempts": state["repair_attempts"] + 1,
#                     "reason": "score did not improve",
#                     "revertedEdits": reverted_count,
#                 }
#             ],
#             "log": [
#                 f"revert {cluster.rule_id}: score would have gone "
#                 f"{state['cluster_start_score']} -> {state['score_after']}; "
#                 f"rolled back {reverted_count} edit(s)"
#             ],
#         }

#     status = "deferred" if state["last_rule_status"] == "deferred" else "fixed"

#     already_reported = any(report["rule"] == cluster.rule_id for report in reports)
#     if not already_reported:
#         reports = reports + [
#             {
#                 "rule": cluster.rule_id,
#                 "status": status,
#                 "attempts": state["repair_attempts"] + 1,
#             }
#         ]

#     deferred_rules = state["deferred_rules"]
#     if status == "deferred" and cluster.rule_id not in deferred_rules:
#         deferred_rules = deferred_rules + [cluster.rule_id]

#     return {
#         "cluster_index": state["cluster_index"] + 1,
#         "repair_attempts": 0,
#         "last_rejection_feedback": "",
#         "last_newly_introduced": [],
#         "last_pass_was_empty": False,
#         "last_rule_status": "open",
#         "cluster_reports": reports,
#         "deferred_rules": deferred_rules,
#     }


# def route_after_cluster(state: RunState) -> str:
#     return "generate" if state["clusters"] else "finalise"


# def route_after_next_cluster(state: RunState) -> str:
#     if state["cluster_index"] >= len(state["clusters"]):
#         return "finalise"
#     return "generate"


# def node_finalise(state: RunState) -> StateUpdate:
#     result = audit_html(state["working_html"], take_screenshot=False)
#     delta = compare(state["original_violations"], result.violations)
#     return {
#         "final_violations": result.violations,
#         "score_after": delta.score_after,
#         "log": [f"finalise: {delta.summary_line()}"],
#     }


# def build_graph():
#     graph = StateGraph(RunState)

#     graph.add_node("audit_original", node_audit_original)
#     graph.add_node("cluster", node_cluster)
#     graph.add_node("generate", node_generate_and_apply)
#     graph.add_node("verify", node_verify)
#     graph.add_node("repair", node_repair)
#     graph.add_node("give_up", node_give_up)
#     graph.add_node("next_cluster", node_next_cluster)
#     graph.add_node("finalise", node_finalise)

#     graph.set_entry_point("audit_original")
#     graph.add_edge("audit_original", "cluster")

#     graph.add_conditional_edges(
#         "cluster",
#         route_after_cluster,
#         {"generate": "generate", "finalise": "finalise"},
#     )

#     graph.add_edge("generate", "verify")
#     graph.add_conditional_edges(
#         "verify",
#         route_after_verify,
#         {"repair": "repair", "give_up": "give_up", "next_cluster": "next_cluster"},
#     )
#     graph.add_edge("repair", "generate")
#     graph.add_edge("give_up", "next_cluster")

#     graph.add_conditional_edges(
#         "next_cluster",
#         route_after_next_cluster,
#         {"generate": "generate", "finalise": "finalise"},
#     )
#     graph.add_edge("finalise", END)

#     return graph.compile()