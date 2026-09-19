#!/usr/bin/env python3
"""
Ask the provider what this key can actually call, before a run depends on it.

    python scripts/check_model.py                 # uses MODEL_PROVIDER
    python scripts/check_model.py --provider gemini

Hosted model names are retired without notice. This lists what is available now
and probes the configured one with a real forced tool call, because a model that
answers text can still reject the tool schema the agent depends on.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from agent.schema import TOOL_NAME


def check_gemini() -> int:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("GEMINI_API_KEY is not set.")
        return 1

    response = httpx.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": key, "pageSize": 200},
        timeout=30,
    )
    if response.status_code != 200:
        print(f"list models failed ({response.status_code}): {response.text[:300]}")
        return 1

    usable = [
        model
        for model in response.json().get("models", [])
        if "generateContent" in model.get("supportedGenerationMethods", [])
    ]
    print(f"{len(usable)} model(s) support generateContent:\n")
    for model in sorted(usable, key=lambda m: m["name"]):
        name = model["name"].removeprefix("models/")
        print(f"  {name}")

    configured = os.environ.get("GEMINI_MODEL", "")
    if configured:
        names = {m["name"].removeprefix("models/") for m in usable}
        print()
        if configured in names:
            print(f"GEMINI_MODEL={configured} is available.")
        else:
            print(f"GEMINI_MODEL={configured} is NOT in the list above. Pick one that is.")
            return 1
    return 0


def probe_tool_call() -> int:
    """A model can answer text and still refuse the forced tool call."""
    from agent.model_provider import request_edits, active_model_label

    print(f"\nprobing a forced tool call against {active_model_label()} ...")
    try:
        result = request_edits(
            "You emit accessibility fixes as structured DOM edits.",
            'The rule "html-has-lang" failed on the <html> element, which has no '
            "lang attribute. Emit one set_attribute edit adding lang=\"en\".",
            rule_id="html-has-lang",
            nodes=[{"target": ["html"], "html": "<html>"}],
        )
    except Exception as error:
        print(f"  FAILED: {error}")
        return 1

    print(f"  ok -- {len(result.edits)} edit(s), {len(result.deferred)} deferred")
    if result.edits:
        print(f"  {json.dumps(result.edits[0], indent=2)[:300]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", help="Overrides MODEL_PROVIDER for this check.")
    parser.add_argument("--skip-probe", action="store_true")
    arguments = parser.parse_args()

    if arguments.provider:
        os.environ["MODEL_PROVIDER"] = arguments.provider

    provider = os.environ.get("MODEL_PROVIDER", "bedrock")
    print(f"provider: {provider}\n")

    status = 0
    if provider == "gemini":
        status = check_gemini()
    elif provider == "bedrock":
        print("For Bedrock, run scripts/check_bedrock.py -- it probes tool use per model.")
        return 0

    if status == 0 and not arguments.skip_probe:
        status = probe_tool_call()
    return status


if __name__ == "__main__":
    sys.exit(main())