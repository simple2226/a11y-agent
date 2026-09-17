#!/usr/bin/env bash
# Local dev setup. Run once.
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium

echo
echo "Checking Bedrock model access in ${BEDROCK_REGION:-us-east-1}..."
aws bedrock list-foundation-models \
  --region "${BEDROCK_REGION:-us-east-1}" \
  --query "modelSummaries[?contains(modelId,'claude-sonnet')].modelId" \
  --output table || echo "  bedrock check failed - request model access in the console"

echo
echo "Done. Try:  python local_run.py --fixture evals/fixtures/broken-demo.html --no-agent"
