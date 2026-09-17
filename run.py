#!/usr/bin/env python3
"""
Run the whole pipeline locally against one URL. No S3, no Lambda, no Step
Functions. This is what you develop against all of Thursday and Friday.

    python local_run.py https://example.ac.in/ --out out/
    python local_run.py --fixture evals/fixtures/example.html --out out/
    python local_run.py https://example.ac.in/ --no-agent      # audit only

Artifacts written to --out:
    original.html   the mirrored page, before any edits
    patched.html    after the agent
    report.json     violations, edits, deferrals, scores, tokens
    run.log         the agent's step log
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from audit.mirror import mirror
from audit.runner import audit_html
from audit.scorer import compare, score_from_violations


def extract_title(html_text: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "lxml")
    title_tag = soup.find("title")
    return title_tag.get_text(strip=True) if title_tag else ""


def audit_only(html_text: str) -> None:
    result = audit_html(html_text, take_screenshot=False)
    score = score_from_violations(result.violations)
    print(f"\nscore: {score}/100   ({len(result.violations)} rules failing)\n")
    print(f"{'rule':<32} {'impact':<10} {'nodes':>6}")
    print("-" * 52)
    for violation in sorted(
        result.violations, key=lambda v: v.get("totalNodes", 0), reverse=True
    ):
        print(f"{violation['id']:<32} {violation['impact'] or '':<10} {violation['totalNodes']:>6}")


def run_agent(html_text: str, page_url: str, output_directory: Path) -> dict:
    from agent.graph import build_graph

    graph = build_graph()
    run_id = uuid.uuid4().hex[:12]

    initial_state = {
        "run_id": run_id,
        "page_url": page_url,
        "page_title": extract_title(html_text),
        "original_html": html_text,
        "log": [],
    }

    started_at = time.time()
    final_state = graph.invoke(initial_state, config={"recursion_limit": 100})
    elapsed_seconds = time.time() - started_at

    delta = compare(final_state["original_violations"], final_state["final_violations"])
    usage = final_state.get("token_usage")

    (output_directory / "patched.html").write_text(final_state["working_html"], encoding="utf-8")
    (output_directory / "run.log").write_text("\n".join(final_state.get("log", [])), encoding="utf-8")

    report = {
        "run_id": run_id,
        "page_url": page_url,
        "page_title": final_state.get("page_title", ""),
        "elapsed_seconds": round(elapsed_seconds, 1),
        "score_before": delta.score_before,
        "score_after": delta.score_after,
        "fixed_rules": delta.fixed_rules,
        "partially_fixed": delta.partially_fixed,
        "unchanged_rules": delta.unchanged_rules,
        "introduced_rules": delta.introduced_rules,
        "unfixed_rules": final_state.get("unfixed_rules", []),
        "cluster_reports": final_state.get("cluster_reports", []),
        "accepted_edits": final_state.get("accepted_edits", []),
        "deferred_items": final_state.get("deferred_items", []),
        "original_violations": final_state["original_violations"],
        "final_violations": final_state["final_violations"],
        "tokens": {
            "input": usage.input_tokens if usage else 0,
            "output": usage.output_tokens if usage else 0,
            "calls": usage.calls if usage else 0,
        },
    }
    (output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n".join(final_state.get("log", [])))
    print("\n" + "=" * 60)
    print(f"  {delta.summary_line()}")
    print(f"  deferred to human: {len(report['deferred_items'])}")
    print(f"  edits applied:     {len(report['accepted_edits'])}")
    print(f"  model calls:       {report['tokens']['calls']}  "
          f"(in={report['tokens']['input']} out={report['tokens']['output']})")
    print(f"  wall clock:        {report['elapsed_seconds']}s")
    print("=" * 60)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", help="URL to mirror and fix")
    parser.add_argument("--fixture", help="Use a frozen local HTML file instead of fetching")
    parser.add_argument("--out", default="out", help="Output directory")
    parser.add_argument("--no-agent", action="store_true", help="Audit only, no model calls")
    arguments = parser.parse_args()

    if not arguments.url and not arguments.fixture:
        parser.error("give a URL or --fixture")

    output_directory = Path(arguments.out)
    output_directory.mkdir(parents=True, exist_ok=True)

    if arguments.fixture:
        html_text = Path(arguments.fixture).read_text(encoding="utf-8")
        page_url = f"file://{Path(arguments.fixture).resolve()}"
        print(f"fixture: {arguments.fixture} ({len(html_text)} bytes)")
    else:
        mirrored = mirror(arguments.url)
        html_text = mirrored.html
        page_url = mirrored.final_url
        print(f"mirrored: {mirrored.final_url} [{mirrored.status_code}] ({len(html_text)} bytes)")
        for note in mirrored.notes:
            print(f"  note: {note}")

    (output_directory / "original.html").write_text(html_text, encoding="utf-8")

    if arguments.no_agent:
        audit_only(html_text)
        return 0

    run_agent(html_text, page_url, output_directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
