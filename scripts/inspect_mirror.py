#!/usr/bin/env python3
"""
Answer the question the score cannot: did the mirrored page actually RENDER?

    python scripts/inspect_mirror.py https://www.iiita.ac.in/

A high score on a real site usually means axe found nothing to look at, not that
the site is accessible. This renders the mirror in a real browser and reports
what the browser actually got: stylesheets applied, images loaded, visible text,
computed background colour. It also writes a screenshot you can eyeball.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from audit.mirror import mirror
from audit.runner import BLOCKED_RESOURCE_TYPES, chromium_launch_args

PROBE = """
() => {
    const sheets = Array.from(document.styleSheets);
    let ruleCount = 0;
    let blockedSheets = 0;
    for (const sheet of sheets) {
        try { ruleCount += sheet.cssRules.length; }
        catch (error) { blockedSheets += 1; }
    }
    const images = Array.from(document.images);
    return {
        sheets: sheets.length,
        ruleCount,
        blockedSheets,
        images: images.length,
        imagesLoaded: images.filter(i => i.complete && i.naturalWidth > 0).length,
        elements: document.querySelectorAll('*').length,
        visibleText: (document.body ? document.body.innerText : '').trim().length,
        bodyBg: getComputedStyle(document.body).backgroundColor,
        bodyFont: getComputedStyle(document.body).fontFamily,
        scrollHeight: document.body ? document.body.scrollHeight : 0,
    };
}
"""


def main() -> int:
    url = sys.argv[1]
    output = Path(sys.argv[2] if len(sys.argv) > 2 else "out")
    output.mkdir(parents=True, exist_ok=True)

    mirrored = mirror(url)
    (output / "original.html").write_text(mirrored.html, encoding="utf-8")
    print(f"mirrored {mirrored.final_url} -> {len(mirrored.html)} bytes")
    for note in mirrored.notes:
        print(f"  {note}")

    page_file = Path(tempfile.mkdtemp(prefix="a11y-inspect-")) / "page.html"
    page_file.write_text(mirrored.html, encoding="utf-8")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=chromium_launch_args())
        context = browser.new_context(viewport={"width": 1280, "height": 900},
                                      ignore_https_errors=True)

        # Mirror what the audit does, so this measures the same page the scorer sees.
        blocked = {}

        def route_handler(route, request):
            if request.resource_type in BLOCKED_RESOURCE_TYPES:
                blocked[request.resource_type] = blocked.get(request.resource_type, 0) + 1
                route.abort()
            else:
                route.continue_()

        # http/https only -- a "**/*" pattern swallows the file:// navigation.
        context.route("http://**", route_handler)
        context.route("https://**", route_handler)
        page = context.new_page()
        failed_requests = []
        page.on("requestfailed", lambda request: failed_requests.append(
            (request.resource_type, request.url[:90])))

        try:
            page.goto(page_file.as_uri(), wait_until="domcontentloaded", timeout=20000)
        except Exception:
            print("  (domcontentloaded late; inspecting what rendered)")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            print("  (network never settled)")
        page.wait_for_timeout(1500)

        stats = page.evaluate(PROBE)

        screenshot_path = output / "mirror-check.png"
        captured = False
        for full_page, timeout_ms in ((True, 8000), (False, 5000)):
            try:
                page.screenshot(path=str(screenshot_path), full_page=full_page,
                                timeout=timeout_ms, animations="disabled")
                captured = True
                break
            except Exception:
                continue
        if not captured:
            print("  (screenshot failed; page never finished painting)")
        browser.close()

    print(f"\n  elements        {stats['elements']}")
    print(f"  visible text    {stats['visibleText']} chars")
    print(f"  page height     {stats['scrollHeight']}px")
    print(f"  stylesheets     {stats['sheets']} ({stats['ruleCount']} rules, "
          f"{stats['blockedSheets']} unreadable)")
    print(f"  images          {stats['imagesLoaded']}/{stats['images']} loaded")
    print(f"  body background {stats['bodyBg']}")
    print(f"  body font       {stats['bodyFont'][:60]}")

    if blocked:
        print("\n  deliberately blocked: "
              + ", ".join(f"{count} {kind}" for kind, count in blocked.items()))

    if failed_requests:
        print(f"\n  {len(failed_requests)} failed requests, first 8:")
        for resource_type, failed_url in failed_requests[:8]:
            print(f"    {resource_type:12} {failed_url}")

    print()
    # font-family and background are the honest test: a stylesheet can be present
    # but cross-origin (unreadable rules) while still applying, or present and
    # refused. What the body actually computes to tells you which.
    body_font = stats["bodyFont"].lower()
    unstyled_body = body_font.startswith('"times') or body_font.startswith("times")

    if unstyled_body:
        print("  VERDICT: the theme CSS is NOT applied -- body is still the browser")
        print("           default serif. Any score from this page is fiction.")
        if stats["blockedSheets"]:
            print(f"           {stats['blockedSheets']} stylesheet(s) are still cross-origin")
            print("           links rather than inlined <style> blocks.")
        print("           Check the 'inlined N/M' note above: M is how many the page has.")
    elif stats["imagesLoaded"] == 0 and stats["images"] > 0:
        print("  VERDICT: styled, but no image loaded. Usually lazy-loading that needs")
        print("           the page's own JS (which the audit blocks), not hotlinking.")
        print("           image-alt is unaffected: the <img> elements are in the DOM.")
    elif stats["visibleText"] < 200:
        print("  VERDICT: page rendered nearly empty. Content is probably JS-injected.")
    else:
        print("  VERDICT: looks like a real render. Compare the screenshot to the live site.")

    if captured:
        print(f"\n  screenshot: {screenshot_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())