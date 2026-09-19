"""
Read API for the dashboard, plus run creation.

    POST /runs               {"urls": [...]}  -> start a Step Functions execution
    GET  /runs/{id}                           -> run status + per-page summaries
    GET  /runs/{id}/pages/{pageId}            -> violations, edits, artifact URLs
    GET  /healthz
"""

from __future__ import annotations

import json
import json
import os
import uuid

import boto3
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from storage import dynamo_store, s3_store

STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
MAX_URLS_PER_RUN = 5

app = FastAPI(title="a11y-agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class CreateRunRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=MAX_URLS_PER_RUN)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/runs")
def create_run(request: CreateRunRequest) -> dict:
    run_id = uuid.uuid4().hex[:12]

    dynamo_store.put_run(run_id, {"status": "queued", "urls": request.urls})

    boto3.client("stepfunctions").start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=f"run-{run_id}",
        input=json.dumps({"runId": run_id, "urls": request.urls}),
    )
    return {"runId": run_id, "status": "queued"}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = dynamo_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")

    pages = dynamo_store.list_run_items(run_id, "PAGE#")
    finished = [page for page in pages if page.get("status") == "done"]
    failed = [page for page in pages if page.get("status") == "failed"]
    expected = len(run.get("urls", []))

    # A run is only "failed" once nothing is still working. With several pages in
    # flight one can fail while the rest succeed, and that run is partly usable.
    if expected and len(finished) >= expected:
        derived_status = "done"
    elif failed and len(finished) + len(failed) >= max(expected, len(pages)):
        derived_status = "done" if finished else "failed"
    elif run.get("status") == "failed":
        # Written by the Step Functions catch when the Lambda was killed outright
        # and could not record its own failure. Nothing is still working.
        derived_status = "failed"
    elif pages:
        derived_status = "running"
    else:
        derived_status = run.get("status", "queued")

    return {
        "runId": run_id,
        "status": derived_status,
        "error": run.get("error") if derived_status == "failed" else None,
        "urls": run.get("urls", []),
        "pages": [
            {
                "pageId": page["pageId"],
                "url": page.get("url"),
                "title": page.get("title"),
                "status": page.get("status"),
                "scoreBefore": page.get("scoreBefore"),
                "scoreAfter": page.get("scoreAfter"),
                # Live progress while the agent is still working.
                "progress": page.get("progress"),
                # Present only on a page that failed, so the dashboard can say
                # what went wrong instead of spinning.
                "error": page.get("error"),
            }
            for page in pages
        ],
        "aggregate": {
            "pagesDone": len(finished),
            "pagesFailed": len(failed),
            "pagesTotal": len(run.get("urls", [])),
            "scoreBefore": _mean(page.get("scoreBefore") for page in finished),
            "scoreAfter": _mean(page.get("scoreAfter") for page in finished),
        },
    }


def _mean(values) -> int | None:
    numbers = [value for value in values if isinstance(value, (int, float))]
    return round(sum(numbers) / len(numbers)) if numbers else None


def _find_page(run_id: str, page_id: str) -> dict:
    for item in dynamo_store.list_run_items(run_id, f"PAGE#{page_id}"):
        if item["pageId"] == page_id:
            return item
    raise HTTPException(status_code=404, detail="page not found")


@app.get("/runs/{run_id}/pages/{page_id}/report")
def get_page_report(run_id: str, page_id: str) -> dict:
    """The exact shape local_run.py writes to out/report.json, plus artifact URLs.

    One contract for the dashboard whether it reads a local file or this API.
    """
    page = _find_page(run_id, page_id)

    report_key = page.get("reportKey")
    if not report_key:
        raise HTTPException(status_code=409, detail="report not written yet")

    report = json.loads(s3_store.get_text(report_key))
    report["artifacts"] = {
        label: s3_store.presigned_url(page[key])
        for label, key in (("original", "originalKey"), ("patched", "patchedKey"))
        if page.get(key)
    }
    return report


@app.get("/runs/{run_id}/pages/{page_id}")
def get_page(run_id: str, page_id: str) -> dict:
    page = _find_page(run_id, page_id)

    violations = dynamo_store.list_run_items(run_id, f"VIOLATION#{page_id}#")

    artifacts = {}
    for label, key_name in (("original", "originalKey"), ("patched", "patchedKey")):
        key = page.get(key_name)
        if key:
            artifacts[label] = s3_store.presigned_url(key)

    return {"page": page, "violations": violations, "artifacts": artifacts}