"""
Lambda entry point for one page. Invoked by the Step Functions Map state.

Event:  {"runId": "...", "url": "https://..."}
Result: {"runId", "pageId", "scoreBefore", "scoreAfter", "status"}
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import traceback

from agent.graph import RUN_BUDGET_SECONDS, build_graph, initial_state
from audit.mirror import mirror
from audit.scorer import compare
from storage import dynamo_store, s3_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Time left at the end of the Lambda for the closing audit, the S3 writes and the
# DynamoDB writes. The fixing loop stops this far before the Lambda would be
# killed, so a run that goes long still produces a report.
SHUTDOWN_RESERVE_SECONDS = 180

# How often the heartbeat writes, while a single graph node is in flight. Nodes
# take tens of seconds (a Chromium audit) to minutes (a model call), and without
# this the page row's updatedAt freezes and the dashboard cannot tell a slow node
# from a dead Lambda.
HEARTBEAT_INTERVAL_SECONDS = 10


def page_id_for(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def budget_for(context) -> float:
    """Seconds the fixing loop may use, from whichever limit binds first."""
    try:
        remaining = context.get_remaining_time_in_millis() / 1000.0
    except AttributeError:  # local invocation, no Lambda context
        return RUN_BUDGET_SECONDS
    return max(60.0, min(RUN_BUDGET_SECONDS, remaining - SHUTDOWN_RESERVE_SECONDS))


def extract_title(html_text: str) -> str:
    from bs4 import BeautifulSoup

    title_tag = BeautifulSoup(html_text, "lxml").find("title")
    return title_tag.get_text(strip=True) if title_tag else ""


def lambda_handler(event: dict, context) -> dict:
    """Run one page, and make sure *something* is written either way.

    The failure path is the point of this wrapper. Without it a raised exception
    or a killed Lambda leaves the page row on status "running" for ever: the
    dashboard keeps polling, the spinner keeps spinning, and there is nothing
    anywhere that says what went wrong. In front of a judge that is
    indistinguishable from a product that does not work.
    """
    run_id = event["runId"]
    url = event["url"]
    page_id = page_id_for(url)
    started_at = time.time()

    logger.info("run=%s page=%s url=%s", run_id, page_id, url)

    # Written before anything can fail, so even a mirror error has a row to
    # attach itself to.
    dynamo_store.put_page(run_id, page_id, {"url": url, "status": "running"})

    try:
        return _run_page(run_id, page_id, url, started_at, context)
    except Exception as error:  # noqa: BLE001 -- re-raised below
        logger.exception("run=%s page=%s failed", run_id, page_id)
        _record_failure(run_id, page_id, url, error, started_at)
        raise


def _record_failure(run_id: str, page_id: str, url: str, error: Exception, started_at: float) -> None:
    """Best effort. A failure to record the failure must not mask the failure."""
    try:
        dynamo_store.update_page_fields(
            run_id,
            page_id,
            {
                "url": url,
                "status": "failed",
                "error": f"{type(error).__name__}: {error}"[:900],
                "errorTrace": traceback.format_exc()[-1500:],
                "elapsedSeconds": round(time.time() - started_at, 1),
                "failedAt": int(time.time()),
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record failure for run=%s page=%s", run_id, page_id)


def _next_phase(clusters: list, done: int, progress_log: list[str]) -> str:
    """A sentence about the work in flight, not the work just finished."""
    if done < len(clusters):
        rule_id = getattr(clusters[done], "rule_id", "the next rule")
        return f"fixing {rule_id} ({done + 1} of {len(clusters)}) — waiting on the model"
    if clusters:
        return "re-auditing the patched page"
    return progress_log[-1] if progress_log else "starting up"


def _run_page(run_id: str, page_id: str, url: str, started_at: float, context) -> dict:
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

    # What is happening *now*, as opposed to the log, which is what already
    # happened. A model call takes a minute or more and the last log line during
    # it reads "cluster: 3 cluster(s) queued" -- which looks like a hang.
    phase_state = {"phase": "auditing the original page", "done": 0, "total": 0}
    write_lock = threading.Lock()

    def record_progress() -> None:
        """One writer, so the heartbeat and the stream cannot interleave."""
        with write_lock:
            try:
                dynamo_store.update_page_progress(
                    run_id,
                    page_id,
                    {
                        "phase": phase_state["phase"],
                        "clustersDone": phase_state["done"],
                        "clustersTotal": phase_state["total"],
                        "log": progress_log[-40:],
                        "elapsedSeconds": int(time.time() - started_at),
                        "updatedAt": int(time.time()),
                    },
                )
            except Exception:  # noqa: BLE001
                # Progress is a convenience. Losing a write must not kill a run
                # that is otherwise going fine.
                logger.warning("progress write failed", exc_info=True)

    record_progress()

    # Heartbeat: keeps updatedAt moving while a single node is in flight, which
    # is what lets the dashboard distinguish "slow" from "dead".
    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(HEARTBEAT_INTERVAL_SECONDS):
            record_progress()

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    try:
        for update in graph.stream(
            initial_state(
                run_id=run_id,
                page_url=mirrored.final_url,
                page_title=extract_title(mirrored.html),
                original_html=mirrored.html,
                budget_seconds=budget_for(context),
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

            phase_state["done"] = done
            phase_state["total"] = len(clusters)
            phase_state["phase"] = _next_phase(clusters, done, progress_log)

            if new_lines:
                logger.info("progress: %s", progress_log[-1])
            record_progress()
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=2)

    phase_state["phase"] = "writing the report"
    record_progress()

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
            "error": None,
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