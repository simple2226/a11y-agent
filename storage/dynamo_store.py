"""
Run ledger. Single table, composite key.

    pk = RUN#<run_id>
    sk = META                      run-level status and aggregate scores
         PAGE#<page_id>            per-page scores and artifact keys
         VIOLATION#<page>#<rule>   per-rule outcome, edits, deferrals

Everything carries a TTL so a hackathon account does not accumulate junk.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

RUN_TABLE = os.environ.get("RUN_TABLE", "")
TTL_DAYS = 7


def _table():
    return boto3.resource("dynamodb").Table(RUN_TABLE)


def _expiry() -> int:
    return int(time.time()) + TTL_DAYS * 86400


def _to_dynamo(value: Any) -> Any:
    """DynamoDB rejects float. Convert on the way in, recursively."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _to_dynamo(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(item) for item in value]
    return value


def _from_dynamo(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {key: _from_dynamo(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(item) for item in value]
    return value


def put_run(run_id: str, attributes: dict) -> None:
    _table().put_item(
        Item=_to_dynamo(
            {"pk": f"RUN#{run_id}", "sk": "META", "expiresAt": _expiry(), **attributes}
        )
    )


def update_run_status(run_id: str, status: str, extra: dict | None = None) -> None:
    item = {"status": status, **(extra or {})}
    expression = "SET " + ", ".join(f"#{key} = :{key}" for key in item)
    _table().update_item(
        Key={"pk": f"RUN#{run_id}", "sk": "META"},
        UpdateExpression=expression,
        ExpressionAttributeNames={f"#{key}": key for key in item},
        ExpressionAttributeValues={f":{key}": _to_dynamo(value) for key, value in item.items()},
    )


def put_page(run_id: str, page_id: str, attributes: dict) -> None:
    _table().put_item(
        Item=_to_dynamo(
            {
                "pk": f"RUN#{run_id}",
                "sk": f"PAGE#{page_id}",
                "pageId": page_id,
                "expiresAt": _expiry(),
                **attributes,
            }
        )
    )


def put_violation(run_id: str, page_id: str, rule_id: str, attributes: dict) -> None:
    _table().put_item(
        Item=_to_dynamo(
            {
                "pk": f"RUN#{run_id}",
                "sk": f"VIOLATION#{page_id}#{rule_id}",
                "pageId": page_id,
                "ruleId": rule_id,
                "expiresAt": _expiry(),
                **attributes,
            }
        )
    )


def update_page_progress(run_id: str, page_id: str, progress: dict) -> None:
    """Merge live progress into an existing page row.

    update_item rather than put_item: a run in flight must not clobber the url,
    title and artifact keys that were written when the page started.
    """
    _table().update_item(
        Key={"pk": f"RUN#{run_id}", "sk": f"PAGE#{page_id}"},
        UpdateExpression="SET progress = :progress",
        ExpressionAttributeValues={":progress": _to_dynamo(progress)},
    )


def update_page_fields(run_id: str, page_id: str, fields: dict) -> None:
    """Merge arbitrary attributes into an existing page row.

    Used to mark a page failed without losing the url, title and artifact keys
    written when it started -- a failed run still has a mirrored original worth
    showing, and the UI needs the url to say what failed.
    """
    if not fields:
        return
    expression = "SET " + ", ".join(f"#{key} = :{key}" for key in fields)
    _table().update_item(
        Key={"pk": f"RUN#{run_id}", "sk": f"PAGE#{page_id}"},
        UpdateExpression=expression,
        ExpressionAttributeNames={f"#{key}": key for key in fields},
        ExpressionAttributeValues={
            f":{key}": _to_dynamo(value) for key, value in fields.items()
        },
    )


def get_run(run_id: str) -> dict | None:
    response = _table().get_item(Key={"pk": f"RUN#{run_id}", "sk": "META"})
    item = response.get("Item")
    return _from_dynamo(item) if item else None


def list_run_items(run_id: str, sk_prefix: str = "") -> list[dict]:
    condition = Key("pk").eq(f"RUN#{run_id}")
    if sk_prefix:
        condition = condition & Key("sk").begins_with(sk_prefix)
    response = _table().query(KeyConditionExpression=condition)
    return [_from_dynamo(item) for item in response.get("Items", [])]