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
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-5-20250929-v1:0"
)

MAX_OUTPUT_TOKENS = 4096
TEMPERATURE = 0.0

MAX_ATTEMPTS = 4
RETRYABLE_ERROR_CODES = {"ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException"}

_boto_config = Config(retries={"max_attempts": 1, "mode": "standard"}, read_timeout=120)


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


def _extract_tool_input(response: dict) -> dict:
    for content_block in response["output"]["message"]["content"]:
        tool_use = content_block.get("toolUse")
        if tool_use and tool_use.get("name") == TOOL_NAME:
            return tool_use["input"]
    raise RuntimeError(
        "model did not call the tool; stopReason=" + response.get("stopReason", "unknown")
    )


def request_edits(system_prompt: str, user_prompt: str) -> ModelEdits:
    client = _client()
    last_error: Exception | None = None

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
                    "toolChoice": {"tool": {"name": TOOL_NAME}},
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
            if error_code not in RETRYABLE_ERROR_CODES:
                raise
            backoff_seconds = (2**attempt) + random.random()
            time.sleep(backoff_seconds)

    raise RuntimeError(f"bedrock call failed after {MAX_ATTEMPTS} attempts") from last_error
