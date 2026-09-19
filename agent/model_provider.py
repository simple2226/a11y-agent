"""
Pluggable model provider.

The agent needs one thing from a model: a forced call to our emit_edits tool
returning {edits, deferred}. Every provider here does that and returns the same
ModelEdits, so agent/graph.py never changes.

    MODEL_PROVIDER=bedrock    (default)  BEDROCK_MODEL_ID, BEDROCK_REGION
    MODEL_PROVIDER=anthropic              ANTHROPIC_API_KEY, ANTHROPIC_MODEL
    MODEL_PROVIDER=gemini                 GEMINI_API_KEY, GEMINI_MODEL
    MODEL_PROVIDER=openai                 OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BASE_URL
    MODEL_PROVIDER=mock                   no credentials, deterministic rules

MODEL_PROVIDER also takes a comma-separated fallback chain, tried in order:

    MODEL_PROVIDER=gemini,groq,mock

That exists because a free-tier endpoint returning 503 "experiencing high
demand" is capacity, not a bug, and no retry policy fixes it. The only remedy is
a different endpoint. The chain shares one time budget, and the run log records
which provider actually answered.

OPENAI_BASE_URL makes the openai provider work against anything speaking the
Chat Completions API -- Groq, OpenRouter, Together, a local Ollama.

Only httpx is used, so no provider SDK needs installing.
"""

from __future__ import annotations

import json
import os
import random
import time

import httpx

from agent.bedrock_client import ModelEdits
from agent.schema import TOOL_NAME, TOOL_SPEC

# A structured-output call of this size answers in seconds. One that has not
# answered in a minute is not about to; waiting longer only spends the Lambda's
# budget on a request that will fail anyway.
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("MODEL_REQUEST_TIMEOUT_SECONDS", "75"))

# Every hosted model API rate limits and sheds load. Without retries here, one
# transient 503 fails the Lambda, Step Functions retries the WHOLE invocation,
# and the mirror plus every audit runs again from scratch -- minutes of work
# thrown away for a blip that a two second wait would have cleared.
MAX_ATTEMPTS = 5

# ...but retries must not become their own hang. Five attempts at the old 180s
# timeout, each followed by a 30s backoff, is 17 minutes -- longer than the
# Lambda lives. The Lambda is then killed mid-call, nothing writes a result, and
# the dashboard sits on "0 of 3 rules" forever with no error to show. This is the
# ceiling on one logical model call including all of its retries.
MODEL_CALL_BUDGET_SECONDS = float(os.environ.get("MODEL_CALL_BUDGET_SECONDS", "240"))
# 404 is deliberately absent: a retired model name never comes back, and
# retrying it just burns the backoff budget before reporting the real problem.
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

# Credentials, billing and bad model names. These fail identically on every call
# for the rest of the process, so a provider that returns one is taken out of
# the chain instead of being asked again per rule. 402 is here because of a real
# run: "your prepayment credits are depleted" would otherwise have cost one
# round trip for each of eight clusters, to be told the same thing eight times.
FATAL_STATUS_CODES = {400, 401, 402, 403, 404}
BASE_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0


class RetryableProviderError(Exception):
    """Transient upstream failure. Raised inside the retry loop only."""


def _backoff_seconds(attempt: int, response: httpx.Response | None = None) -> float:
    """Exponential backoff with jitter, honouring Retry-After when the API sends it."""
    if response is not None:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return min(float(retry_after), MAX_BACKOFF_SECONDS)

    exponential = BASE_BACKOFF_SECONDS * (2 ** attempt)
    # Full jitter: without it, parallel pages retry in lockstep and hit the
    # same rate limit together.
    return min(exponential, MAX_BACKOFF_SECONDS) * (0.5 + random.random() * 0.5)


def _explain_http_failure(response: httpx.Response) -> str:
    """Say what to do about it, not just what the server said."""
    body = response.text[:400]
    lowered = body.lower()

    if "no longer available" in lowered or "not found for api version" in lowered:
        return (
            f"the model name is retired or wrong -- set GEMINI_MODEL (or the "
            f"provider's model variable) to a current one. The API's own message "
            f"usually names the replacement:\n  {body}"
        )
    if "api key not valid" in lowered or "api_key_invalid" in lowered:
        return (
            "the API key was rejected. Confirm it is set on this environment and "
            "not expired -- `python scripts/check_model.py` lists what it can call."
        )
    if "exceeded your current quota" in lowered:
        return (
            "the account's quota is exhausted, which no amount of retrying "
            "clears. Wait for the reset, use a different key, or switch model.\n"
            f"  {body}"
        )
    if "credits are depleted" in lowered or "prepayment" in lowered:
        return (
            "the account is out of prepaid credit -- this is a billing wall, not "
            "a rate limit, and it will fail identically on every call until the "
            "balance is topped up. Add credit, use a key on a project with free-"
            "tier access, or put another provider first in MODEL_PROVIDER.\n"
            f"  {body}"
        )
    return body


def _post_with_retry(
    provider: str,
    budget_seconds: float | None = None,
    **request_kwargs,
) -> httpx.Response:
    """POST, retrying transient failures. Returns a 2xx response or raises.

    Bounded twice over: MAX_ATTEMPTS caps how many times we ask, and the budget
    caps how long the whole thing may take. The deadline is the one that matters
    operationally -- it is what turns "the demo hung" into "the demo said what
    went wrong", and with a fallback chain it is what leaves time for the next
    provider to be tried.
    """
    budget = MODEL_CALL_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    last_detail = "no attempt made"
    deadline = time.monotonic() + budget

    for attempt in range(MAX_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            raise ProviderError(
                f"{provider} gave up after {budget:.0f}s "
                f"({attempt} attempt(s)) -- {last_detail}"
            )

        # Never let a single attempt outlive the budget.
        request_kwargs["timeout"] = min(
            float(request_kwargs.get("timeout", REQUEST_TIMEOUT_SECONDS)), remaining
        )

        try:
            response = httpx.post(**request_kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            last_detail = f"{type(error).__name__}: {error}"
        else:
            if response.status_code < 400:
                return response

            detail = response.text[:300].replace("\n", " ")
            last_detail = f"HTTP {response.status_code}: {detail}"

            if response.status_code not in RETRYABLE_STATUS_CODES:
                error_type = (
                    ProviderConfigError
                    if response.status_code in FATAL_STATUS_CODES
                    else ProviderError
                )
                raise error_type(
                    f"{provider} {response.status_code}: "
                    f"{_explain_http_failure(response)}"
                )

            if attempt + 1 < MAX_ATTEMPTS:
                delay = min(_backoff_seconds(attempt, response), deadline - time.monotonic())
                if delay <= 0:
                    break
                print(
                    f"{provider}: {response.status_code} on attempt {attempt + 1}"
                    f"/{MAX_ATTEMPTS}, retrying in {delay:.1f}s",
                    flush=True,
                )
                time.sleep(delay)
                continue

        if attempt + 1 < MAX_ATTEMPTS:
            delay = min(_backoff_seconds(attempt), deadline - time.monotonic())
            if delay <= 0:
                break
            print(
                f"{provider}: {last_detail} on attempt {attempt + 1}/{MAX_ATTEMPTS}, "
                f"retrying in {delay:.1f}s",
                flush=True,
            )
            time.sleep(delay)

    raise ProviderError(f"{provider} failed after {MAX_ATTEMPTS} attempts -- {last_detail}")

KNOWN_PROVIDERS = {"bedrock", "anthropic", "gemini", "openai", "groq", "openrouter",
                   "ollama", "mock"}


def resolve_provider_chain() -> list[str]:
    """The providers to try, in order, on every call.

    MODEL_PROVIDER accepts a comma-separated list: "gemini,groq,mock" means try
    Gemini, fall back to Groq, and fall back again to the deterministic rules.
    A free-tier endpoint answering 503 "experiencing high demand" is not a bug
    we can retry our way out of -- it is capacity, and the only real remedy is a
    different endpoint.

    Deliberately NOT a module-level constant: a --provider flag sets os.environ
    after this module is imported, and a constant would silently ignore it.
    """
    if os.environ.get("A11Y_MOCK_MODEL", "").strip() in {"1", "true", "yes"}:
        return ["mock"]

    raw = os.environ.get("MODEL_PROVIDER", "bedrock")
    chain = [name.strip().lower() for name in raw.split(",") if name.strip()]

    unknown = [name for name in chain if name not in KNOWN_PROVIDERS]
    if unknown:
        raise ProviderError(
            f"unknown provider(s) {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(KNOWN_PROVIDERS))}"
        )
    return chain or ["bedrock"]


def resolve_provider() -> str:
    """The first provider in the chain. Kept for callers that want one name."""
    return resolve_provider_chain()[0]


def model_for(provider: str) -> str:
    named = {
        "bedrock": os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0"),
        "anthropic": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
        "gemini": os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL),
        "mock": "deterministic rules, no network",
    }
    if provider in named:
        return named[provider]

    defaults = OPENAI_COMPATIBLE_DEFAULTS.get(
        provider, OPENAI_COMPATIBLE_DEFAULTS["openai"]
    )
    return (
        os.environ.get(defaults["model_env"], "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()
        or defaults["model"]
    )


def active_model_label() -> str:
    """One line describing exactly what will be called. Printed at startup so
    'why did it hit Bedrock' is never a question again."""
    return " -> ".join(
        f"{provider} / {model_for(provider)}" for provider in resolve_provider_chain()
    )

# The tool's JSON schema, shared by every provider that speaks plain JSON Schema.
TOOL_INPUT_SCHEMA = TOOL_SPEC["toolSpec"]["inputSchema"]["json"]
TOOL_DESCRIPTION = TOOL_SPEC["toolSpec"]["description"]


class ProviderError(RuntimeError):
    pass


class ProviderConfigError(ProviderError):
    """A failure that will not clear on its own within this run.

    A depleted prepay balance, a rejected key, a retired model name: retrying is
    pointless and so is asking this provider again on the next cluster. The
    chain marks it dead for the rest of the process rather than paying a round
    trip per rule to be told the same thing eight more times.
    """


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        # Config, not weather: it will be just as unset on the next cluster.
        raise ProviderConfigError(
            f"{name} is not set but MODEL_PROVIDER={resolve_provider()}.\n"
            f"Either set it, or run with --provider mock."
        )
    return value


def _as_model_edits(payload: dict, usage: dict) -> ModelEdits:
    return ModelEdits(
        edits=payload.get("edits", []) or [],
        deferred=payload.get("deferred", []) or [],
        usage=usage,
        stop_reason="tool_use",
    )


# --------------------------------------------------------------------------
# Anthropic Messages API
# --------------------------------------------------------------------------

def _request_anthropic(system_prompt: str, user_prompt: str, budget: float) -> ModelEdits:
    api_key = _require_env("ANTHROPIC_API_KEY")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

    response = _post_with_retry(
        "anthropic",
        budget_seconds=budget,
        url="https://api.anthropic.com/v1/messages",
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 4096,
            "temperature": 0,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": TOOL_DESCRIPTION,
                    "input_schema": TOOL_INPUT_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        },
    )
    body = response.json()
    for block in body.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
            usage = body.get("usage", {})
            return _as_model_edits(
                block["input"],
                {
                    "inputTokens": usage.get("input_tokens", 0),
                    "outputTokens": usage.get("output_tokens", 0),
                },
            )
    raise ProviderError("anthropic returned no tool_use block")


# --------------------------------------------------------------------------
# Google Gemini generateContent
# --------------------------------------------------------------------------

# Gemini rejects an OBJECT with no declared properties, and our `args` field is
# deliberately free-form. So for Gemini only, `args` is declared as a JSON string
# and parsed back on the way out. Contained entirely in this provider.
def _gemini_schema() -> dict:
    return {
        "type": "OBJECT",
        "properties": {
            "edits": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "violation_id": {"type": "STRING"},
                        "selector": {"type": "STRING"},
                        "op": {
                            "type": "STRING",
                            "enum": TOOL_INPUT_SCHEMA["properties"]["edits"]["items"][
                                "properties"
                            ]["op"]["enum"],
                        },
                        "args_json": {
                            "type": "STRING",
                            "description": (
                                "A JSON object literal of the operation arguments. "
                                'Example: {"name":"alt","value":""}'
                            ),
                        },
                        "rationale": {"type": "STRING"},
                    },
                    "required": ["violation_id", "selector", "op", "args_json", "rationale"],
                },
            },
            "deferred": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "violation_id": {"type": "STRING"},
                        "selector": {"type": "STRING"},
                        "reason": {"type": "STRING"},
                    },
                    "required": ["violation_id", "selector", "reason"],
                },
            },
        },
        "required": ["edits", "deferred"],
    }


def _unpack_gemini_args(payload: dict) -> dict:
    unpacked_edits = []
    for edit in payload.get("edits", []) or []:
        raw_args = edit.pop("args_json", "{}")
        try:
            edit["args"] = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            # Leave it malformed; the applier rejects it with a reason the
            # repair pass can act on. Better than silently dropping the edit.
            edit["args"] = {}
        unpacked_edits.append(edit)
    payload["edits"] = unpacked_edits
    return payload


# An alias, deliberately, not a pinned version. Google retires specific names
# without notice -- "no longer available" arrives as a 404 mid-run, and we have
# been bitten by it twice. The alias tracks whatever is current.
# Pin a version via GEMINI_MODEL when reproducibility matters more than uptime.
GEMINI_DEFAULT_MODEL = "gemini-flash-latest"

GEMINI_MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "16384"))
GEMINI_THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))


def _describe_gemini_failure(body: dict) -> str:
    """Say what actually went wrong instead of 'no functionCall'."""
    candidates = body.get("candidates", [])
    if not candidates:
        feedback = body.get("promptFeedback", {})
        if feedback.get("blockReason"):
            return f"prompt blocked: {feedback['blockReason']}"
        return f"no candidates returned: {json.dumps(body)[:300]}"

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason", "unknown")
    parts = candidate.get("content", {}).get("parts", [])
    usage = body.get("usageMetadata", {})

    if finish_reason == "MAX_TOKENS":
        thoughts = usage.get("thoughtsTokenCount", 0)
        return (
            f"hit MAX_TOKENS before emitting the tool call "
            f"(thinking used {thoughts} tokens of {GEMINI_MAX_OUTPUT_TOKENS}). "
            f"Raise GEMINI_MAX_OUTPUT_TOKENS or keep GEMINI_THINKING_BUDGET=0."
        )
    if finish_reason in {"SAFETY", "RECITATION", "PROHIBITED_CONTENT"}:
        return f"response stopped: finishReason={finish_reason}"

    text_parts = [part["text"] for part in parts if "text" in part]
    if text_parts:
        return (
            f"answered with prose instead of calling the tool "
            f"(finishReason={finish_reason}): {text_parts[0][:200]}"
        )
    return f"no functionCall and no text (finishReason={finish_reason})"


def _request_gemini(system_prompt: str, user_prompt: str, budget: float) -> ModelEdits:
    api_key = _require_env("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL)

    response = _post_with_retry(
        "gemini",
        budget_seconds=budget,
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        timeout=REQUEST_TIMEOUT_SECONDS,
        params={"key": api_key},
        json={
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": TOOL_NAME,
                            "description": TOOL_DESCRIPTION,
                            "parameters": _gemini_schema(),
                        }
                    ]
                }
            ],
            "toolConfig": {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [TOOL_NAME],
                }
            },
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS,
                # Gemini 2.5 models think by default and thinking tokens are
                # billed against maxOutputTokens. A long prompt plus thinking
                # exhausts the budget, the candidate comes back with no parts,
                # and the call looks like an unexplained failure. Budget 0
                # disables it; set GEMINI_THINKING_BUDGET to re-enable.
                "thinkingConfig": {"thinkingBudget": GEMINI_THINKING_BUDGET},
            },
        },
    )
    body = response.json()
    for part in (
        body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    ):
        function_call = part.get("functionCall")
        if function_call and function_call.get("name") == TOOL_NAME:
            usage = body.get("usageMetadata", {})
            return _as_model_edits(
                _unpack_gemini_args(dict(function_call.get("args", {}))),
                {
                    "inputTokens": usage.get("promptTokenCount", 0),
                    "outputTokens": usage.get("candidatesTokenCount", 0),
                },
            )
    raise ProviderError(f"gemini: {_describe_gemini_failure(body)}")


# --------------------------------------------------------------------------
# OpenAI-compatible Chat Completions (Groq, OpenRouter, Together, Ollama)
# --------------------------------------------------------------------------

# Everything here speaks the same Chat Completions API; only the address, the
# key variable and the default model differ. Spelling them out means
# MODEL_PROVIDER=gemini,groq works with GROQ_API_KEY alone, instead of needing
# three OPENAI_* variables set to Groq's values.
OPENAI_COMPATIBLE_DEFAULTS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "model_env": "OPENAI_MODEL",
        "model": "gpt-4o-mini",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "model": "llama-3.3-70b-versatile",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "model": "meta-llama/llama-3.3-70b-instruct",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "key_env": "OLLAMA_API_KEY",
        "model_env": "OLLAMA_MODEL",
        "model": "llama3.1",
    },
}


def _openai_compatible_settings(provider: str) -> tuple[str, str, str]:
    """(api_key, model, base_url) for one OpenAI-compatible provider.

    OPENAI_BASE_URL and OPENAI_MODEL still override, so an unusual endpoint does
    not need a new entry in the table above.
    """
    defaults = OPENAI_COMPATIBLE_DEFAULTS.get(
        provider, OPENAI_COMPATIBLE_DEFAULTS["openai"]
    )

    api_key = os.environ.get(defaults["key_env"], "").strip()
    if not api_key and provider != "ollama":
        # Fall back to the generic name, then complain about the specific one.
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key and provider != "ollama":
        raise ProviderConfigError(
            f"{defaults['key_env']} is not set but {provider} is in the provider "
            f"chain. Either set it, drop {provider} from MODEL_PROVIDER, or run "
            f"with --provider mock."
        )

    model = (
        os.environ.get(defaults["model_env"], "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()
        or defaults["model"]
    )
    base_url = (
        os.environ.get("OPENAI_BASE_URL", "").strip() or defaults["base_url"]
    ).rstrip("/")

    return api_key, model, base_url


def _request_openai_compatible(
    system_prompt: str,
    user_prompt: str,
    budget: float,
    provider: str = "openai",
) -> ModelEdits:
    api_key, model, base_url = _openai_compatible_settings(provider)

    response = _post_with_retry(
        provider,
        budget_seconds=budget,
        url=f"{base_url}/chat/completions",
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "parameters": TOOL_INPUT_SCHEMA,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        },
    )
    body = response.json()
    tool_calls = body["choices"][0]["message"].get("tool_calls") or []
    for tool_call in tool_calls:
        if tool_call["function"]["name"] == TOOL_NAME:
            usage = body.get("usage", {})
            return _as_model_edits(
                json.loads(tool_call["function"]["arguments"]),
                {
                    "inputTokens": usage.get("prompt_tokens", 0),
                    "outputTokens": usage.get("completion_tokens", 0),
                },
            )
    raise ProviderError(f"{provider} returned no tool_calls")


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

# Providers knocked out for the rest of this process, and why. Lambda reuses a
# warm container across invocations, so this also spares the next run the same
# dead round trips -- and a redeploy or a cold start clears it, which is the
# right granularity for "someone has topped the account up".
_disabled_providers: dict[str, str] = {}


def _request_one(
    provider: str,
    system_prompt: str,
    user_prompt: str,
    rule_id: str,
    nodes: list[dict] | None,
    budget: float,
) -> ModelEdits:
    if provider == "mock":
        from agent.mock_model import generate_mock_edits

        return generate_mock_edits(rule_id, nodes or [])

    if provider == "bedrock":
        from agent.bedrock_client import request_edits as bedrock_request_edits

        return bedrock_request_edits(system_prompt, user_prompt, rule_id, nodes)

    if provider == "anthropic":
        return _request_anthropic(system_prompt, user_prompt, budget)

    if provider == "gemini":
        return _request_gemini(system_prompt, user_prompt, budget)

    if provider in {"openai", "groq", "openrouter", "ollama"}:
        return _request_openai_compatible(system_prompt, user_prompt, budget, provider)

    raise ProviderError(
        f"unknown provider {provider!r}. Known: {', '.join(sorted(KNOWN_PROVIDERS))}"
    )


def request_edits(
    system_prompt: str,
    user_prompt: str,
    rule_id: str = "",
    nodes: list[dict] | None = None,
) -> ModelEdits:
    """Ask the first provider that answers.

    Each provider gets an equal share of the total budget, so a chain of three
    cannot take three times as long as a chain of one. A provider that raises is
    logged and the next one is tried; if every one fails, the last error is
    raised with the whole chain named, because "gemini 503" alone would hide the
    fact that two others were tried too.
    """
    chain = resolve_provider_chain()
    live = [provider for provider in chain if provider not in _disabled_providers]

    # Everything is dead. Report why each one is, not just the last.
    if not live:
        raise ProviderError(
            "every provider in the chain is out of action:\n  "
            + "\n  ".join(f"{name}: {why}" for name, why in _disabled_providers.items())
        )

    per_provider_budget = MODEL_CALL_BUDGET_SECONDS / len(live)
    failures: list[str] = []

    for position, provider in enumerate(live):
        try:
            result = _request_one(
                provider, system_prompt, user_prompt, rule_id, nodes, per_provider_budget
            )
        except ProviderConfigError as error:
            # Billing, credentials or a bad model name. Asking again next rule
            # would waste a round trip per cluster to be told the same thing.
            _disabled_providers[provider] = str(error)
            failures.append(f"{provider}: {error}")
            print(
                f"provider {provider} taken out of the chain for this run -- {error}",
                flush=True,
            )
            continue
        except ProviderError as error:
            failures.append(f"{provider}: {error}")
            print(f"provider {provider} failed, falling back -- {error}", flush=True)
            continue
        except Exception as error:  # noqa: BLE001 -- a provider SDK's own error type
            failures.append(f"{provider}: {type(error).__name__}: {error}")
            print(f"provider {provider} failed, falling back -- {error}", flush=True)
            continue

        result.provider = provider
        if position:
            print(f"answered by fallback provider {provider}", flush=True)
        return result

    raise ProviderError(
        "every provider in the chain failed ("
        + " -> ".join(live)
        + "):\n  "
        + "\n  ".join(failures)
    )