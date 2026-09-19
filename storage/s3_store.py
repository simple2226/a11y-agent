"""
Artifact storage. Original and patched HTML, screenshots, axe JSON, reports.

Patched copies of third-party pages are NEVER public. The bucket blocks public
access; the dashboard reads through presigned URLs that expire in an hour.
"""

from __future__ import annotations

import json
import os

import boto3

ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "")
PRESIGNED_URL_TTL_SECONDS = 3600


def _client():
    return boto3.client("s3")


def _key(run_id: str, page_id: str, kind: str, extension: str) -> str:
    return f"{kind}/{run_id}/{page_id}.{extension}"


def put_html(run_id: str, page_id: str, kind: str, html_text: str) -> str:
    """kind is 'original' or 'patched'."""
    key = _key(run_id, page_id, kind, "html")
    _client().put_object(
        Bucket=ARTIFACT_BUCKET,
        Key=key,
        Body=html_text.encode("utf-8"),
        ContentType="text/html; charset=utf-8",
        # Belt and braces: even if the bucket policy were loosened, this copy
        # of someone else's page should never be indexed.
        Metadata={"x-robots-tag": "noindex, nofollow"},
    )
    return key


def put_screenshot(run_id: str, page_id: str, kind: str, png_bytes: bytes) -> str:
    key = _key(run_id, page_id, f"screenshots/{kind}", "png")
    _client().put_object(
        Bucket=ARTIFACT_BUCKET, Key=key, Body=png_bytes, ContentType="image/png"
    )
    return key


def put_json(run_id: str, page_id: str, kind: str, payload: dict) -> str:
    key = _key(run_id, page_id, kind, "json")
    _client().put_object(
        Bucket=ARTIFACT_BUCKET,
        Key=key,
        Body=json.dumps(payload).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def presigned_url(key: str, ttl_seconds: int = PRESIGNED_URL_TTL_SECONDS) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": ARTIFACT_BUCKET, "Key": key},
        ExpiresIn=ttl_seconds,
    )


def get_text(key: str) -> str:
    response = _client().get_object(Bucket=ARTIFACT_BUCKET, Key=key)
    return response["Body"].read().decode("utf-8")
