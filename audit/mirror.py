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
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "a11y-agent/0.1 (accessibility audit; +https://github.com/YOURNAME/a11y-agent)"

REQUEST_TIMEOUT_SECONDS = 20

# The page fetch is the most fragile step in the pipeline and the one furthest
# outside our control: one refused connection to a university site on a bad link
# used to kill the whole run before a single row was written.
PAGE_FETCH_ATTEMPTS = 3
PAGE_FETCH_BACKOFF_SECONDS = 3.0
RETRYABLE_PAGE_STATUS = {408, 425, 429, 500, 502, 503, 504}

CSS_URL_PATTERN = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)

CSS_IMPORT_PATTERN = re.compile(
    r"""@import\s+(?:url\(\s*)?['"]?([^'")\s;]+)['"]?\s*\)?\s*;""", re.IGNORECASE
)

# A mirrored page is loaded from a file:// URL, which Chromium treats as an
# opaque origin. Every stylesheet still sitting at https://origin/... is then
# cross-origin and is simply not applied -- the page renders in Times New Roman
# with no layout, and every accessibility score computed from it is fiction.
#
# So we fetch the stylesheets ourselves and inline them. After this the document
# needs nothing from the origin in order to render correctly.
# Drupal and WordPress sites routinely ship 20-40 separate stylesheets. A cap
# below that silently leaves the theme CSS as a cross-origin <link>, which does
# not apply -- the page renders unstyled and every score is fiction.
MAX_INLINED_STYLESHEETS = 50
MAX_STYLESHEET_BYTES = 2_000_000
STYLESHEET_TIMEOUT_SECONDS = 10


@dataclass
class MirroredPage:
    requested_url: str
    final_url: str
    status_code: int
    html: str
    notes: list[str] = field(default_factory=list)


class MirrorError(RuntimeError):
    """The page could not be fetched. Carries something worth reading."""


def fetch_page(url: str) -> tuple[str, int, str]:
    """Return (html_text, status_code, final_url_after_redirects).

    Retried, because this is the single most fragile step in the pipeline and
    the one furthest from our control. A university site on a wobbly link
    refuses one connection in five; without a retry here that is a dead run and
    a spinner, for a site that would have answered a second later.
    """
    last_error = "no attempt made"

    for attempt in range(PAGE_FETCH_ATTEMPTS):
        try:
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

            if response.status_code in RETRYABLE_PAGE_STATUS:
                last_error = f"HTTP {response.status_code} from the site"
            else:
                response.raise_for_status()
                return response.text, response.status_code, str(response.url)

        except httpx.HTTPStatusError as error:
            # 404, 403, 401 -- the site answered and said no. Retrying is rude
            # and pointless.
            raise MirrorError(
                f"{url} returned HTTP {error.response.status_code}. "
                f"The page is not publicly fetchable, so there is nothing to audit."
            ) from error

        except httpx.HTTPError as error:
            last_error = f"{type(error).__name__}: {error}"

        if attempt + 1 < PAGE_FETCH_ATTEMPTS:
            delay = PAGE_FETCH_BACKOFF_SECONDS * (2 ** attempt)
            print(
                f"mirror: {last_error} fetching {url} "
                f"(attempt {attempt + 1}/{PAGE_FETCH_ATTEMPTS}), retrying in {delay:.0f}s",
                flush=True,
            )
            time.sleep(delay)

    raise MirrorError(
        f"could not fetch {url} after {PAGE_FETCH_ATTEMPTS} attempts -- {last_error}.\n"
        f"The site is unreachable from here rather than broken in our code: check it "
        f"loads in a browser, then try a different page."
    )


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


def _neutralise_form_actions(soup: BeautifulSoup) -> None:
    """Forms are never submitted in our sandbox, but a relative action combined
    with <base> can send a stray request at the origin. Point them nowhere and
    keep the original action on a data attribute for the diff view."""
    for form_element in soup.find_all("form"):
        original_action = form_element.get("action", "")
        form_element["data-a11y-agent-original-action"] = original_action
        form_element["action"] = "javascript:void(0)"


def _fetch_stylesheet(
    client: httpx.Client, sheet_url: str, referer: str, failures: list[str] | None = None
) -> str | None:
    try:
        response = client.get(sheet_url, headers={"Referer": referer})
    except httpx.HTTPError as error:
        if failures is not None:
            failures.append(f"{sheet_url} -> {type(error).__name__}")
        return None

    if response.status_code != 200:
        if failures is not None:
            failures.append(f"{sheet_url} -> HTTP {response.status_code}")
        return None
    if len(response.content) > MAX_STYLESHEET_BYTES:
        if failures is not None:
            failures.append(f"{sheet_url} -> too large ({len(response.content)} bytes)")
        return None
    return response.text


def _inline_stylesheets(soup: BeautifulSoup, base_url: str, notes: list[str]) -> None:
    """Replace every <link rel=stylesheet> with a <style> holding its content."""
    link_tags = [
        tag
        for tag in soup.find_all("link")
        if "stylesheet" in " ".join(tag.get("rel") or []).lower() and tag.get("href")
    ]
    if not link_tags:
        return

    inlined_count = 0
    inlined_bytes = 0
    failures: list[str] = []

    with httpx.Client(
        follow_redirects=True,
        timeout=STYLESHEET_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept": "text/css,*/*;q=0.1"},
    ) as client:
        for link_tag in link_tags[:MAX_INLINED_STYLESHEETS]:
            sheet_url = urljoin(base_url, link_tag["href"])
            css_text = _fetch_stylesheet(client, sheet_url, base_url, failures)

            if css_text is None:
                link_tag.decompose()
                continue

            # Resolve one level of @import, then absolutise every url().
            for import_target in CSS_IMPORT_PATTERN.findall(css_text):
                import_url = urljoin(sheet_url, import_target)
                imported = _fetch_stylesheet(client, import_url, base_url)
                if imported:
                    css_text = _rewrite_css_urls(imported, import_url) + "\n" + css_text
            css_text = CSS_IMPORT_PATTERN.sub("", css_text)
            css_text = _rewrite_css_urls(css_text, sheet_url)

            style_tag = soup.new_tag("style")
            style_tag.string = css_text
            style_tag["data-a11y-agent-inlined-from"] = sheet_url
            link_tag.replace_with(style_tag)
            inlined_count += 1
            inlined_bytes += len(css_text)

    notes.append(
        f"inlined {inlined_count}/{len(link_tags)} stylesheet(s), "
        f"{inlined_bytes} bytes of CSS"
    )
    for failure in failures[:6]:
        notes.append(f"  stylesheet failed: {failure}")
    if len(failures) > 6:
        notes.append(f"  ...and {len(failures) - 6} more stylesheet failures")
    if len(link_tags) > MAX_INLINED_STYLESHEETS:
        notes.append(
            f"NOT INLINED: {len(link_tags) - MAX_INLINED_STYLESHEETS} stylesheet(s) "
            f"over the cap of {MAX_INLINED_STYLESHEETS} -- page may render unstyled"
        )


def mirror(
    url: str,
    neutralise_forms: bool = True,
    inline_css: bool = True,
) -> MirroredPage:
    html_text, status_code, final_url = fetch_page(url)
    soup = BeautifulSoup(html_text, "lxml")
    notes: list[str] = []

    removed_csp = _strip_csp_meta_tags(soup)
    if removed_csp:
        notes.append(f"removed {removed_csp} CSP meta tag(s)")

    _ensure_base_tag(soup, final_url)

    if inline_css:
        _inline_stylesheets(soup, final_url, notes)

    rewritten_srcset = _rewrite_srcset_attributes(soup, final_url)
    if rewritten_srcset:
        notes.append(f"rewrote {rewritten_srcset} srcset attribute(s)")

    rewritten_styles = _rewrite_inline_styles(soup, final_url)
    if rewritten_styles:
        notes.append(f"rewrote url() in {rewritten_styles} style block(s)/attribute(s)")

    if neutralise_forms:
        _neutralise_form_actions(soup)

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