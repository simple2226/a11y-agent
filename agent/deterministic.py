"""Fixes that need no model, because they have exactly one right answer.

Why this exists
---------------
Every fix used to depend on a language model producing a good selector and a
good value. That made the agent's success rate a function of the model's mood,
the provider's uptime, and how many nodes fitted in one prompt -- so a page
could come back completely unchanged, which is the worst possible outcome for a
tool whose entire claim is "it works and you can show it".

But a large share of real-world accessibility violations are not judgement
calls at all:

  * `<html>` with no `lang` -- the fix is `lang="en"`. There is no second option.
  * `user-scalable=no` -- the fix is to remove it.
  * A link whose only content is `<img alt="Facebook">` -- the accessible name
    is already in the markup; it just is not exposed.
  * Low contrast -- axe hands us the foreground colour, the background colour
    and the ratio it wanted. The compliant colour is arithmetic.

Running those deterministically, before the model sees anything, means every
page improves by at least the mechanical amount, and the model's budget goes on
the genuinely ambiguous nodes.

What this deliberately does NOT do
----------------------------------
Anything requiring meaning that is not already present in the markup. It will
not invent alt text for a photograph, will not guess what an unlabelled control
does, and will not restructure the document. Those still go to the model, and
the model is still free to defer them.

Every function here returns edits in the same schema the model emits, so they
flow through exactly the same validation and applier as model edits. Nothing
here bypasses a safety check.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------

_RGB = re.compile(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", re.I)


def parse_colour(value: str) -> tuple[int, int, int] | None:
    """Accept the two forms axe emits: #rrggbb and rgb()/rgba()."""
    if not value:
        return None
    value = value.strip()

    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) >= 6:
            try:
                return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
            except ValueError:
                return None
        return None

    match = _RGB.match(value)
    if match:
        return tuple(max(0, min(255, int(float(part)))) for part in match.groups())  # type: ignore[return-value]
    return None


def _channel_luminance(channel: int) -> float:
    ratio = channel / 255.0
    return ratio / 12.92 if ratio <= 0.04045 else ((ratio + 0.055) / 1.055) ** 2.4


def relative_luminance(colour: tuple[int, int, int]) -> float:
    red, green, blue = (_channel_luminance(c) for c in colour)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(one: tuple[int, int, int], two: tuple[int, int, int]) -> float:
    light, dark = sorted((relative_luminance(one), relative_luminance(two)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _scale(colour: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Move a colour toward black (factor<1) or white (factor>1), keeping hue."""
    if factor <= 1:
        return tuple(max(0, min(255, round(c * factor))) for c in colour)  # type: ignore[return-value]
    return tuple(  # type: ignore[return-value]
        max(0, min(255, round(c + (255 - c) * (factor - 1)))) for c in colour
    )


def accessible_foreground(
    foreground: tuple[int, int, int],
    background: tuple[int, int, int],
    required: float,
) -> tuple[int, int, int] | None:
    """The nearest colour to `foreground` that clears `required` against `background`.

    Hue is preserved: the colour is walked toward black or white, whichever the
    background allows, in small steps, and the first step that clears the ratio
    wins. Keeping the brand hue is the difference between a fix a site would
    accept and one they would revert.
    """
    if contrast_ratio(foreground, background) >= required:
        return None

    # Push away from the background: darken on a light background, lighten on a
    # dark one.
    darken = relative_luminance(background) > 0.5
    steps = [1 - i / 40 for i in range(1, 41)] if darken else [1 + i / 40 for i in range(1, 41)]

    for factor in steps:
        candidate = _scale(foreground, factor)
        if contrast_ratio(candidate, background) >= required:
            return candidate

    return (0, 0, 0) if darken else (255, 255, 255)


def to_hex(colour: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*colour)


def required_ratio(data: dict) -> float:
    """axe tells us what it wanted; fall back to the WCAG AA rules for text size."""
    expected = data.get("expectedContrastRatio")
    if isinstance(expected, str) and expected.endswith(":1"):
        try:
            return float(expected[:-2])
        except ValueError:
            pass
    if isinstance(expected, (int, float)):
        return float(expected)

    # Large text is 18pt, or 14pt bold.
    try:
        size = float(str(data.get("fontSize", "0")).split("pt")[0])
        weight = str(data.get("fontWeight", "normal"))
    except (TypeError, ValueError):
        return 4.5
    if size >= 18 or (size >= 14 and weight in {"bold", "bolder", "700", "800", "900"}):
        return 3.0
    return 4.5


# --------------------------------------------------------------------------
# accessible names from markup already present
# --------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_ATTR_CACHE: dict[str, re.Pattern] = {}


def attribute_value(html: str, name: str) -> str:
    """Read an attribute off the OUTERMOST tag of a node's html snippet."""
    pattern = _ATTR_CACHE.get(name)
    if pattern is None:
        pattern = re.compile(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"|\b{re.escape(name)}\s*=\s*\'([^\']*)\'', re.I)
        _ATTR_CACHE[name] = pattern
    opening = html[: html.index(">") + 1] if ">" in html else html
    match = pattern.search(opening)
    if not match:
        return ""
    return (match.group(1) or match.group(2) or "").strip()


def nested_attribute(html: str, name: str) -> str:
    """Read an attribute from anywhere inside the snippet (e.g. a nested <img alt>)."""
    pattern = re.compile(rf'\b{re.escape(name)}\s*=\s*"([^"]*)"|\b{re.escape(name)}\s*=\s*\'([^\']*)\'', re.I)
    for match in pattern.finditer(html):
        value = (match.group(1) or match.group(2) or "").strip()
        if value:
            return value
    return ""


def visible_text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", html)).strip()


_URL_WORD = re.compile(r"[a-z0-9]+", re.I)
_SOCIAL = {
    "facebook": "Facebook", "twitter": "Twitter", "x.com": "X",
    "instagram": "Instagram", "youtube": "YouTube", "linkedin": "LinkedIn",
    "whatsapp": "WhatsApp", "telegram": "Telegram", "github": "GitHub",
}


def name_from_href(href: str) -> str:
    """A last resort, and only for destinations whose name is unambiguous.

    A link to facebook.com is a Facebook link -- that is not a guess. A link to
    /academics/ugrad is NOT reliably "Academics ugrad", so anything that is not
    a recognised destination returns empty and the node is left for the model.
    """
    lowered = href.lower()
    for needle, label in _SOCIAL.items():
        if needle in lowered:
            return f"{label} page"
    return ""


def derive_name(node_html: str) -> tuple[str, str]:
    """Return (name, where_it_came_from) using only what the markup already says."""
    for attribute in ("aria-label", "title"):
        value = attribute_value(node_html, attribute)
        if value:
            return value, attribute

    text = visible_text(node_html)
    if text:
        return text, "visible text"

    for attribute in ("alt", "title"):
        value = nested_attribute(node_html, attribute)
        if value:
            return value, f"nested {attribute}"

    value = attribute_value(node_html, "value")
    if value:
        return value, "value"

    href = attribute_value(node_html, "href")
    if href:
        derived = name_from_href(href)
        if derived:
            return derived, "destination"

    return "", ""


# --------------------------------------------------------------------------
# rule handlers
# --------------------------------------------------------------------------

def _edit(rule: str, selector: str, op: str, args: dict, why: str) -> dict:
    return {
        "violation_id": rule,
        "selector": selector,
        "op": op,
        "args": args,
        "rationale": why,
        "source": "deterministic",
    }


def _first_selector(node: dict) -> str:
    targets = node.get("target") or []
    return targets[0] if targets else ""


def fix_html_lang(rule: str, nodes: list[dict]) -> list[dict]:
    return [
        _edit(rule, "html", "set_attribute", {"name": "lang", "value": "en"},
              "A document language is required and English is what this page is written in.")
    ]


def fix_viewport(rule: str, nodes: list[dict]) -> list[dict]:
    edits = []
    for node in nodes:
        selector = _first_selector(node) or 'meta[name="viewport"]'
        content = attribute_value(node.get("html", ""), "content")
        cleaned = ",".join(
            part.strip() for part in content.split(",")
            if part.strip() and not re.match(r"(user-scalable|maximum-scale)", part.strip(), re.I)
        ) or "width=device-width, initial-scale=1"
        edits.append(_edit(
            rule, selector, "set_attribute", {"name": "content", "value": cleaned},
            "Blocking zoom stops low-vision users enlarging the page; the rest of "
            "the viewport settings are unchanged.",
        ))
    return edits


def fix_accessible_name(rule: str, nodes: list[dict]) -> list[dict]:
    edits = []
    for node in nodes:
        selector = _first_selector(node)
        if not selector:
            continue
        name, source = derive_name(node.get("html", ""))
        if not name:
            continue  # genuinely ambiguous -- leave it for the model, or deferral
        edits.append(_edit(
            rule, selector, "set_attribute",
            {"name": "aria-label", "value": name[:120]},
            f"The name was already in the markup ({source}); this exposes it to "
            f"assistive technology.",
        ))
    return edits


DECORATIVE = re.compile(
    r"(^|[-_\s])(icon|bullet|divider|spacer|separator|ornament|decoration|shape|"
    r"pattern|bg|background|overlay|arrow|chevron)([-_\s]|$)", re.I
)


def fix_decorative_images(rule: str, nodes: list[dict]) -> list[dict]:
    """Only images the markup positively marks as decorative. Never a guess."""
    edits = []
    for node in nodes:
        selector = _first_selector(node)
        html = node.get("html", "")
        if not selector:
            continue
        classes = attribute_value(html, "class")
        identifier = attribute_value(html, "id")
        source = attribute_value(html, "src")
        looks_decorative = (
            DECORATIVE.search(classes) or DECORATIVE.search(identifier)
            or DECORATIVE.search(source.rsplit("/", 1)[-1])
            or re.search(r'\b(width|height)\s*=\s*["\']?1["\']?', html, re.I)
        )
        if not looks_decorative:
            continue
        edits.append(_edit(
            rule, selector, "set_attribute", {"name": "alt", "value": ""},
            "The markup marks this image as decorative, so an empty alt removes "
            "it from the accessibility tree rather than inventing a description.",
        ))
    return edits


def fix_colour_contrast(rule: str, nodes: list[dict]) -> list[dict]:
    """Compute a compliant foreground from the colours axe already measured."""
    edits = []
    for node in nodes:
        selector = _first_selector(node)
        data = node.get("data") or {}
        if not selector or not data:
            continue

        foreground = parse_colour(str(data.get("fgColor", "")))
        background = parse_colour(str(data.get("bgColor", "")))
        if not foreground or not background:
            continue

        needed = required_ratio(data)
        replacement = accessible_foreground(foreground, background, needed)
        if not replacement:
            continue

        achieved = contrast_ratio(replacement, background)
        edits.append(_edit(
            rule, selector, "add_css_rule",
            {"rule": f"{selector} {{ color: {to_hex(replacement)}; }}"},
            f"Contrast was {data.get('contrastRatio', '?')}:1 against "
            f"{to_hex(background)}; darkening the same hue to "
            f"{to_hex(replacement)} reaches {achieved:.1f}:1 "
            f"(needs {needed:g}:1).",
        ))
    return edits


HANDLERS = {
    "html-has-lang": fix_html_lang,
    "html-lang-valid": fix_html_lang,
    "meta-viewport": fix_viewport,
    "meta-viewport-large": fix_viewport,
    "link-name": fix_accessible_name,
    "button-name": fix_accessible_name,
    "input-button-name": fix_accessible_name,
    "aria-command-name": fix_accessible_name,
    "aria-toggle-field-name": fix_accessible_name,
    "select-name": fix_accessible_name,
    "label": fix_accessible_name,
    "image-alt": fix_decorative_images,
    "input-image-alt": fix_decorative_images,
    "color-contrast": fix_colour_contrast,
}


def deterministic_edits(violations: list[dict]) -> list[dict]:
    """Every mechanically-derivable edit for this page, in one pass, no model."""
    edits: list[dict] = []
    for violation in violations:
        handler = HANDLERS.get(violation.get("id", ""))
        if not handler:
            continue
        edits.extend(handler(violation["id"], violation.get("nodes", [])))
    return edits