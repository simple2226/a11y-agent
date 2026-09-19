# """
# Apply structured edits to an HTML document, deterministically.

# Every edit either applies cleanly to a bounded set of nodes or is rejected with
# a reason. Nothing is applied on a best-effort basis. The per-edit before/after
# snippets recorded here are exactly what the frontend renders as the diff.
# """

# from __future__ import annotations

# import copy
# from dataclasses import dataclass, field
# from typing import cast

# from lxml import html as lxml_html
# from lxml.cssselect import CSSSelector

# from agent.schema import MAX_MATCHES_PER_EDIT, InvalidEdit, validate_edit

# PATCH_STYLE_ELEMENT_ID = "a11y-agent-patch"

# SNIPPET_LENGTH = 600


# @dataclass
# class AppliedEdit:
#     edit: dict
#     matched_nodes: int
#     before_snippets: list[str] = field(default_factory=list)
#     after_snippets: list[str] = field(default_factory=list)


# @dataclass
# class RejectedEdit:
#     edit: dict
#     reason: str


# @dataclass
# class ApplyResult:
#     html: str
#     applied: list[AppliedEdit] = field(default_factory=list)
#     rejected: list[RejectedEdit] = field(default_factory=list)

#     @property
#     def applied_count(self) -> int:
#         return len(self.applied)

#     def rejection_feedback(self) -> str:
#         """Compact text fed back to the model on a repair pass."""
#         lines = []
#         for rejection in self.rejected:
#             lines.append(
#                 f"- selector={rejection.edit.get('selector')!r} "
#                 f"op={rejection.edit.get('op')!r} rejected: {rejection.reason}"
#             )
#         return "\n".join(lines)


# def _serialise(element, **kwargs) -> str:
#     """lxml's tostring is typed str | bytes; with encoding="unicode" it is always str."""
#     return cast(str, lxml_html.tostring(element, encoding="unicode", **kwargs))


# def _snippet(element) -> str:
#     return _serialise(element, with_tail=False)[:SNIPPET_LENGTH]


# def _get_or_create_patch_style_element(tree):
#     existing = tree.xpath(f"//style[@id='{PATCH_STYLE_ELEMENT_ID}']")
#     if existing:
#         return existing[0]

#     head_elements = tree.xpath("//head")
#     parent = head_elements[0] if head_elements else tree

#     style_element = lxml_html.Element("style")
#     style_element.set("id", PATCH_STYLE_ELEMENT_ID)
#     style_element.text = "\n"
#     parent.append(style_element)
#     return style_element


# def _apply_set_attribute(element, args: dict) -> None:
#     element.set(args["name"], str(args["value"]))


# def _apply_remove_attribute(element, args: dict) -> None:
#     element.attrib.pop(args["name"], None)


# def _apply_set_text(element, args: dict) -> None:
#     for child in list(element):
#         element.remove(child)
#     element.text = str(args["value"])


# def _build_new_element(args: dict):
#     new_element = lxml_html.Element(args["tag"])
#     for attribute_name, attribute_value in (args.get("attributes") or {}).items():
#         new_element.set(attribute_name, str(attribute_value))
#     if args.get("text"):
#         new_element.text = str(args["text"])
#     return new_element


# def _apply_wrap_element(element, args: dict) -> None:
#     parent = element.getparent()
#     if parent is None:
#         raise InvalidEdit("cannot wrap a node with no parent")

#     wrapper = _build_new_element(args)
#     index = parent.index(element)
#     parent.insert(index, wrapper)
#     wrapper.append(element)

#     # Preserve tail text that belonged to the original node.
#     wrapper.tail = element.tail
#     element.tail = None


# def _apply_insert_before(element, args: dict) -> None:
#     parent = element.getparent()
#     if parent is None:
#         raise InvalidEdit("cannot insert before a node with no parent")
#     parent.insert(parent.index(element), _build_new_element(args))


# def _apply_insert_after(element, args: dict) -> None:
#     parent = element.getparent()
#     if parent is None:
#         raise InvalidEdit("cannot insert after a node with no parent")
#     parent.insert(parent.index(element) + 1, _build_new_element(args))


# NODE_OPERATIONS = {
#     "set_attribute": _apply_set_attribute,
#     "remove_attribute": _apply_remove_attribute,
#     "set_text": _apply_set_text,
#     "wrap_element": _apply_wrap_element,
#     "insert_before": _apply_insert_before,
#     "insert_after": _apply_insert_after,
# }


# def apply_edits(html_text: str, edits: list[dict]) -> ApplyResult:
#     tree = lxml_html.fromstring(html_text)
#     result = ApplyResult(html=html_text)

#     for edit in edits:
#         try:
#             validate_edit(edit)
#         except InvalidEdit as error:
#             result.rejected.append(RejectedEdit(edit, str(error)))
#             continue

#         if edit["op"] == "add_css_rule":
#             style_element = _get_or_create_patch_style_element(tree)
#             style_element.text = (style_element.text or "") + "\n" + edit["args"]["rule"]
#             result.applied.append(AppliedEdit(edit=edit, matched_nodes=1))
#             continue

#         try:
#             selector = CSSSelector(edit["selector"])
#         except Exception as error:  # cssselect raises several unrelated types
#             result.rejected.append(RejectedEdit(edit, f"unparseable selector: {error}"))
#             continue

#         matches = selector(tree)

#         if not matches:
#             result.rejected.append(RejectedEdit(edit, "selector matched 0 nodes"))
#             continue
#         if len(matches) > MAX_MATCHES_PER_EDIT:
#             result.rejected.append(
#                 RejectedEdit(edit, f"selector matched {len(matches)} nodes (max {MAX_MATCHES_PER_EDIT})")
#             )
#             continue

#         applied_record = AppliedEdit(edit=edit, matched_nodes=len(matches))
#         operation = NODE_OPERATIONS[edit["op"]]

#         failed = False
#         for element in matches:
#             before_copy = copy.deepcopy(element)
#             try:
#                 operation(element, edit["args"])
#             except InvalidEdit as error:
#                 result.rejected.append(RejectedEdit(edit, str(error)))
#                 failed = True
#                 break
#             except Exception as error:
#                 result.rejected.append(RejectedEdit(edit, f"apply failed: {error}"))
#                 failed = True
#                 break

#             if len(applied_record.before_snippets) < 3:
#                 applied_record.before_snippets.append(_snippet(before_copy))
#                 target = element.getparent() if edit["op"] == "wrap_element" else element
#                 applied_record.after_snippets.append(_snippet(target))

#         if not failed:
#             result.applied.append(applied_record)

#     result.html = _serialise(tree, doctype="<!DOCTYPE html>")
#     return result

# """
# Apply structured edits to an HTML document, deterministically.

# Every edit either applies cleanly to a bounded set of nodes or is rejected with
# a reason. Nothing is applied on a best-effort basis. The per-edit before/after
# snippets recorded here are exactly what the frontend renders as the diff.
# """

# from __future__ import annotations

# import copy
# from dataclasses import dataclass, field
# from typing import cast

# from lxml import html as lxml_html
# from lxml.cssselect import CSSSelector

# from agent.schema import MAX_MATCHES_PER_EDIT, InvalidEdit, validate_edit

# PATCH_STYLE_ELEMENT_ID = "a11y-agent-patch"

# SNIPPET_LENGTH = 600


# @dataclass
# class AppliedEdit:
#     edit: dict
#     matched_nodes: int
#     before_snippets: list[str] = field(default_factory=list)
#     after_snippets: list[str] = field(default_factory=list)


# @dataclass
# class RejectedEdit:
#     edit: dict
#     reason: str


# @dataclass
# class ApplyResult:
#     html: str
#     applied: list[AppliedEdit] = field(default_factory=list)
#     rejected: list[RejectedEdit] = field(default_factory=list)

#     @property
#     def applied_count(self) -> int:
#         return len(self.applied)

#     def rejection_feedback(self) -> str:
#         """Compact text fed back to the model on a repair pass."""
#         lines = []
#         for rejection in self.rejected:
#             lines.append(
#                 f"- selector={rejection.edit.get('selector')!r} "
#                 f"op={rejection.edit.get('op')!r} rejected: {rejection.reason}"
#             )
#         return "\n".join(lines)


# def _serialise(element, **kwargs) -> str:
#     """lxml's tostring is typed str | bytes; with encoding="unicode" it is always str."""
#     return cast(str, lxml_html.tostring(element, encoding="unicode", **kwargs))


# def _snippet(element) -> str:
#     return _serialise(element, with_tail=False)[:SNIPPET_LENGTH]


# def _get_or_create_patch_style_element(tree):
#     existing = tree.xpath(f"//style[@id='{PATCH_STYLE_ELEMENT_ID}']")
#     if existing:
#         return existing[0]

#     head_elements = tree.xpath("//head")
#     parent = head_elements[0] if head_elements else tree

#     style_element = lxml_html.Element("style")
#     style_element.set("id", PATCH_STYLE_ELEMENT_ID)
#     style_element.text = "\n"
#     parent.append(style_element)
#     return style_element


# def _apply_set_attribute(element, args: dict) -> None:
#     element.set(args["name"], str(args["value"]))


# def _apply_remove_attribute(element, args: dict) -> None:
#     element.attrib.pop(args["name"], None)


# def _apply_set_text(element, args: dict) -> None:
#     for child in list(element):
#         element.remove(child)
#     element.text = str(args["value"])


# def _build_new_element(args: dict):
#     new_element = lxml_html.Element(args["tag"])
#     for attribute_name, attribute_value in (args.get("attributes") or {}).items():
#         new_element.set(attribute_name, str(attribute_value))
#     if args.get("text"):
#         new_element.text = str(args["text"])
#     return new_element


# def _apply_wrap_element(element, args: dict) -> None:
#     parent = element.getparent()
#     if parent is None:
#         raise InvalidEdit("cannot wrap a node with no parent")

#     wrapper = _build_new_element(args)
#     index = parent.index(element)
#     parent.insert(index, wrapper)
#     wrapper.append(element)

#     # Preserve tail text that belonged to the original node.
#     wrapper.tail = element.tail
#     element.tail = None


# def _apply_insert_before(element, args: dict) -> None:
#     parent = element.getparent()
#     if parent is None:
#         raise InvalidEdit("cannot insert before a node with no parent")
#     parent.insert(parent.index(element), _build_new_element(args))


# def _apply_insert_after(element, args: dict) -> None:
#     parent = element.getparent()
#     if parent is None:
#         raise InvalidEdit("cannot insert after a node with no parent")
#     parent.insert(parent.index(element) + 1, _build_new_element(args))


# NODE_OPERATIONS = {
#     "set_attribute": _apply_set_attribute,
#     "remove_attribute": _apply_remove_attribute,
#     "set_text": _apply_set_text,
#     "wrap_element": _apply_wrap_element,
#     "insert_before": _apply_insert_before,
#     "insert_after": _apply_insert_after,
# }


# def apply_edits(html_text: str, edits: list[dict]) -> ApplyResult:
#     tree = lxml_html.fromstring(html_text)
#     result = ApplyResult(html=html_text)

#     for edit in edits:
#         try:
#             validate_edit(edit)
#         except InvalidEdit as error:
#             result.rejected.append(RejectedEdit(edit, str(error)))
#             continue

#         if edit["op"] == "add_css_rule":
#             style_element = _get_or_create_patch_style_element(tree)
#             style_element.text = (style_element.text or "") + "\n" + edit["args"]["rule"]
#             result.applied.append(AppliedEdit(edit=edit, matched_nodes=1))
#             continue

#         try:
#             selector = CSSSelector(edit["selector"])
#         except Exception as error:  # cssselect raises several unrelated types
#             result.rejected.append(RejectedEdit(edit, f"unparseable selector: {error}"))
#             continue

#         matches = selector(tree)

#         if not matches:
#             result.rejected.append(RejectedEdit(edit, "selector matched 0 nodes"))
#             continue
#         if len(matches) > MAX_MATCHES_PER_EDIT:
#             result.rejected.append(
#                 RejectedEdit(edit, f"selector matched {len(matches)} nodes (max {MAX_MATCHES_PER_EDIT})")
#             )
#             continue

#         applied_record = AppliedEdit(edit=edit, matched_nodes=len(matches))
#         operation = NODE_OPERATIONS[edit["op"]]

#         failed = False
#         for element in matches:
#             before_copy = copy.deepcopy(element)
#             try:
#                 operation(element, edit["args"])
#             except InvalidEdit as error:
#                 result.rejected.append(RejectedEdit(edit, str(error)))
#                 failed = True
#                 break
#             except Exception as error:
#                 result.rejected.append(RejectedEdit(edit, f"apply failed: {error}"))
#                 failed = True
#                 break

#             if len(applied_record.before_snippets) < 3:
#                 applied_record.before_snippets.append(_snippet(before_copy))
#                 target = element.getparent() if edit["op"] == "wrap_element" else element
#                 applied_record.after_snippets.append(_snippet(target))

#         if not failed:
#             result.applied.append(applied_record)

#     result.html = _serialise(tree, doctype="<!DOCTYPE html>")
#     return result



"""
Apply structured edits to an HTML document, deterministically.

Every edit either applies cleanly to a bounded set of nodes or is rejected with
a reason. Nothing is applied on a best-effort basis. The per-edit before/after
snippets recorded here are exactly what the frontend renders as the diff.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import cast

from lxml import html as lxml_html
from lxml.cssselect import CSSSelector

from agent.schema import MAX_MATCHES_PER_EDIT, InvalidEdit, validate_edit

PATCH_STYLE_ELEMENT_ID = "a11y-agent-patch"

# A real site ships thousands of rules. A patch appended to <head> loses every
# specificity contest and the fix silently does nothing -- the agent reports
# edits applied, axe reports no change, and two repair passes are burned proving
# it. The patch block therefore goes LAST in <body> (later beats earlier on a
# specificity tie) and every declaration is marked !important.
DECLARATION_PATTERN = re.compile(r"([^;{}]+:[^;{}]+?)(?=;|\s*\})")

SNIPPET_LENGTH = 600


@dataclass
class AppliedEdit:
    edit: dict
    matched_nodes: int
    before_snippets: list[str] = field(default_factory=list)
    after_snippets: list[str] = field(default_factory=list)


@dataclass
class RejectedEdit:
    edit: dict
    reason: str


@dataclass
class ApplyResult:
    html: str
    applied: list[AppliedEdit] = field(default_factory=list)
    rejected: list[RejectedEdit] = field(default_factory=list)

    @property
    def applied_count(self) -> int:
        return len(self.applied)

    def rejection_feedback(self) -> str:
        """Compact text fed back to the model on a repair pass."""
        lines = []
        for rejection in self.rejected:
            lines.append(
                f"- selector={rejection.edit.get('selector')!r} "
                f"op={rejection.edit.get('op')!r} rejected: {rejection.reason}"
            )
        return "\n".join(lines)


def _serialise(element, **kwargs) -> str:
    """lxml's tostring is typed str | bytes; with encoding="unicode" it is always str."""
    return cast(str, lxml_html.tostring(element, encoding="unicode", **kwargs))


def _snippet(element) -> str:
    return _serialise(element, with_tail=False)[:SNIPPET_LENGTH]


def _force_important(css_rule: str) -> str:
    """Mark every declaration !important so the patch can beat the site's CSS."""

    def mark(match: re.Match) -> str:
        declaration = match.group(1).strip()
        if not declaration or "!important" in declaration.lower():
            return match.group(1)
        return f"{declaration} !important"

    return DECLARATION_PATTERN.sub(mark, css_rule)


def _get_or_create_patch_style_element(tree):
    existing = tree.xpath(f"//style[@id='{PATCH_STYLE_ELEMENT_ID}']")
    if existing:
        return existing[0]

    body_elements = tree.xpath("//body")
    parent = body_elements[0] if body_elements else tree

    style_element = lxml_html.Element("style")
    style_element.set("id", PATCH_STYLE_ELEMENT_ID)
    style_element.text = "\n"
    parent.append(style_element)
    return style_element


def _apply_set_attribute(element, args: dict) -> None:
    element.set(args["name"], str(args["value"]))


def _apply_remove_attribute(element, args: dict) -> None:
    element.attrib.pop(args["name"], None)


def _apply_set_text(element, args: dict) -> None:
    for child in list(element):
        element.remove(child)
    element.text = str(args["value"])


def _build_new_element(args: dict):
    new_element = lxml_html.Element(args["tag"])
    for attribute_name, attribute_value in (args.get("attributes") or {}).items():
        new_element.set(attribute_name, str(attribute_value))
    if args.get("text"):
        new_element.text = str(args["text"])
    return new_element


def _apply_wrap_element(element, args: dict) -> None:
    parent = element.getparent()
    if parent is None:
        raise InvalidEdit("cannot wrap a node with no parent")

    wrapper = _build_new_element(args)
    index = parent.index(element)
    parent.insert(index, wrapper)
    wrapper.append(element)

    # Preserve tail text that belonged to the original node.
    wrapper.tail = element.tail
    element.tail = None


def _apply_insert_before(element, args: dict) -> None:
    parent = element.getparent()
    if parent is None:
        raise InvalidEdit("cannot insert before a node with no parent")
    parent.insert(parent.index(element), _build_new_element(args))


def _apply_insert_after(element, args: dict) -> None:
    parent = element.getparent()
    if parent is None:
        raise InvalidEdit("cannot insert after a node with no parent")
    parent.insert(parent.index(element) + 1, _build_new_element(args))


NODE_OPERATIONS = {
    "set_attribute": _apply_set_attribute,
    "remove_attribute": _apply_remove_attribute,
    "set_text": _apply_set_text,
    "wrap_element": _apply_wrap_element,
    "insert_before": _apply_insert_before,
    "insert_after": _apply_insert_after,
}


def apply_edits(html_text: str, edits: list[dict]) -> ApplyResult:
    tree = lxml_html.fromstring(html_text)
    result = ApplyResult(html=html_text)

    for edit in edits:
        try:
            validate_edit(edit)
        except InvalidEdit as error:
            result.rejected.append(RejectedEdit(edit, str(error)))
            continue

        if edit["op"] == "add_css_rule":
            style_element = _get_or_create_patch_style_element(tree)
            rule_text = _force_important(edit["args"]["rule"])
            style_element.text = (style_element.text or "") + "\n" + rule_text
            result.applied.append(AppliedEdit(edit=edit, matched_nodes=1))
            continue

        try:
            selector = CSSSelector(edit["selector"])
        except Exception as error:  # cssselect raises several unrelated types
            result.rejected.append(RejectedEdit(edit, f"unparseable selector: {error}"))
            continue

        matches = selector(tree)

        if not matches:
            result.rejected.append(RejectedEdit(edit, "selector matched 0 nodes"))
            continue
        if len(matches) > MAX_MATCHES_PER_EDIT:
            result.rejected.append(
                RejectedEdit(edit, f"selector matched {len(matches)} nodes (max {MAX_MATCHES_PER_EDIT})")
            )
            continue

        applied_record = AppliedEdit(edit=edit, matched_nodes=len(matches))
        operation = NODE_OPERATIONS[edit["op"]]

        failed = False
        for element in matches:
            before_copy = copy.deepcopy(element)
            try:
                operation(element, edit["args"])
            except InvalidEdit as error:
                result.rejected.append(RejectedEdit(edit, str(error)))
                failed = True
                break
            except Exception as error:
                result.rejected.append(RejectedEdit(edit, f"apply failed: {error}"))
                failed = True
                break

            if len(applied_record.before_snippets) < 3:
                applied_record.before_snippets.append(_snippet(before_copy))
                target = element.getparent() if edit["op"] == "wrap_element" else element
                applied_record.after_snippets.append(_snippet(target))

        if not failed:
            result.applied.append(applied_record)

    result.html = _serialise(tree, doctype="<!DOCTYPE html>")
    return result
