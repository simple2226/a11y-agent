"""
Bedrock Converse wrapper.

toolChoice forces the model to call our tool, which means we never parse JSON out
of prose and never see a markdown fence. If the call succeeds we have a valid
edits/deferred payload; if it does not, we raise.

Set the model id from the environment. Confirm the exact id for your account with:
    aws bedrock list-foundation-models --region us-east-1 \
        --query "modelSummaries[?contains(modelId,'claude')].modelId" --output table
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from agent.schema import TOOL_NAME, TOOL_SPEC

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")

# Newer Claude models are not invocable through their bare foundation-model id;
# they need a cross-region inference profile, which is the "global." / "us."
# prefixed form. Confirm what your account can actually call with:
#     python scripts/check_bedrock.py
# Default is Amazon Nova Pro, not Claude. Nova needs no provider usage form,
# supports toolChoice auto/any/tool, and is cheaper. Swap it with the env var
# once your account is authorised for whichever provider you prefer.
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0")

# "tool" forces our specific tool. Models that reject it fall back automatically;
# set this explicitly to skip the probing.
TOOL_CHOICE_MODE = os.environ.get("BEDROCK_TOOL_CHOICE_MODE", "tool")

# Set A11Y_MOCK_MODEL=1 to run the whole loop with a deterministic rule-based
# stand-in instead of Bedrock. See agent/mock_model.py.
USE_MOCK_MODEL = os.environ.get("A11Y_MOCK_MODEL", "").strip() in {"1", "true", "yes"}

MAX_OUTPUT_TOKENS = 4096
TEMPERATURE = 0.0

MAX_ATTEMPTS = 4
RETRYABLE_ERROR_CODES = {"ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException"}

# Nova's server-side inference timeout is far longer than boto3's 60s default.
# Same reasoning as model_provider.REQUEST_TIMEOUT_SECONDS: a converse call that
# has not answered in 75s is not going to, and waiting 180s for it spends the
# Lambda's budget on a request that will fail anyway.
_boto_config = Config(
    retries={"max_attempts": 1, "mode": "standard"},
    connect_timeout=15,
    read_timeout=int(float(os.environ.get("MODEL_REQUEST_TIMEOUT_SECONDS", "75"))),
)


def _tool_choice_payload(mode: str) -> dict:
    if mode == "any":
        return {"any": {}}
    if mode == "auto":
        return {"auto": {}}
    return {"tool": {"name": TOOL_NAME}}


TOOL_CHOICE_FALLBACK_ORDER = ["tool", "any", "auto"]


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, usage: dict) -> None:
        self.input_tokens += usage.get("inputTokens", 0)
        self.output_tokens += usage.get("outputTokens", 0)
        self.calls += 1


@dataclass
class ModelEdits:
    edits: list[dict] = field(default_factory=list)
    deferred: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    stop_reason: str = ""


def _client():
    return boto3.client("bedrock-runtime", region_name=BEDROCK_REGION, config=_boto_config)


class BedrockAccessError(RuntimeError):
    """A Bedrock failure we can explain, rather than a raw botocore traceback."""


def _explain_client_error(error: ClientError) -> str | None:
    message = error.response.get("Error", {}).get("Message", "")
    code = error.response.get("Error", {}).get("Code", "")
    lowered = message.lower()

    if "on-demand throughput" in lowered or "inference profile" in lowered:
        return (
            f"Model id {BEDROCK_MODEL_ID!r} cannot be invoked on demand. Use the "
            f"inference-profile form instead, e.g. 'global.' or 'us.' prefixed. "
            f"Run: python scripts/check_bedrock.py"
        )
    if "create a support case" in lowered or "not authorized to perform this action" in lowered:
        return (
            "This AWS account is not authorised for this model provider. File an "
            "Account-and-billing support case, and meanwhile switch providers:\n"
            '  $env:BEDROCK_MODEL_ID="us.amazon.nova-pro-v1:0"\n'
            "Or run offline with A11Y_MOCK_MODEL=1. Diagnose with: "
            "python scripts/check_bedrock.py"
        )
    if "operation not allowed" in lowered:
        return (
            "Bedrock refused the call with 'Operation not allowed'. Anthropic models "
            "are enabled by default but still require a one-time usage form before "
            "first use. Open the AWS console -> Bedrock -> Model catalog -> a Claude "
            "model -> Open in Playground, submit the form, send one message there, "
            "then retry. Run: python scripts/check_bedrock.py"
        )
    if code == "AccessDeniedException":
        return (
            "IAM denied bedrock:InvokeModel / bedrock:Converse for this principal. "
            "Add the permission, or run with A11Y_MOCK_MODEL=1 to keep working."
        )
    return None


def _extract_tool_input(response: dict) -> dict:
    for content_block in response["output"]["message"]["content"]:
        tool_use = content_block.get("toolUse")
        if tool_use and tool_use.get("name") == TOOL_NAME:
            return tool_use["input"]
    raise RuntimeError(
        "model did not call the tool; stopReason=" + response.get("stopReason", "unknown")
    )


def request_edits(
    system_prompt: str,
    user_prompt: str,
    rule_id: str = "",
    nodes: list[dict] | None = None,
) -> ModelEdits:
    if USE_MOCK_MODEL:
        from agent.mock_model import generate_mock_edits

        return generate_mock_edits(rule_id, nodes or [])

    client = _client()
    last_error: Exception | None = None

    # Try the configured toolChoice mode first, then degrade. Nova and Claude
    # accept "tool"; some other providers only accept "any" or "auto".
    modes = [TOOL_CHOICE_MODE] + [
        mode for mode in TOOL_CHOICE_FALLBACK_ORDER if mode != TOOL_CHOICE_MODE
    ]
    mode_index = 0

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.converse(
                modelId=BEDROCK_MODEL_ID,
                system=[{"text": system_prompt}],
                messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                inferenceConfig={
                    "maxTokens": MAX_OUTPUT_TOKENS,
                    "temperature": TEMPERATURE,
                },
                toolConfig={
                    "tools": [TOOL_SPEC],
                    "toolChoice": _tool_choice_payload(modes[mode_index]),
                },
            )
            tool_input = _extract_tool_input(response)
            return ModelEdits(
                edits=tool_input.get("edits", []),
                deferred=tool_input.get("deferred", []),
                usage=response.get("usage", {}),
                stop_reason=response.get("stopReason", ""),
            )

        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code", "")
            last_error = error
            message = error.response.get("Error", {}).get("Message", "").lower()

            # A rejected toolChoice mode is recoverable: degrade and retry.
            if "toolchoice" in message or "tool choice" in message:
                if mode_index + 1 < len(modes):
                    mode_index += 1
                    continue

            if error_code not in RETRYABLE_ERROR_CODES:
                explanation = _explain_client_error(error)
                if explanation:
                    raise BedrockAccessError(explanation) from error
                raise
            backoff_seconds = (2**attempt) + random.random()
            time.sleep(backoff_seconds)

    raise RuntimeError(f"bedrock call failed after {MAX_ATTEMPTS} attempts") from last_error