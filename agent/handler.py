"""
Lambda entry point for one page. Invoked by the Step Functions Map state.

Event:  {"runId": "...", "url": "https://..."}
Result: {"runId", "pageId", "scoreBefore", "scoreAfter", "status"}
"""

from __future__ import annotations

import hashlib
import logging
import time

from agent.graph import build_graph, initial_state
from audit.mirror import mirror
from audit.scorer import compare
from storage import dynamo_store, s3_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def page_id_for(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def extract_title(html_text: str) -> str:
    from bs4 import BeautifulSoup

    title_tag = BeautifulSoup(html_text, "lxml").find("title")
    return title_tag.get_text(strip=True) if title_tag else ""


def lambda_handler(event: dict, context) -> dict:
    run_id = event["runId"]
    url = event["url"]
    page_id = page_id_for(url)
    started_at = time.time()

    logger.info("run=%s page=%s url=%s", run_id, page_id, url)

    mirrored = mirror(url)
    original_key = s3_store.put_html(run_id, page_id, "original", mirrored.html)

    dynamo_store.put_page(
        run_id,
        page_id,
        {
            "url": url,
            "finalUrl": mirrored.final_url,
            "title": extract_title(mirrored.html),
            "status": "running",
            "originalKey": original_key,
            "mirrorNotes": mirrored.notes,
        },
    )

    graph = build_graph()

    # Stream rather than invoke, so the dashboard has something to show while a
    # multi-minute run is in flight. Each node's log line is written straight to
    # DynamoDB; the poller picks it up on its next pass. Without this the page
    # row is only written once at the very end and the UI can do nothing but
    # say "0 of 1 done" for several minutes.
    final_state: dict = {}
    progress_log: list[str] = []

    def record_progress(phase: str, done: int, total: int) -> None:
        dynamo_store.update_page_progress(
            run_id,
            page_id,
            {
                "phase": phase,
                "clustersDone": done,
                "clustersTotal": total,
                "log": progress_log[-40:],
                "updatedAt": int(time.time()),
            },
        )

    record_progress("auditing the original page", 0, 0)

    for update in graph.stream(
        initial_state(
            run_id=run_id,
            page_url=mirrored.final_url,
            page_title=extract_title(mirrored.html),
            original_html=mirrored.html,
        ),
        config={"recursion_limit": 100},
        stream_mode="values",
    ):
        final_state = update

        new_lines = update.get("log", [])[len(progress_log):]
        if new_lines:
            progress_log.extend(new_lines)

        clusters = update.get("clusters") or []
        done = min(update.get("cluster_index", 0), len(clusters))
        phase = progress_log[-1] if progress_log else "starting"

        if new_lines:
            logger.info("progress: %s", phase)
            record_progress(phase, done, len(clusters))

    patched_key = s3_store.put_html(run_id, page_id, "patched", final_state["working_html"])
    delta = compare(final_state["original_violations"], final_state["final_violations"])
    usage = final_state["token_usage"]

    axe_key = s3_store.put_json(
        run_id,
        page_id,
        "axe",
        {
            "before": final_state["original_violations"],
            "after": final_state["final_violations"],
        },
    )

    # Identical shape to what local_run.py writes, so web/lib/api.ts has one
    # contract whether it is reading a local file or the deployed API.
    report_key = s3_store.put_json(
        run_id,
        page_id,
        "report",
        {
            "run_id": run_id,
            "page_url": mirrored.final_url,
            "page_title": final_state["page_title"],
            "elapsed_seconds": round(time.time() - started_at, 1),
            "score_before": delta.score_before,
            "score_after": delta.score_after,
            "fixed_rules": delta.fixed_rules,
            "partially_fixed": delta.partially_fixed,
            "unchanged_rules": delta.unchanged_rules,
            "introduced_rules": delta.introduced_rules,
            "unfixed_rules": final_state["unfixed_rules"],
            "deferred_rules": final_state["deferred_rules"],
            "reverted_rules": [
                report["rule"]
                for report in final_state["cluster_reports"]
                if report.get("status") == "reverted"
            ],
            "cluster_reports": final_state["cluster_reports"],
            "accepted_edits": final_state["accepted_edits"],
            "deferred_items": final_state["deferred_items"],
            "original_violations": final_state["original_violations"],
            "final_violations": final_state["final_violations"],
            "tokens": {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "calls": usage.calls,
            },
        },
    )

    # The dashboard consumes exactly one shape. This is byte-for-byte the same
    # object local_run.py writes to out/report.json, so the deployed UI and the
    # local UI share a single type and a single code path.
    report = {
        "run_id": run_id,
        "page_url": mirrored.final_url,
        "page_title": final_state["page_title"],
        "elapsed_seconds": round(time.time() - started_at, 1),
        "score_before": delta.score_before,
        "score_after": delta.score_after,
        "fixed_rules": delta.fixed_rules,
        "partially_fixed": delta.partially_fixed,
        "unchanged_rules": delta.unchanged_rules,
        "introduced_rules": delta.introduced_rules,
        "unfixed_rules": final_state["unfixed_rules"],
        "deferred_rules": final_state["deferred_rules"],
        "reverted_rules": [
            report_row["rule"]
            for report_row in final_state["cluster_reports"]
            if report_row.get("status") == "reverted"
        ],
        "cluster_reports": final_state["cluster_reports"],
        "accepted_edits": final_state["accepted_edits"],
        "deferred_items": final_state["deferred_items"],
        "original_violations": final_state["original_violations"],
        "final_violations": final_state["final_violations"],
        "tokens": {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "calls": usage.calls,
        },
        "log": final_state["log"],
    }
    report_key = s3_store.put_json(run_id, page_id, "reports", report)

    for report in final_state["cluster_reports"]:
        rule_id = report["rule"]
        dynamo_store.put_violation(
            run_id,
            page_id,
            rule_id,
            {
                "status": report["status"],
                "attempts": report["attempts"],
                "edits": [
                    edit for edit in final_state["accepted_edits"]
                    if edit.get("violation_id") == rule_id
                ],
                "deferred": [
                    item for item in final_state["deferred_items"]
                    if item.get("violation_id") == rule_id
                ],
            },
        )

    dynamo_store.put_page(
        run_id,
        page_id,
        {
            "url": url,
            "finalUrl": mirrored.final_url,
            "title": final_state["page_title"],
            "status": "done",
            "originalKey": original_key,
            "patchedKey": patched_key,
            "axeKey": axe_key,
            "reportKey": report_key,
            "reportKey": report_key,
            "scoreBefore": delta.score_before,
            "scoreAfter": delta.score_after,
            "fixedRules": delta.fixed_rules,
            "partiallyFixed": delta.partially_fixed,
            "unchangedRules": delta.unchanged_rules,
            "introducedRules": delta.introduced_rules,
            "unfixedRules": final_state["unfixed_rules"],
            "deferredCount": len(final_state["deferred_items"]),
            "editCount": len(final_state["accepted_edits"]),
            "tokensIn": usage.input_tokens,
            "tokensOut": usage.output_tokens,
            "modelCalls": usage.calls,
            "elapsedSeconds": round(time.time() - started_at, 1),
            "log": final_state["log"],
        },
    )

    return {
        "runId": run_id,
        "pageId": page_id,
        "scoreBefore": delta.score_before,
        "scoreAfter": delta.score_after,
        "status": "done",
    }