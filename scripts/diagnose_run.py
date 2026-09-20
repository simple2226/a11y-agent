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


def show_logs(function_name: str, region: str, minutes: int, lines: int) -> str:
    """The tail of the agent's log, unfiltered.

    Deliberately no filterPattern. A Python traceback arrives as several events
    and the interesting line is rarely the one containing the word "error" --
    filtering shredded exactly the exception we needed to read.
    """
    client = boto3.client("logs", region_name=region)
    group = f"/aws/lambda/{function_name}"
    start = int((time.time() - minutes * 60) * 1000)

    collected: list[str] = []
    try:
        pages = client.get_paginator("filter_log_events").paginate(
            logGroupName=group, startTime=start
        )
        for page in pages:
            for event in page.get("events", []):
                message = str(cast(dict[str, Any], event).get("message", "")).rstrip()
                collected.extend(message.splitlines() or [""])
            # A long window on a busy function is not worth paging through in
            # full; the tail is what matters and it is at the end.
            if len(collected) > 4000:
                break
    except client.exceptions.ResourceNotFoundException:
        print(f"  no log group {group} yet -- the Lambda has never run")
        return ""

    if not collected:
        print(f"  no log events in the last {minutes} minutes (try --minutes 120)")
        return ""

    if len(collected) > lines:
        print(f"  ... {len(collected) - lines} earlier line(s) omitted, --lines to see more")
    for line in collected[-lines:]:
        print(f"  {line}")

    return "\n".join(collected[-400:])


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


def show_deployment(function_name: str, region: str) -> None:
    """When the agent was last deployed, and which config it is running.

    "Did my deploy take?" is otherwise answered by guessing. The presence of
    RUN_BUDGET_SECONDS is a direct probe: it only exists in the template that
    also fixed the failure path, so if it is missing, the running Lambda is the
    old one however recently it was pushed.
    """
    client = boto3.client("lambda", region_name=region)
    config = cast(dict[str, Any], client.get_function_configuration(FunctionName=function_name))

    environment = cast(dict[str, Any], config.get("Environment") or {})
    variables = cast(dict[str, Any], environment.get("Variables") or {})

    print(f"  last modified: {config.get('LastModified')}")
    print(f"  provider chain: {variables.get('MODEL_PROVIDER', '(unset)')}")

    # /tmp is where Chromium and Playwright do all their scratch work, and it
    # persists across invocations on a warm container. At the 512 MB default a
    # few runs fill it, and the failure arrives disguised as a browser crash.
    storage = cast(dict[str, Any], config.get("EphemeralStorage") or {})
    ephemeral_mb = int(storage.get("Size", 512))
    if ephemeral_mb <= 512:
        print(f"  /tmp size: {ephemeral_mb} MB -- DEFAULT. Chromium will run out on a")
        print("             warm container. The current template sets 4096.")
    else:
        print(f"  /tmp size: {ephemeral_mb} MB")

    if "RUN_BUDGET_SECONDS" in variables:
        print(f"  budgets: run={variables['RUN_BUDGET_SECONDS']}s "
              f"call={variables.get('MODEL_CALL_BUDGET_SECONDS')}s "
              f"request={variables.get('MODEL_REQUEST_TIMEOUT_SECONDS')}s")
    else:
        print("  budgets: MISSING -- this is the OLD template. Failures will not be")
        print("           recorded and the dashboard will spin. Run infra/deploy.ps1.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--stack", default="a11y-agent")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--lines", type=int, default=60, help="log tail length")
    args = parser.parse_args()

    outputs = stack_outputs(args.stack, args.region)

    print("\n== ledger ==")
    page_statuses = show_dynamo(outputs["RunTableName"], args.region, args.run_id)

    print("\n== step functions ==")
    execution_status = show_execution(outputs["StateMachineArn"], args.region, args.run_id)

    agent_function = find_agent_function(args.stack, args.region)

    print("\n== deployment ==")
    if agent_function:
        show_deployment(agent_function, args.region)
    else:
        print(f"  could not find a {args.stack}-AgentFunction-* Lambda")

    print("\n== agent logs ==")
    log_tail = ""
    if agent_function:
        log_tail = show_logs(agent_function, args.region, args.minutes, args.lines)

    print("\n== verdict ==")
    print("  " + verdict(execution_status, page_statuses, log_tail))
    print()


def verdict(execution_status: str, page_statuses: list[str], log_tail: str = "") -> str:
    """Name the failure mode, rather than leaving two readings side by side."""
    unfinished = [status for status in page_statuses if status == "running"]

    # An import error means the Lambda died before any of our code ran, so
    # nothing else in this report is evidence about the page or the model.
    if "Runtime.ImportModuleError" in log_tail:
        missing = ""
        for line in log_tail.splitlines():
            if "ImportModuleError" in line:
                missing = line.split("ImportModuleError:")[-1].strip()[:160]
                break
        return (
            "the Lambda could not even import the agent, so nothing about the\n"
            "  page or the model is being tested here. The deployed .py files are\n"
            "  from different versions of each other:\n"
            f"    {missing}\n"
            "  Replace the file named above and redeploy."
        )

    # Disk before anything else. A full /tmp presents as at least three
    # different-looking failures -- an OSError on write, a Playwright launch
    # error, and a renderer that dies with TargetClosedError -- and all three
    # were read as Chromium being flaky. Name it once, here.
    if "ENOSPC" in log_tail or "No space left on device" in log_tail:
        return (
            "/tmp filled up. Chromium and Playwright use it as scratch and it\n"
            "  SURVIVES between invocations on a warm container, so this gets worse\n"
            "  the more runs a container serves. It presents as a browser crash,\n"
            "  which is what makes it confusing.\n"
            "  Check '/tmp size' above: 512 MB is the Lambda default and is not\n"
            "  enough. The current template sets EphemeralStorage to 4096 MB and\n"
            "  audit/runner.py sweeps leftovers before each audit."
        )

    # No page row at all is its own failure, and a distinctive one: the agent
    # died before it could write anything, which means it never got past the
    # mirror. Reading this as "completed" because no page is unfinished was a
    # bug in this function.
    if not page_statuses:
        if execution_status in {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}:
            return (
                "the agent never wrote a page row -- it died before or during the\n"
                "  mirror, so the cause is a page fetch, not the model. The traceback\n"
                "  is in the agent logs above."
            )
        return "no page row yet; the agent has not reached its first write"

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