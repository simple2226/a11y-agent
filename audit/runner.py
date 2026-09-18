"""
Run axe-core against a page using Playwright/Chromium.

Two entry points:
  audit_url(url)         -- load a real URL (live site, S3 object, file://)
  audit_html(html_text)  -- load an HTML string via set_content

audit_html is what the patched copy goes through, so we never need to upload
before we can score. That keeps the inner agent loop fast and offline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import ViewportSize, sync_playwright

AXE_SCRIPT_PATH = os.environ.get(
    "AXE_SCRIPT_PATH",
    str(Path(__file__).resolve().parent.parent / "vendor" / "axe.min.js"),
)

# Chromium flags that matter inside Lambda / a slim container.
CHROMIUM_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    # "--single-process",
    "--no-zygote",
]

VIEWPORT: ViewportSize = {"width": 1280, "height": 900}

NAVIGATION_TIMEOUT_MS = 30_000

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
    page.add_script_tag(path=str(Path(AXE_SCRIPT_PATH).resolve()))
    raw = page.evaluate(AXE_RUN_SCRIPT, AXE_RUN_OPTIONS)

    screenshot_png = None
    if take_screenshot:
        try:
            screenshot_png = page.screenshot(full_page=True, timeout=15_000)
        except PlaywrightError:
            screenshot_png = page.screenshot(full_page=False)

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
) -> AuditResult:
    notes: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=CHROMIUM_LAUNCH_ARGS)
        context = browser.new_context(viewport=VIEWPORT, ignore_https_errors=True)
        page = context.new_page()
        try:
            try:
                page.goto(url, wait_until="networkidle", timeout=NAVIGATION_TIMEOUT_MS)
            except PlaywrightError:
                notes.append("networkidle timed out; fell back to domcontentloaded")
                page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            page.wait_for_timeout(1500)
            result = _run_axe_on_page(page, take_screenshot, max_nodes_per_violation)
            result.notes = notes
            return result
        finally:
            browser.close()


def audit_html(
    html_text: str,
    base_url: str | None = None,
    take_screenshot: bool = True,
    max_nodes_per_violation: int = 25,
) -> AuditResult:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=CHROMIUM_LAUNCH_ARGS)
        context = browser.new_context(
            viewport=VIEWPORT,
            ignore_https_errors=True,
            base_url=base_url,
        )
        page = context.new_page()
        try:
            page.set_content(html_text, wait_until="load", timeout=NAVIGATION_TIMEOUT_MS)
            page.wait_for_timeout(1500)
            return _run_axe_on_page(page, take_screenshot, max_nodes_per_violation)
        finally:
            browser.close()