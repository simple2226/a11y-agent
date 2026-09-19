"""
A deterministic, rule-based stand-in for the model.

Set A11Y_MOCK_MODEL=1 and the agent loop runs end to end with no Bedrock call at
all. It is not smart -- it handles the handful of axe rules that have a single
obviously-correct mechanical fix -- but it exercises every other part of the
pipeline: clustering, the applier, verification, regression detection, repair
routing, scoring and the report.

Use it when Bedrock is blocked, when you are building the frontend, or when you
want the eval harness to run in seconds instead of minutes. Never use it for the
numbers you put in the demo -- say plainly which mode produced a given result.
"""

from __future__ import annotations

import re

from agent.bedrock_client import ModelEdits

# Markup signals that an image is decorative. Same policy the real prompt states.
DECORATIVE_PATTERN = re.compile(
    r"(icon|bullet|spacer|divider|separator|ornament|decoration|"
    r"\bbg\b|background|arrow|dot|line)",
    re.IGNORECASE,
)

VAGUE_LINK_TEXT = {
    "click here", "here", "more", "read more", "link", "download",
    "view", "details", "learn more", "continue", "go",
}


def _first_selector(node: dict) -> str:
    targets = node.get("target", [])
    return targets[0] if targets else ""


def _looks_decorative(node_html: str) -> bool:
    return bool(DECORATIVE_PATTERN.search(node_html))


def _mock_image_alt(nodes: list[dict], rule_id: str) -> tuple[list[dict], list[dict]]:
    edits, deferred = [], []
    for node in nodes:
        selector = _first_selector(node)
        if not selector:
            continue
        if _looks_decorative(node.get("html", "")):
            edits.append(
                {
                    "violation_id": rule_id,
                    "selector": selector,
                    "op": "set_attribute",
                    "args": {"name": "alt", "value": ""},
                    "rationale": "Markup identifies this as a decorative image.",
                }
            )
            edits.append(
                {
                    "violation_id": rule_id,
                    "selector": selector,
                    "op": "set_attribute",
                    "args": {"name": "role", "value": "presentation"},
                    "rationale": "Hide the decorative image from assistive technology.",
                }
            )
        else:
            deferred.append(
                {
                    "violation_id": rule_id,
                    "selector": selector,
                    "reason": "Content image; a human must describe what it depicts.",
                }
            )
    return edits, deferred


def _mock_label(nodes: list[dict], rule_id: str) -> tuple[list[dict], list[dict]]:
    edits, deferred = [], []
    for node in nodes:
        selector = _first_selector(node)
        node_html = node.get("html", "")

        name_match = re.search(r'name=["\']([^"\']+)["\']', node_html)
        placeholder_match = re.search(r'placeholder=["\']([^"\']+)["\']', node_html)

        if placeholder_match:
            label_value = placeholder_match.group(1)
        elif name_match:
            label_value = name_match.group(1).replace("_", " ").replace("-", " ").title()
        else:
            deferred.append(
                {
                    "violation_id": rule_id,
                    "selector": selector,
                    "reason": "No name or placeholder to derive a label from.",
                }
            )
            continue

        edits.append(
            {
                "violation_id": rule_id,
                "selector": selector,
                "op": "set_attribute",
                "args": {"name": "aria-label", "value": label_value},
                "rationale": f"Derived an accessible name from the field's own markup.",
            }
        )
    return edits, deferred


def _mock_link_name(nodes: list[dict], rule_id: str) -> tuple[list[dict], list[dict]]:
    edits, deferred = [], []
    for node in nodes:
        selector = _first_selector(node)
        node_html = node.get("html", "")
        text_content = re.sub(r"<[^>]+>", "", node_html).strip().lower()

        if text_content in VAGUE_LINK_TEXT or not text_content:
            deferred.append(
                {
                    "violation_id": rule_id,
                    "selector": selector,
                    "reason": "Link text is vague; a human must supply the destination's purpose.",
                }
            )
        else:
            edits.append(
                {
                    "violation_id": rule_id,
                    "selector": selector,
                    "op": "set_attribute",
                    "args": {"name": "aria-label", "value": text_content[:80]},
                    "rationale": "Promote the visible text to an explicit accessible name.",
                }
            )
    return edits, deferred


def _mock_simple_attribute(rule_id: str, selector: str, name: str, value: str, why: str) -> dict:
    return {
        "violation_id": rule_id,
        "selector": selector,
        "op": "set_attribute",
        "args": {"name": name, "value": value},
        "rationale": why,
    }


SIMPLE_RULES = {
    "html-has-lang": ("html", "lang", "en", "Declare the document language."),
    "html-lang-valid": ("html", "lang", "en", "Replace the invalid language tag."),
}


def generate_mock_edits(rule_id: str, nodes: list[dict]) -> ModelEdits:
    if rule_id in {"image-alt", "input-image-alt", "area-alt", "object-alt"}:
        edits, deferred = _mock_image_alt(nodes, rule_id)
    elif rule_id in {"label", "form-field-multiple-labels", "select-name"}:
        edits, deferred = _mock_label(nodes, rule_id)
    elif rule_id in {"link-name", "button-name"}:
        edits, deferred = _mock_link_name(nodes, rule_id)
    elif rule_id in SIMPLE_RULES:
        selector, name, value, why = SIMPLE_RULES[rule_id]
        edits = [_mock_simple_attribute(rule_id, selector, name, value, why)]
        deferred = []
    elif rule_id == "color-contrast":
        edits = [
            {
                "violation_id": rule_id,
                "selector": _first_selector(node),
                "op": "add_css_rule",
                "args": {"rule": f"{_first_selector(node)} {{ color: #1a1a1a; }}"},
                "rationale": "Darken foreground text to reach a 4.5:1 ratio.",
            }
            for node in nodes
            if _first_selector(node)
        ]
        deferred = []
    else:
        edits = []
        deferred = [
            {
                "violation_id": rule_id,
                "selector": _first_selector(node),
                "reason": "Mock model has no rule for this violation; run with Bedrock.",
            }
            for node in nodes
        ]

    return ModelEdits(
        edits=edits,
        deferred=deferred,
        usage={"inputTokens": 0, "outputTokens": 0},
        stop_reason="mock",
    )
