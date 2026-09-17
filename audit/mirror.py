"""
Fetch a live page and rewrite it so it renders identically when served from
somewhere else (S3, local disk). This is the load-bearing piece of the whole
project: if the mirrored copy does not render like the original, every score
we produce is meaningless.

Strategy, in order of importance:
  1. Inject <base href="<original url>"> so relative URLs resolve to the origin.
  2. Strip Content-Security-Policy <meta> tags, which would otherwise block the
     cross-origin CSS/JS that <base> now points at.
  3. Rewrite srcset and inline style url(...) by hand, because <base> handling
     for those is inconsistent across engines.
  4. Leave everything else alone. We are not building a site archiver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "a11y-agent/0.1 (accessibility audit; +https://github.com/YOURNAME/a11y-agent)"

REQUEST_TIMEOUT_SECONDS = 20

CSS_URL_PATTERN = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)


@dataclass
class MirroredPage:
    requested_url: str
    final_url: str
    status_code: int
    html: str
    notes: list[str] = field(default_factory=list)


def fetch_page(url: str) -> tuple[str, int, str]:
    """Return (html_text, status_code, final_url_after_redirects)."""
    with httpx.Client(
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        },
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text, response.status_code, str(response.url)


def _strip_csp_meta_tags(soup: BeautifulSoup) -> int:
    removed_count = 0
    for meta_tag in soup.find_all("meta"):
        http_equiv = (meta_tag.get("http-equiv") or "").lower()
        if http_equiv in {"content-security-policy", "content-security-policy-report-only"}:
            meta_tag.decompose()
            removed_count += 1
    return removed_count


def _ensure_base_tag(soup: BeautifulSoup, base_url: str) -> None:
    head = soup.find("head")
    if head is None:
        head = soup.new_tag("head")
        html_root = soup.find("html")
        if html_root is not None:
            html_root.insert(0, head)
        else:
            soup.insert(0, head)

    for existing_base in head.find_all("base"):
        existing_base.decompose()

    base_tag = soup.new_tag("base", href=base_url)
    head.insert(0, base_tag)


def _rewrite_srcset_attributes(soup: BeautifulSoup, base_url: str) -> int:
    rewritten_count = 0
    for element in soup.find_all(attrs={"srcset": True}):
        candidates = []
        for candidate in element["srcset"].split(","):
            parts = candidate.strip().split()
            if not parts:
                continue
            parts[0] = urljoin(base_url, parts[0])
            candidates.append(" ".join(parts))
        if candidates:
            element["srcset"] = ", ".join(candidates)
            rewritten_count += 1
    return rewritten_count


def _rewrite_css_urls(css_text: str, base_url: str) -> str:
    def replace(match: re.Match) -> str:
        quote_character, raw_url = match.group(1), match.group(2)
        if raw_url.startswith(("data:", "#", "http://", "https://", "//")):
            return match.group(0)
        return f"url({quote_character}{urljoin(base_url, raw_url)}{quote_character})"

    return CSS_URL_PATTERN.sub(replace, css_text)


def _rewrite_inline_styles(soup: BeautifulSoup, base_url: str) -> int:
    rewritten_count = 0

    for style_element in soup.find_all("style"):
        if style_element.string:
            style_element.string.replace_with(_rewrite_css_urls(style_element.string, base_url))
            rewritten_count += 1

    for element in soup.find_all(attrs={"style": True}):
        element["style"] = _rewrite_css_urls(element["style"], base_url)
        rewritten_count += 1

    return rewritten_count


def _absolutise_form_actions(soup: BeautifulSoup, base_url: str) -> None:
    """Forms are never submitted in our sandbox, but a relative action combined
    with <base> can send a stray request at the origin. Point them nowhere."""
    for form_element in soup.find_all("form"):
        form_element["action"] = "javascript:void(0)"
        form_element["data-a11y-agent-original-action"] = form_element.get("action", "")


def mirror(url: str, neutralise_forms: bool = True) -> MirroredPage:
    html_text, status_code, final_url = fetch_page(url)
    soup = BeautifulSoup(html_text, "lxml")
    notes: list[str] = []

    removed_csp = _strip_csp_meta_tags(soup)
    if removed_csp:
        notes.append(f"removed {removed_csp} CSP meta tag(s)")

    _ensure_base_tag(soup, final_url)

    rewritten_srcset = _rewrite_srcset_attributes(soup, final_url)
    if rewritten_srcset:
        notes.append(f"rewrote {rewritten_srcset} srcset attribute(s)")

    rewritten_styles = _rewrite_inline_styles(soup, final_url)
    if rewritten_styles:
        notes.append(f"rewrote url() in {rewritten_styles} style block(s)/attribute(s)")

    if neutralise_forms:
        _absolutise_form_actions(soup)

    if final_url != url:
        notes.append(f"redirected to {final_url}")

    return MirroredPage(
        requested_url=url,
        final_url=final_url,
        status_code=status_code,
        html=str(soup),
        notes=notes,
    )


def same_origin(first_url: str, second_url: str) -> bool:
    first, second = urlparse(first_url), urlparse(second_url)
    return (first.scheme, first.netloc) == (second.scheme, second.netloc)
