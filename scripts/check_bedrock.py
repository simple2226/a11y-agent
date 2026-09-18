#!/usr/bin/env python3
"""
Find a Bedrock model this account can actually use for forced tool calling.

    python scripts/check_bedrock.py

Probes candidates across providers with a REAL forced tool call, because a model
that answers text fine can still reject toolChoice. Prints the export line for
the first one that works.

Distinguishes the failure modes that all surface as opaque boto3 errors:
  1. Credentials / region not configured
  2. Anthropic one-time usage form not submitted / account not authorised
        -> "Operation not allowed" or "not authorized ... create a support case"
  3. Bare model id needs an inference profile
        -> "on-demand throughput isn't supported"
  4. IAM missing bedrock:InvokeModel        -> AccessDeniedException
  5. Account quota is 0 (new free-tier)     -> ThrottlingException
"""
import os
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError

REGION = os.environ.get("BEDROCK_REGION", "us-east-1")

# (provider, model id). Nova first: no usage-form gate, supports toolChoice
# auto/any/tool, and costs less than Sonnet.
CANDIDATE_MODELS = [
    ("amazon", "us.amazon.nova-pro-v1:0"),
    ("amazon", "us.amazon.nova-lite-v1:0"),
    ("amazon", "amazon.nova-pro-v1:0"),
    ("anthropic", "global.anthropic.claude-sonnet-4-5-20250929-v1:0"),
    ("anthropic", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
    ("anthropic", "us.anthropic.claude-3-5-sonnet-20241022-v2:0"),
    ("anthropic", "anthropic.claude-3-5-haiku-20241022-v1:0"),
    ("mistral", "mistral.mistral-large-2407-v1:0"),
    ("meta", "us.meta.llama3-3-70b-instruct-v1:0"),
]

PROBE_TOOL = {
    "toolSpec": {
        "name": "emit_probe",
        "description": "Return the two values you were asked for.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "colour": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["colour", "count"],
            }
        },
    }
}

PROBE_PROMPT = "Call emit_probe with colour set to blue and count set to 3."


def explain(error: ClientError) -> str:
    message = error.response.get("Error", {}).get("Message", "")
    code = error.response.get("Error", {}).get("Code", "")
    lowered = message.lower()

    if "on-demand throughput" in lowered or "inference profile" in lowered:
        return "needs an inference profile -- prefix the id with 'us.' or 'global.'"
    if "create a support case" in lowered or "not authorized to perform this action" in lowered:
        return ("account NOT AUTHORISED for this provider. File an Account-and-billing "
                "support case, and use a different provider meanwhile.")
    if "operation not allowed" in lowered:
        return ("provider gate: Anthropic models need a one-time usage form "
                "(Console -> Bedrock -> Model catalog -> Claude -> Open in Playground).")
    if "toolchoice" in lowered or "tool choice" in lowered:
        return f"model does not support this toolChoice mode ({message[:120]})"
    if code == "AccessDeniedException":
        return "IAM: principal lacks bedrock:InvokeModel / bedrock:Converse"
    if code == "ThrottlingException":
        return ("throttled. Check Service Quotas -> Amazon Bedrock. A quota of 0 means "
                "the account is not fully provisioned; that needs a support case.")
    if code == "ResourceNotFoundException":
        return f"not available in {REGION}"
    return f"{code}: {message[:160]}"


def probe(runtime, model_id: str) -> tuple[bool, str]:
    """Try forced tool choice, then 'any', then 'auto'. Report which worked."""
    for label, tool_choice in (
        ("tool", {"tool": {"name": "emit_probe"}}),
        ("any", {"any": {}}),
        ("auto", {"auto": {}}),
    ):
        try:
            response = runtime.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": PROBE_PROMPT}]}],
                inferenceConfig={"maxTokens": 256, "temperature": 0.0},
                toolConfig={"tools": [PROBE_TOOL], "toolChoice": tool_choice},
            )
            for block in response["output"]["message"]["content"]:
                if "toolUse" in block:
                    return True, label
            return False, f"no toolUse block with toolChoice={label}"
        except ClientError as error:
            reason = explain(error)
            if "toolChoice" not in reason and "tool choice" not in reason.lower():
                return False, reason
    return False, "no toolChoice mode accepted"


def main() -> int:
    print(f"region: {REGION}")

    try:
        identity = boto3.client("sts", region_name=REGION).get_caller_identity()
        print(f"identity: {identity['Arn']}")
    except NoCredentialsError:
        print("FAIL: no AWS credentials. Run `aws configure`.")
        return 1

    # Nova's server-side timeout is long; keep the client patient but bounded.
    runtime = boto3.client(
        "bedrock-runtime",
        region_name=REGION,
        config=Config(read_timeout=180, retries={"max_attempts": 1}),
    )

    print("\nprobing forced tool calling:")
    for provider, model_id in CANDIDATE_MODELS:
        works, detail = probe(runtime, model_id)
        if works:
            print(f"  OK    [{provider}] {model_id}  (toolChoice={detail})")
            print("\nUse this:")
            print(f'  PowerShell: $env:BEDROCK_MODEL_ID="{model_id}"')
            print(f"  bash:       export BEDROCK_MODEL_ID={model_id}")
            if detail != "tool":
                print(f"\n  NOTE: forced toolChoice was rejected; '{detail}' worked instead.")
                print("  Set BEDROCK_TOOL_CHOICE_MODE=" + detail)
            return 0
        print(f"  FAIL  [{provider}] {model_id}\n        {detail}")

    print("\nNothing worked. Meanwhile: set A11Y_MOCK_MODEL=1 and keep building the loop.")
    return 1


if __name__ == "__main__":
    sys.exit(main())