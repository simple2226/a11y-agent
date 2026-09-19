"""
Why is this run stuck?

    python scripts/diagnose_run.py <runId> [--stack a11y-agent] [--region us-east-1]

Reads the run's DynamoDB rows, the Step Functions execution and the agent
Lambda's recent logs, and says which of the three things went wrong:

    the execution never started        -> API/Step Functions permissions
    the execution is still running     -> check the heartbeat age below
    the execution failed               -> the cause is printed

Run it the moment a demo looks stuck. Faster than three console tabs, and it
prints the heartbeat age, which is the single number that distinguishes "slow"
from "dead".
"""

from __future__ import annotations

import argparse
import time
from typing import Any, cast

import boto3
from boto3.dynamodb.conditions import Key


def stack_outputs(stack_name: str, region: str) -> dict:
    client = boto3.client("cloudformation", region_name=region)
    stack = client.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}


def show_dynamo(table_name: str, region: str, run_id: str) -> list[str]:
    """Print the ledger. Returns the status of every page row, for the verdict."""
    # boto3.resource returns a union of every service resource, which no stub set
    # narrows from a runtime string. One cast at the boundary, then normal code.
    dynamodb = cast(Any, boto3.resource("dynamodb", region_name=region))
    table = dynamodb.Table(table_name)
    response = table.query(KeyConditionExpression=Key("pk").eq(f"RUN#{run_id}"))

    # boto3's resource API types every attribute as a union of every DynamoDB
    # type. Nothing here is dynamic enough to be worth threading that through,
    # so each row is read as a plain dict once, at the boundary.
    items = [cast(dict[str, Any], item) for item in response.get("Items", [])]

    if not items:
        print(f"  no rows at all for run {run_id} -- the API never wrote it")
        return []

    page_statuses: list[str] = []

    for item in items:
        sort_key = str(item.get("sk", ""))

        if sort_key == "META":
            print(f"  META      status={item.get('status')} urls={item.get('urls')}")
            if item.get("error"):
                print(f"      error: {item['error']}")
            continue

        if not sort_key.startswith("PAGE#"):
            continue

        page_statuses.append(str(item.get("status", "unknown")))

        print(f"  {sort_key}  status={item.get('status')}")
        if item.get("error"):
            print(f"      error: {item['error']}")

        progress = cast(dict[str, Any], item.get("progress") or {})
        if not progress:
            continue

        age_seconds = int(time.time()) - int(progress.get("updatedAt", 0))
        verdict = "DEAD -- nothing has been written for over a minute" if age_seconds > 75 else "alive"

        print(f"      phase: {progress.get('phase')}")
        print(
            f"      {progress.get('clustersDone')}/{progress.get('clustersTotal')} rules"
            f", heartbeat {age_seconds}s ago ({verdict})"
        )

        log_lines = cast(list[str], progress.get("log") or [])
        for line in log_lines[-6:]:
            print(f"        | {line}")

    return page_statuses


def show_execution(state_machine_arn: str, region: str, run_id: str) -> str:
    """Print the execution's state. Returns its status, for the verdict."""
    client = boto3.client("stepfunctions", region_name=region)
    arn = state_machine_arn.replace(":stateMachine:", ":execution:") + f":run-{run_id}"
    try:
        execution = client.describe_execution(executionArn=arn)
    except client.exceptions.ExecutionDoesNotExist:
        print("  no execution -- POST /runs did not start one (check API permissions)")
        return "MISSING"

    status = execution["status"]
    started = execution["startDate"]
    print(f"  status={status}  started={started:%H:%M:%S}")

    if status == "RUNNING":
        elapsed = int(time.time() - started.timestamp())
        print(f"  running for {elapsed // 60}m {elapsed % 60}s")
        return status

    if status == "SUCCEEDED":
        return status

    # Walk backwards: the last failure event is the one that ended the execution.
    history = client.get_execution_history(executionArn=arn, reverseOrder=True)
    failure_keys = (
        "taskFailedEventDetails",
        "taskTimedOutEventDetails",
        "executionFailedEventDetails",
        "executionAbortedEventDetails",
    )

    for event in history.get("events", []):
        event_fields = cast(dict[str, Any], event)
        for key in failure_keys:
            detail = cast(dict[str, Any], event_fields.get(key) or {})
            if detail:
                print(f"  {detail.get('error')}: {str(detail.get('cause'))[:600]}")
                return status

    print("  execution did not succeed but no failure event was found")
    return status


def show_logs(function_name: str, region: str, minutes: int) -> None:
    client = boto3.client("logs", region_name=region)
    group = f"/aws/lambda/{function_name}"
    start = int((time.time() - minutes * 60) * 1000)
    try:
        response = client.filter_log_events(
            logGroupName=group,
            startTime=start,
            filterPattern="?ERROR ?Error ?error ?Task ?timed ?progress",
        )
    except client.exceptions.ResourceNotFoundException:
        print(f"  no log group {group} yet -- the Lambda has never run")
        return

    events = [cast(dict[str, Any], event) for event in response.get("events", [])]
    if not events:
        print(f"  nothing matching in the last {minutes} minutes")
        return

    for event in events[-25:]:
        print(f"  {str(event.get('message', '')).rstrip()}")


def find_agent_function(stack_name: str, region: str) -> str | None:
    """The agent Lambda's generated name, e.g. a11y-agent-AgentFunction-Ab12Cd34.

    Paginated: a busy account has more functions than one page returns, and the
    agent is nobody's guess to be on the first one.
    """
    client = boto3.client("lambda", region_name=region)
    prefix = f"{stack_name}-AgentFunction"

    for page in client.get_paginator("list_functions").paginate():
        for function in page.get("Functions", []):
            name = str(cast(dict[str, Any], function).get("FunctionName", ""))
            if name.startswith(prefix):
                return name
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--stack", default="a11y-agent")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--minutes", type=int, default=20)
    args = parser.parse_args()

    outputs = stack_outputs(args.stack, args.region)

    print("\n== ledger ==")
    page_statuses = show_dynamo(outputs["RunTableName"], args.region, args.run_id)

    print("\n== step functions ==")
    execution_status = show_execution(outputs["StateMachineArn"], args.region, args.run_id)

    print("\n== agent logs ==")
    agent_function = find_agent_function(args.stack, args.region)
    if agent_function:
        show_logs(agent_function, args.region, args.minutes)
    else:
        print(f"  could not find a {args.stack}-AgentFunction-* Lambda")

    print("\n== verdict ==")
    print("  " + verdict(execution_status, page_statuses))
    print()


def verdict(execution_status: str, page_statuses: list[str]) -> str:
    """Name the failure mode, rather than leaving two readings side by side."""
    unfinished = [status for status in page_statuses if status == "running"]

    if execution_status == "SUCCEEDED" and unfinished:
        return (
            "the execution SUCCEEDED but a page is still 'running' -- the agent\n"
            "  raised, the Map state's Catch swallowed it, and RecordFailure recorded\n"
            "  nothing. The real cause is in the agent logs above; widen the window\n"
            "  with --minutes if they are empty. Deploying the current template\n"
            "  replaces that Pass state with a DynamoDB write, so this reads as\n"
            "  'failed' from then on."
        )
    if execution_status == "RUNNING" and not page_statuses:
        return "the execution started but the agent has not written its first row yet (cold start)"
    if execution_status == "MISSING":
        return "nothing ever started -- check the API's Step Functions permissions"
    if unfinished:
        return "a page is still running; check the heartbeat age above before assuming it is dead"
    if execution_status == "SUCCEEDED":
        return "the run completed"
    return f"execution {execution_status}; the cause is printed above"


if __name__ == "__main__":
    main()