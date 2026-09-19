"""
Smoke test the built agent image before pushing 2 GB to ECR.

Run INSIDE the container:
    docker run --rm --entrypoint python agentfunction:agent /var/task/scripts/smoke_image.py

Checks, in order:
  1. Chromium launches at all (the AL2023 / ubuntu20.04-build gamble)
  2. axe-core injects and runs
  3. The scorer produces a number
Any missing shared library shows up here as a clear launch error rather than as
an opaque Lambda init timeout.
"""

import os
import sys

# Running `python /var/task/scripts/smoke_image.py` puts /var/task/scripts on
# sys.path, not /var/task, so `audit` and `agent` are invisible. The Lambda
# runtime adds LAMBDA_TASK_ROOT itself; a direct python invocation does not.
TASK_ROOT = os.environ.get("LAMBDA_TASK_ROOT", "/var/task")
if TASK_ROOT not in sys.path:
    sys.path.insert(0, TASK_ROOT)

BROKEN_PAGE = """<!DOCTYPE html><html><head><title>smoke</title></head><body>
<img src="a.gif" class="icon">
<a href="#">click here</a>
<input type="text" name="q">
</body></html>"""


def main() -> int:
    print(f"0. task root {TASK_ROOT}", flush=True)
    print("1. importing playwright ...", flush=True)
    from playwright.sync_api import sync_playwright

    print("2. launching chromium ...", flush=True)
    from audit.runner import CHROMIUM_LAUNCH_ARGS

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(args=CHROMIUM_LAUNCH_ARGS)
        except Exception as error:
            print(f"   LAUNCH FAILED: {error}")
            print("   A missing .so here means the dnf list in Dockerfile.agent is short.")
            return 1

        print(f"   ok, version {browser.version}", flush=True)
        page = browser.new_page()
        page.set_content(BROKEN_PAGE)

        print("3. injecting axe-core ...", flush=True)
        axe_path = os.environ.get("AXE_SCRIPT_PATH", "/var/task/vendor/axe.min.js")
        if not os.path.exists(axe_path):
            print(f"   MISSING {axe_path} -- vendor/ did not get COPYed")
            return 1
        page.add_script_tag(path=axe_path)

        results = page.evaluate("async () => await axe.run(document)")
        print(f"   ok, {len(results['violations'])} violations found", flush=True)

        print("4. screenshot ...", flush=True)
        image_bytes = page.screenshot(full_page=True)
        print(f"   ok, {len(image_bytes)} bytes", flush=True)

        browser.close()

    print("5. scoring ...", flush=True)
    from audit.scorer import score_from_violations

    trimmed = [
        {"id": v["id"], "impact": v["impact"], "nodes": v["nodes"]}
        for v in results["violations"]
    ]
    print(f"   score {score_from_violations(trimmed)}/100", flush=True)

    print("\nIMAGE OK -- safe to deploy")
    return 0


if __name__ == "__main__":
    sys.exit(main())