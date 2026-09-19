"""
Run axe-core against a page using Playwright/Chromium.

Two entry points:
  audit_url(url)         -- load a real URL (live site, S3 object, file://)
  audit_html(html_text)  -- write to a temp file and load it via file://

audit_html is what the patched copy goes through, so we never need to upload
before we can score. That keeps the inner agent loop fast and offline.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import ViewportSize, sync_playwright

# Resolved as a Path so the value is unambiguous wherever it came from -- an
# env var (a string) in the container, or the vendored copy next to this file
# during local runs. Playwright's add_script_tag has accepted both str and Path
# across versions, so the call site passes str(...) explicitly rather than
# depending on which one is installed.
AXE_SCRIPT_PATH = Path(
    os.environ.get(
        "AXE_SCRIPT_PATH",
        Path(__file__).resolve().parent.parent / "vendor" / "axe.min.js",
    )
).resolve()


def axe_script_path() -> Path:
    """The axe-core bundle, or a clear error naming where we looked.

    Without this the failure is a Playwright error about a script tag, several
    frames from the actual problem: vendor/ not copied into the image, or
    AXE_SCRIPT_PATH pointing somewhere that does not exist.
    """
    if not AXE_SCRIPT_PATH.is_file():
        raise FileNotFoundError(
            f"axe-core not found at {AXE_SCRIPT_PATH}.\n"
            f"Set AXE_SCRIPT_PATH, or restore the vendored copy:\n"
            f"  npm pack axe-core@4.10.2 && tar xzf axe-core-4.10.2.tgz\n"
            f"  cp package/axe.min.js vendor/axe.min.js"
        )
    return AXE_SCRIPT_PATH

# Flags that are safe everywhere.
BASE_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

# Lambda gives one constrained container with no process supervisor, so Chromium
# has to run flat. On a normal desktop these same flags make the renderer crash
# -- which surfaces as TargetClosedError on the next call, not as a launch error.
LAMBDA_ONLY_CHROMIUM_ARGS = [
    "--single-process",
    "--no-zygote",
]


def running_in_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def scaled(timeout_ms: int) -> int:
    """Stretch a timeout when the code is running somewhere slow."""
    if running_in_lambda():
        return timeout_ms * LAMBDA_TIMEOUT_MULTIPLIER
    return timeout_ms


def chromium_launch_args() -> list[str]:
    """Force either mode with A11Y_CHROMIUM_MODE=lambda|desktop."""
    mode = os.environ.get("A11Y_CHROMIUM_MODE", "").strip().lower()
    if mode == "desktop":
        return list(BASE_CHROMIUM_ARGS)
    if mode == "lambda" or running_in_lambda():
        return BASE_CHROMIUM_ARGS + LAMBDA_ONLY_CHROMIUM_ARGS
    return list(BASE_CHROMIUM_ARGS)


# Kept as a name for callers that import it (scripts/smoke_image.py).
CHROMIUM_LAUNCH_ARGS = chromium_launch_args()

VIEWPORT: ViewportSize = {"width": 1280, "height": 900}

# Lambda is slower than a laptop at everything Chromium does: cold filesystem,
# shared CPU, and a container that may be carrying a previous invocation's
# leftovers. The same timeouts that are generous locally are marginal there.
LAMBDA_TIMEOUT_MULTIPLIER = 2

NAVIGATION_TIMEOUT_MS = 30_000

# One audit failing should not discard a run that is already minutes deep, so
# the whole audit is retried once with a fresh browser.
AUDIT_ATTEMPTS = 2

# Real pages pull 100+ subresources from the origin. Waiting for "load" means
# waiting for the slowest one, and a single hanging asset stalls forever. We
# wait for the DOM, then give the network a bounded chance to settle.
DOM_TIMEOUT_MS = 20_000

# Resource types aborted during an audit.
#
# axe-core evaluates the DOM and computed CSS. It does not need the page's own
# JavaScript -- and synchronous <script src> tags from the origin BLOCK HTML
# parsing, which is what stops DOMContentLoaded from ever firing on real sites.
# Blocking them is the difference between a 20s timeout and a 3s audit.
#
# The cost: content injected by client-side JS will not be seen. That is a
# documented limitation, not a silent one -- it lands in AuditResult.notes.
# "font" is here because Playwright's screenshot waits on document.fonts.ready,
# and a webfont the origin never serves hangs it for the full timeout. axe reads
# font-size and font-weight from computed CSS, not from the font file, so the
# contrast thresholds are unaffected by blocking the download.
BLOCKED_RESOURCE_TYPES = {
    "script",
    "media",
    "font",
    "websocket",
    "eventsource",
    "other",
}

# Screenshots are a nice-to-have for the report. They must never fail a run.
SCREENSHOT_FULL_PAGE_TIMEOUT_MS = 8_000
SCREENSHOT_VIEWPORT_TIMEOUT_MS = 5_000
NETWORK_SETTLE_TIMEOUT_MS = 8_000
POST_SETTLE_PAUSE_MS = 1_000

# axe.run options. We restrict to the WCAG tagsets so the violation list stays
# actionable and does not fill up with best-practice noise.
AXE_RUN_OPTIONS = {
    "runOnly": {
        "type": "tag",
        "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"],
    },
    "resultTypes": ["violations"],
}

AXE_RUN_SCRIPT = """
async (options) => {
    const results = await axe.run(document, options);
    return {
        violations: results.violations,
        passCount: results.passes ? results.passes.length : 0,
        incompleteCount: results.incomplete ? results.incomplete.length : 0,
        testEngine: results.testEngine,
    };
}
"""


@dataclass
class AuditResult:
    violations: list[dict]
    pass_count: int
    incomplete_count: int
    screenshot_png: bytes | None = None
    engine_version: str = ""
    notes: list[str] = field(default_factory=list)


def _try_screenshot(page) -> bytes | None:
    """Full page, then viewport, then give up. Never raises.

    Playwright waits for document.fonts.ready before capturing. A webfont the
    origin refuses to serve will hang that wait for the whole timeout, so both
    attempts are short and failure is simply no screenshot.
    """
    for full_page, timeout_ms in (
        (True, SCREENSHOT_FULL_PAGE_TIMEOUT_MS),
        (False, SCREENSHOT_VIEWPORT_TIMEOUT_MS),
    ):
        try:
            return page.screenshot(
                full_page=full_page, timeout=scaled(timeout_ms), animations="disabled"
            )
        except PlaywrightError:
            continue
    return None


def _trim_violation(violation: dict, max_nodes: int) -> dict:
    """axe node payloads are enormous. Keep only what the agent and UI need."""
    trimmed_nodes = []
    for node in violation.get("nodes", [])[:max_nodes]:
        trimmed_nodes.append(
            {
                "target": node.get("target", []),
                "html": (node.get("html") or "")[:1200],
                "failureSummary": (node.get("failureSummary") or "")[:600],
            }
        )
    return {
        "id": violation.get("id"),
        "impact": violation.get("impact"),
        "help": violation.get("help"),
        "description": violation.get("description"),
        "helpUrl": violation.get("helpUrl"),
        "totalNodes": len(violation.get("nodes", [])),
        "nodes": trimmed_nodes,
    }


def _run_axe_on_page(page, take_screenshot: bool, max_nodes_per_violation: int) -> AuditResult:
    # str(): add_script_tag's accepted types have varied across Playwright
    # versions, and a str works in all of them.
    page.add_script_tag(path=str(axe_script_path()))
    raw = page.evaluate(AXE_RUN_SCRIPT, AXE_RUN_OPTIONS)

    screenshot_png = _try_screenshot(page) if take_screenshot else None

    return AuditResult(
        violations=[
            _trim_violation(violation, max_nodes_per_violation)
            for violation in raw["violations"]
        ],
        pass_count=raw["passCount"],
        incomplete_count=raw["incompleteCount"],
        screenshot_png=screenshot_png,
        engine_version=(raw.get("testEngine") or {}).get("version", ""),
    )


def audit_url(
    url: str,
    take_screenshot: bool = True,
    max_nodes_per_violation: int = 25,
    block_scripts: bool = True,
) -> AuditResult:
    notes: list[str] = []
    blocked_counter: dict = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=chromium_launch_args())
        context = browser.new_context(viewport=VIEWPORT, ignore_https_errors=True)
        if block_scripts:
            _block_heavy_resources(context, blocked_counter)
        page = context.new_page()
        try:
            try:
                page.goto(url, wait_until="networkidle", timeout=scaled(NAVIGATION_TIMEOUT_MS))
            except PlaywrightError:
                notes.append("networkidle timed out; fell back to domcontentloaded")
                page.goto(url, wait_until="domcontentloaded", timeout=scaled(NAVIGATION_TIMEOUT_MS))
                _settle(page, notes)
            page.wait_for_timeout(POST_SETTLE_PAUSE_MS)
            result = _run_axe_on_page(page, take_screenshot, max_nodes_per_violation)
            result.notes = notes
            return result
        finally:
            browser.close()


def _block_heavy_resources(context, blocked_counter: dict) -> None:
    """Abort parser-blocking and irrelevant subresources.

    Registered for http/https ONLY. A "**/*" pattern also matches the file://
    navigation that loads the mirrored page, and continuing a file:// request
    through the routing layer stalls the navigation -- the page then never
    reaches domcontentloaded and goto times out.
    """

    def handler(route, request):
        if request.resource_type in BLOCKED_RESOURCE_TYPES:
            blocked_counter[request.resource_type] = (
                blocked_counter.get(request.resource_type, 0) + 1
            )
            route.abort()
        else:
            route.continue_()

    context.route("http://**", handler)
    context.route("https://**", handler)


def _settle(page, notes: list[str]) -> None:
    """Give the page a bounded chance to finish loading, then move on."""
    try:
        page.wait_for_load_state("networkidle", timeout=scaled(NETWORK_SETTLE_TIMEOUT_MS))
    except PlaywrightError:
        notes.append("network did not settle; audited the page as rendered")
    page.wait_for_timeout(POST_SETTLE_PAUSE_MS)


def audit_html(
    html_text: str,
    base_url: str | None = None,
    take_screenshot: bool = True,
    max_nodes_per_violation: int = 25,
    block_scripts: bool = True,
) -> AuditResult:
    """Audit an HTML string.

    The string is written to a temp file and loaded with goto(file://...) rather
    than injected with set_content. set_content waits for a lifecycle event on a
    frame it has populated by hand, and on a real page with synchronous external
    scripts that event never arrives.
    """
    temp_directory = tempfile.mkdtemp(prefix="a11y-audit-")
    temp_file = Path(temp_directory) / "page.html"
    temp_file.write_text(html_text, encoding="utf-8")

    try:
        last_error: Exception | None = None
        for attempt in range(AUDIT_ATTEMPTS):
            try:
                return _audit_file(
                    temp_file, take_screenshot, max_nodes_per_violation, block_scripts
                )
            except PlaywrightError as error:
                # A warm Lambda container can be carrying a previous
                # invocation's Chromium. Retrying with a brand new browser
                # clears that, and is far cheaper than losing the whole run and
                # having Step Functions redo the mirror and every audit.
                last_error = error
                if attempt + 1 < AUDIT_ATTEMPTS:
                    print(
                        f"audit attempt {attempt + 1}/{AUDIT_ATTEMPTS} failed "
                        f"({type(error).__name__}); retrying with a fresh browser",
                        flush=True,
                    )
        raise RuntimeError(f"audit failed after {AUDIT_ATTEMPTS} attempts") from last_error
    finally:
        temp_file.unlink(missing_ok=True)
        Path(temp_directory).rmdir()


def _audit_file(
    temp_file: Path,
    take_screenshot: bool,
    max_nodes_per_violation: int,
    block_scripts: bool,
) -> AuditResult:
    """One audit attempt against an on-disk page, with its own browser."""
    notes: list[str] = []
    blocked_counter: dict = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=chromium_launch_args())
        context = browser.new_context(viewport=VIEWPORT, ignore_https_errors=True)
        if block_scripts:
            _block_heavy_resources(context, blocked_counter)

        page = context.new_page()
        try:
            try:
                page.goto(temp_file.as_uri(), wait_until="domcontentloaded",
                          timeout=scaled(DOM_TIMEOUT_MS))
            except PlaywrightError:
                # The document is already in the page even when the lifecycle
                # event is late, so carry on and audit what rendered rather than
                # navigating a second time.
                notes.append("domcontentloaded timed out; audited the DOM as rendered")

            _settle(page, notes)

            if blocked_counter:
                summary = ", ".join(f"{count} {kind}" for kind, count in blocked_counter.items())
                notes.append(f"blocked during audit: {summary}")

            result = _run_axe_on_page(page, take_screenshot, max_nodes_per_violation)
            result.notes = notes
            return result
        finally:
            try:
                browser.close()
            except PlaywrightError:
                # Already gone. Never let cleanup mask the real failure.
                pass