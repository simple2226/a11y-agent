"""
The contract between the model and the DOM.

The model NEVER writes HTML. It emits a list of structured edit operations,
each keyed by a CSS selector, which we apply deterministically with lxml. This
is what makes the output auditable, diffable, and impossible to hallucinate a
broken layout into.

It also gives the model a legitimate way to say "I can't fix this safely" via
the `deferred` array, which is how we handle things like alt text for images
the model has never seen.
"""

from __future__ import annotations

EDIT_OPS = [
    "set_attribute",
    "remove_attribute",
    "set_text",
    "wrap_element",
    "insert_before",
    "insert_after",
    "add_css_rule",
]

# Selectors we refuse to touch no matter what the model says. Editing these
# is how you accidentally nuke a page.
PROTECTED_SELECTORS = {"html", "head", "body", "*", ":root", "script", "style"}

# If one selector matches more nodes than this, we reject the edit rather than
# apply a blind sweep across the page.
MAX_MATCHES_PER_EDIT = 40

TOOL_NAME = "emit_accessibility_edits"

EDIT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "violation_id": {
            "type": "string",
            "description": "The axe rule id this edit fixes, e.g. 'image-alt'.",
        },
        "selector": {
            "type": "string",
            "description": (
                "A CSS selector identifying the node(s) to edit. Prefer the exact "
                "selector axe reported in nodes[].target. Must not be a bare "
                "html/head/body/* selector."
            ),
        },
        "op": {
            "type": "string",
            "enum": EDIT_OPS,
            "description": "The operation to perform on the matched node(s).",
        },
        "args": {
            "type": "object",
            "description": (
                "Operation arguments. "
                "set_attribute: {name, value}. "
                "remove_attribute: {name}. "
                "set_text: {value}. "
                "wrap_element: {tag, attributes?}. "
                "insert_before / insert_after: {tag, text?, attributes?}. "
                "add_css_rule: {rule} -- a complete CSS rule including selector and braces."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "One sentence on why this fixes the violation. Shown in the UI.",
        },
    },
    "required": ["violation_id", "selector", "op", "args", "rationale"],
}

DEFERRED_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "violation_id": {"type": "string"},
        "selector": {"type": "string"},
        "reason": {
            "type": "string",
            "description": (
                "Why this cannot be fixed automatically and safely. Use this "
                "instead of guessing. Example: alt text for a photograph whose "
                "content is not determinable from the markup."
            ),
        },
    },
    "required": ["violation_id", "selector", "reason"],
}

TOOL_SPEC = {
    "toolSpec": {
        "name": TOOL_NAME,
        "description": (
            "Emit accessibility fixes for the supplied axe-core violation cluster "
            "as structured DOM edit operations. Emit nothing that you cannot "
            "justify from the markup you were shown."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "edits": {"type": "array", "items": EDIT_ITEM_SCHEMA},
                    "deferred": {"type": "array", "items": DEFERRED_ITEM_SCHEMA},
                },
                "required": ["edits", "deferred"],
            }
        },
    }
}


class InvalidEdit(ValueError):
    pass


def validate_edit(edit: dict) -> None:
    """Structural validation before the edit ever reaches lxml."""
    for required_field in ("violation_id", "selector", "op", "args"):
        if required_field not in edit:
            raise InvalidEdit(f"missing field '{required_field}'")

    if edit["op"] not in EDIT_OPS:
        raise InvalidEdit(f"unknown op '{edit['op']}'")

    selector = edit["selector"].strip()
    if not selector:
        raise InvalidEdit("empty selector")
    if selector.lower() in PROTECTED_SELECTORS:
        raise InvalidEdit(f"selector '{selector}' is protected")

    args = edit["args"]
    if not isinstance(args, dict):
        raise InvalidEdit("args must be an object")

    required_args = {
        "set_attribute": ("name", "value"),
        "remove_attribute": ("name",),
        "set_text": ("value",),
        "wrap_element": ("tag",),
        "insert_before": ("tag",),
        "insert_after": ("tag",),
        "add_css_rule": ("rule",),
    }[edit["op"]]

    for argument_name in required_args:
        if argument_name not in args:
            raise InvalidEdit(f"op '{edit['op']}' requires args.{argument_name}")

    if edit["op"] == "set_attribute" and args["name"].lower().startswith("on"):
        raise InvalidEdit("refusing to set an inline event handler attribute")
