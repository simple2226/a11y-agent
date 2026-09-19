#!/usr/bin/env python3
"""
Diagnose and fix Bedrock model access from the CLI.

    python scripts/bedrock_enable.py --check
    python scripts/bedrock_enable.py --submit-form --github https://github.com/YOU
    python scripts/bedrock_enable.py --enable anthropic.claude-sonnet-4-5-20250929-v1:0

GetFoundationModelAvailability breaks "Operation not allowed" into four separate
gates, so you stop guessing:

    regionAvailability      NOT_AVAILABLE -> wrong region for this model
    authorizationStatus     NOT_AUTHORIZED -> ACCOUNT-level block (support case)
    entitlementAvailability NOT_AVAILABLE -> Marketplace: payment method or
                                             aws-marketplace:* IAM permissions
    agreementAvailability   NOT_AVAILABLE -> agreement not created yet; run
                                             --enable (and --submit-form first
                                             for Anthropic)

Requires AWS CLI 2.27.42+ / a recent boto3, and AmazonBedrockFullAccess on the
IAM principal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("BEDROCK_REGION", "us-east-1")

MODELS_TO_CHECK = [
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "amazon.nova-pro-v1:0",
    "meta.llama3-3-70b-instruct-v1:0",
]

GATE_HINTS = {
    "regionAvailability": "model is not offered in this region -- try us-west-2",
    "authorizationStatus": (
        "ACCOUNT-LEVEL BLOCK. No model id, region or form fixes this. "
        "Open a support case under Account and billing (free on Basic support)."
    ),
    "entitlementAvailability": (
        "AWS Marketplace gate. Two usual causes:\n"
        "        (a) no valid payment method on the account "
        "(Billing console -> Payment preferences), or\n"
        "        (b) the IAM principal lacks aws-marketplace:Subscribe / "
        "Unsubscribe / ViewSubscriptions.\n"
        "        Attach AmazonBedrockFullAccess plus the marketplace actions."
    ),
    "agreementAvailability": (
        "no agreement yet. For Anthropic run --submit-form first, then --enable <modelId>."
    ),
}


def bedrock_client():
    return boto3.client("bedrock", region_name=REGION)


def _gate_values(response: dict) -> dict[str, str]:
    agreement = response.get("agreementAvailability", {})
    return {
        "regionAvailability": response.get("regionAvailability", "?"),
        "authorizationStatus": response.get("authorizationStatus", "?"),
        "entitlementAvailability": response.get("entitlementAvailability", "?"),
        "agreementAvailability": agreement.get("status", "?")
        if isinstance(agreement, dict)
        else str(agreement),
    }


GOOD_VALUES = {"AVAILABLE", "AUTHORIZED"}


def check(model_ids: list[str]) -> int:
    client = bedrock_client()
    failing_gates: set[str] = set()

    for model_id in model_ids:
        print(f"\n{model_id}")
        try:
            response = client.get_foundation_model_availability(modelId=model_id)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            message = error.response.get("Error", {}).get("Message", "")
            print(f"  ERROR {code}: {message[:200]}")
            if code == "ValidationException" and "invalid" in message.lower():
                print("  (model id not recognised in this region)")
            continue

        gates = _gate_values(response)
        for gate_name, value in gates.items():
            marker = "ok  " if value in GOOD_VALUES else "FAIL"
            print(f"  [{marker}] {gate_name:<24} {value}")
            if value not in GOOD_VALUES:
                failing_gates.add(gate_name)

    if not failing_gates:
        print("\nAll gates open. Bedrock should work -- rerun scripts/check_bedrock.py")
        return 0

    print("\nWHAT TO FIX, in order:")
    for gate_name in (
        "regionAvailability",
        "authorizationStatus",
        "entitlementAvailability",
        "agreementAvailability",
    ):
        if gate_name in failing_gates:
            print(f"\n  {gate_name}:\n        {GATE_HINTS[gate_name]}")
    return 1


def submit_form(
    company_name: str,
    company_website: str,
    industry: str,
    use_cases: str,
) -> int:
    """PutUseCaseForModelAccess. One-time per account (or per org management
    account) across all commercial regions. Anthropic models only.

    The form wants a company website. AWS documents that an individual developer
    or student without one can supply a personal portfolio, GitHub profile, or
    project URL instead.
    """
    form_data = {
        "companyName": company_name[:128],
        "companyWebsite": company_website[:128],
        "intendedUsers": "0",  # 0 internal, 1 external, 2 both
        "industryOption": industry[:128],
        "otherIndustryOption": "",
        "useCases": use_cases[:8192],
    }
    print("submitting use-case form:")
    print(json.dumps(form_data, indent=2))

    try:
        bedrock_client().put_use_case_for_model_access(
            formData=json.dumps(form_data).encode("utf-8")
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        message = error.response.get("Error", {}).get("Message", "")
        print(f"\nFAILED {code}: {message}")
        if "not authorized" in message.lower() or "support case" in message.lower():
            print(
                "\nThe API refuses the form too, which means the block is above the\n"
                "form: the account is not authorised for Bedrock at all. Run --check\n"
                "and look at authorizationStatus. That needs a support case."
            )
        return 1

    print("\nSubmitted. Access is granted immediately on success. Now run:")
    print("  python scripts/bedrock_enable.py --check")
    return 0


def enable(model_id: str) -> int:
    client = bedrock_client()

    try:
        offers_response = client.list_foundation_model_agreement_offers(
            modelId=model_id, offerType="ALL"
        )
    except ClientError as error:
        print(f"list offers failed: {error}")
        return 1

    offers = offers_response.get("offers", [])
    if not offers:
        print(f"no agreement offers for {model_id} (Amazon/Meta/Mistral models have none)")
        return 1

    offer_token = offers[0]["offerToken"]
    print(f"offer found ({len(offers)} total), creating agreement...")

    try:
        client.create_foundation_model_agreement(offerToken=offer_token, modelId=model_id)
    except ClientError as error:
        print(f"create agreement failed: {error}")
        return 1

    print("agreement created. Verifying:")
    return check([model_id])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="diagnose the four gates")
    parser.add_argument("--submit-form", action="store_true", help="Anthropic FTU form")
    parser.add_argument("--enable", metavar="MODEL_ID", help="create the model agreement")
    parser.add_argument("--model", action="append", help="model id to check (repeatable)")
    parser.add_argument("--company", default="Independent student project")
    parser.add_argument("--github", help="your GitHub profile or project URL")
    parser.add_argument("--industry", default="Education")
    parser.add_argument(
        "--use-cases",
        default=(
            "Automated web accessibility auditing and remediation. The application "
            "audits public web pages against WCAG 2.1 AA using axe-core, and uses a "
            "foundation model to generate structured DOM edit operations that resolve "
            "the detected violations. Output is verified by re-running the audit. "
            "Built for an academic hackathon; no personal data is processed."
        ),
    )
    arguments = parser.parse_args()

    print(f"region: {REGION}")
    print(f"identity: {boto3.client('sts', region_name=REGION).get_caller_identity()['Arn']}")

    if arguments.submit_form:
        if not arguments.github:
            parser.error("--submit-form needs --github <your profile or project URL>")
        return submit_form(
            arguments.company, arguments.github, arguments.industry, arguments.use_cases
        )

    if arguments.enable:
        return enable(arguments.enable)

    return check(arguments.model or MODELS_TO_CHECK)


if __name__ == "__main__":
    sys.exit(main())
