"""
Prompts for the fix pass and the repair pass.

Design rules:
  - Never send the whole page. Send the rule, the help text, and sample nodes.
  - Be explicit that inventing content is worse than deferring.
  - On repair, send back exactly why the previous attempt failed.
"""

from __future__ import annotations

from agent.clustering import DERIVABLE_NAME_RULES, ViolationCluster

SYSTEM_PROMPT = """You are an accessibility engineer fixing WCAG 2.1 AA violations in an HTML document.

You do not write HTML. You emit structured DOM edit operations through the \
emit_accessibility_edits tool, and they are applied deterministically by a \
separate program that you cannot see.

Rules you must follow:

1. Use the exact CSS selector axe reported in the node's target array whenever \
possible. If you must write your own selector, make it as specific as you can.
2. Never target html, head, body, *, script or style. Those edits are rejected.
3. Never invent content that conveys meaning you cannot verify from the markup \
you were shown. An incorrect alt attribute is worse for a screen reader user \
than a missing one, because it cannot be detected and worked around.
4. Defer only when the markup genuinely does not contain the answer. If the \
element already carries the information -- a name, placeholder, title, value, \
aria-label, or visible text that describes it -- USE IT. Deferring something \
you could have derived is as wrong as inventing something you could not.
5. Prefer the least invasive operation that resolves the violation. Adding an \
aria-label beats restructuring the DOM.
6. For colour contrast, emit add_css_rule with a concrete rule that meets the \
4.5:1 ratio for normal text or 3:1 for large text. Keep the original hue where \
you can; darken or lighten to reach the ratio.
7. Every edit needs a one-sentence rationale that a human reviewer can check."""

DERIVABLE_NAME_GUIDANCE = """
Special guidance for this rule:

The accessible name is almost always recoverable from the element itself. Check,
in this order, and use the first that applies:

  1. An associated <label>, or a title / aria-label / aria-labelledby already present
  2. The placeholder attribute
  3. The value attribute on a button or submit input
  4. The visible text content of the element
  5. The name or id attribute, converted to words ("first_name" -> "First name")

Emit set_attribute with aria-label using that value. Only defer an element where
none of the five apply -- an input with no name, no placeholder, no label and no
text. That should be rare."""

DECORATIVE_IMAGE_GUIDANCE = """
Special guidance for this rule:

You cannot see the images. Do NOT generate descriptive alt text from filenames, \
surrounding text, or guesswork.

Only treat an image as decorative -- alt="" plus role="presentation" -- when the \
markup positively indicates it: class or id containing words like icon, bullet, \
divider, spacer, separator, bg, background, ornament, decoration; a 1x1 or very \
small declared size; or an image that sits inside a link or button whose own \
text already names the destination or action.

Everything else goes in `deferred` with a reason naming what a human needs to \
supply. It is normal for most of the nodes in this cluster to be deferred."""


def _node_blocks(nodes: list[dict]) -> str:
    blocks = []
    for index, node in enumerate(nodes, start=1):
        targets = node.get("target", [])
        primary = targets[0] if targets else "(no selector)"
        blocks.append(
            f"[node {index}]\n"
            f"selector: {primary}\n"
            f"html: {node.get('html', '')}\n"
            f"axe says: {node.get('failureSummary', '')}"
        )
    return "\n\n".join(blocks)


def _coverage_guidance(total_nodes: int, sample_count: int) -> str:
    """Tell the model when one-selector-per-node cannot possibly finish the job.

    A rule failing on forty nodes cannot be cleared twelve selectors at a time,
    and the agent used to try exactly that: it emitted one edit per sampled
    node, cleared those, and left the rest untouched. Where the failing elements
    share a class or a container, ONE selector fixes all of them at once -- and
    that is the difference between a score that moves and a score that does not.
    """
    hidden = total_nodes - sample_count
    if hidden <= 0:
        return (
            "\nEvery failing node is listed above, so per-node selectors are fine "
            "here.\n"
        )
    return f"""
IMPORTANT -- COVERAGE. {hidden} further node(s) fail this rule and are not \
listed. One edit per listed node will leave those {hidden} broken and the page \
will score no better than before.

Look at what the listed nodes have in common -- a shared class, a shared parent, \
a repeated element type inside one container -- and prefer a single selector \
that matches the whole family (for example `.card-link` or `nav.main a` rather \
than `:nth-child(3)` on one element). A grouped selector that fixes thirty nodes \
is worth far more than twelve exact ones.

Two limits on that: never write a selector so broad it would touch elements that \
are not failing this rule, and never use `*`, `body` or `html` as the selector. \
If the failing nodes genuinely have nothing in common, fall back to per-node \
selectors for the ones you can see.
"""


def build_fix_prompt(cluster: ViolationCluster, page_title: str, page_url: str) -> str:
    if cluster.needs_human_judgement:
        guidance = DECORATIVE_IMAGE_GUIDANCE
    elif cluster.rule_id in DERIVABLE_NAME_RULES:
        guidance = DERIVABLE_NAME_GUIDANCE
    else:
        guidance = ""
    nodes_text = _node_blocks(cluster.sample_nodes)
    sample_count = len(cluster.sample_nodes)

    return f"""Page: {page_title or '(untitled)'}
Source URL: {page_url}

axe rule: {cluster.rule_id}
impact: {cluster.impact}
help: {cluster.help_text}
description: {cluster.description}
reference: {cluster.help_url}

This rule failed on {cluster.total_nodes} node(s). You are being shown \
{sample_count} of them:

{nodes_text}
{guidance}
{_coverage_guidance(cluster.total_nodes, sample_count)}
Emit edits that resolve this violation for the nodes shown."""


def build_repair_prompt(
    cluster: ViolationCluster,
    rejection_feedback: str,
    remaining_violations_for_rule: int,
    newly_introduced_rules: list[str],
) -> str:
    sections = [
        f"Your previous edits for axe rule '{cluster.rule_id}' did not fully work.",
    ]

    if rejection_feedback:
        sections.append(f"These edits were rejected before being applied:\n{rejection_feedback}")

    if remaining_violations_for_rule:
        # These are the nodes that are STILL failing, re-read from a fresh audit
        # of the patched page -- not the original sample. Showing the original
        # sample here meant every repair pass was looking at work it had already
        # done.
        still_failing = _node_blocks(cluster.sample_nodes)
        sections.append(
            f"After applying your accepted edits, axe still reports "
            f"{remaining_violations_for_rule} node(s) failing '{cluster.rule_id}'. "
            f"These are the ones that remain -- the nodes you already fixed are "
            f"not listed:\n\n{still_failing}"
        )
        sections.append(
            _coverage_guidance(remaining_violations_for_rule, len(cluster.sample_nodes))
        )

    if newly_introduced_rules:
        sections.append(
            "Your edits INTRODUCED new violations, which is worse than leaving the "
            f"original ones: {', '.join(newly_introduced_rules)}. "
            "Fix or withdraw whatever caused them."
        )

    sections.append(
        "Emit edits for the nodes listed above. Do not repeat an edit that was "
        "already applied -- it is already in the page. If you cannot fix "
        "something safely, defer it instead of retrying the same edit."
    )

    return "\n\n".join(sections)

# """
# Prompts for the fix pass and the repair pass.

# Design rules:
#   - Never send the whole page. Send the rule, the help text, and sample nodes.
#   - Be explicit that inventing content is worse than deferring.
#   - On repair, send back exactly why the previous attempt failed.
# """

# from __future__ import annotations

# from agent.clustering import DERIVABLE_NAME_RULES, ViolationCluster

# SYSTEM_PROMPT = """You are an accessibility engineer fixing WCAG 2.1 AA violations in an HTML document.

# You do not write HTML. You emit structured DOM edit operations through the \
# emit_accessibility_edits tool, and they are applied deterministically by a \
# separate program that you cannot see.

# Rules you must follow:

# 1. Use the exact CSS selector axe reported in the node's target array whenever \
# possible. If you must write your own selector, make it as specific as you can.
# 2. Never target html, head, body, *, script or style. Those edits are rejected.
# 3. Never invent content that conveys meaning you cannot verify from the markup \
# you were shown. An incorrect alt attribute is worse for a screen reader user \
# than a missing one, because it cannot be detected and worked around.
# 4. Defer only when the markup genuinely does not contain the answer. If the \
# element already carries the information -- a name, placeholder, title, value, \
# aria-label, or visible text that describes it -- USE IT. Deferring something \
# you could have derived is as wrong as inventing something you could not.
# 5. Prefer the least invasive operation that resolves the violation. Adding an \
# aria-label beats restructuring the DOM.
# 6. For colour contrast, emit add_css_rule with a concrete rule that meets the \
# 4.5:1 ratio for normal text or 3:1 for large text. Keep the original hue where \
# you can; darken or lighten to reach the ratio.
# 7. Every edit needs a one-sentence rationale that a human reviewer can check."""

# DERIVABLE_NAME_GUIDANCE = """
# Special guidance for this rule:

# The accessible name is almost always recoverable from the element itself. Check,
# in this order, and use the first that applies:

#   1. An associated <label>, or a title / aria-label / aria-labelledby already present
#   2. The placeholder attribute
#   3. The value attribute on a button or submit input
#   4. The visible text content of the element
#   5. The name or id attribute, converted to words ("first_name" -> "First name")

# Emit set_attribute with aria-label using that value. Only defer an element where
# none of the five apply -- an input with no name, no placeholder, no label and no
# text. That should be rare."""

# DECORATIVE_IMAGE_GUIDANCE = """
# Special guidance for this rule:

# You cannot see the images. Do NOT generate descriptive alt text from filenames, \
# surrounding text, or guesswork.

# Only treat an image as decorative -- alt="" plus role="presentation" -- when the \
# markup positively indicates it: class or id containing words like icon, bullet, \
# divider, spacer, separator, bg, background, ornament, decoration; a 1x1 or very \
# small declared size; or an image that sits inside a link or button whose own \
# text already names the destination or action.

# Everything else goes in `deferred` with a reason naming what a human needs to \
# supply. It is normal for most of the nodes in this cluster to be deferred."""


# def build_fix_prompt(cluster: ViolationCluster, page_title: str, page_url: str) -> str:
#     node_blocks = []
#     for index, node in enumerate(cluster.sample_nodes, start=1):
#         target_selectors = node.get("target", [])
#         primary_selector = target_selectors[0] if target_selectors else "(no selector)"
#         node_blocks.append(
#             f"[node {index}]\n"
#             f"selector: {primary_selector}\n"
#             f"html: {node.get('html', '')}\n"
#             f"axe says: {node.get('failureSummary', '')}"
#         )

#     if cluster.needs_human_judgement:
#         guidance = DECORATIVE_IMAGE_GUIDANCE
#     elif cluster.rule_id in DERIVABLE_NAME_RULES:
#         guidance = DERIVABLE_NAME_GUIDANCE
#     else:
#         guidance = ""
#     nodes_text = "\n\n".join(node_blocks)
#     sample_count = len(cluster.sample_nodes)

#     return f"""Page: {page_title or '(untitled)'}
# Source URL: {page_url}

# axe rule: {cluster.rule_id}
# impact: {cluster.impact}
# help: {cluster.help_text}
# description: {cluster.description}
# reference: {cluster.help_url}

# This rule failed on {cluster.total_nodes} node(s). Here are up to \
# {sample_count} of them:

# {nodes_text}
# {guidance}

# Emit edits that resolve this violation for the nodes shown. If a selector can \
# safely cover more nodes of the same shape than the ones listed, use it -- but \
# never a selector so broad it would hit unrelated elements."""


# def build_repair_prompt(
#     cluster: ViolationCluster,
#     rejection_feedback: str,
#     remaining_violations_for_rule: int,
#     newly_introduced_rules: list[str],
# ) -> str:
#     sections = [
#         f"Your previous edits for axe rule '{cluster.rule_id}' did not fully work.",
#     ]

#     if rejection_feedback:
#         sections.append(f"These edits were rejected before being applied:\n{rejection_feedback}")

#     if remaining_violations_for_rule:
#         sections.append(
#             f"After applying your accepted edits, axe still reports "
#             f"{remaining_violations_for_rule} node(s) failing '{cluster.rule_id}'."
#         )

#     if newly_introduced_rules:
#         sections.append(
#             "Your edits INTRODUCED new violations, which is worse than leaving the "
#             f"original ones: {', '.join(newly_introduced_rules)}. "
#             "Fix or withdraw whatever caused them."
#         )

#     sections.append(
#         "Emit a corrected set of edits. Use different, more specific selectors. "
#         "If you cannot fix something safely, defer it instead of retrying the same edit."
#     )

#     return "\n\n".join(sections)