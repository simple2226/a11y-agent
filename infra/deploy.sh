#!/usr/bin/env bash
# One-command deploy. Docker must be running.
set -euo pipefail
cd "$(dirname "$0")"

REGION="${AWS_REGION:-us-east-1}"

# Both functions must use different Dockerfiles, or the API image ships without api/.
mapfile -t DOCKERFILES < <(grep -oP 'Dockerfile:\s*\K\S+' template.yaml)
UNIQUE=$(printf '%s\n' "${DOCKERFILES[@]}" | sort -u | wc -l)
if [ "$UNIQUE" -lt 2 ]; then
  echo "template.yaml points both functions at the same Dockerfile: ${DOCKERFILES[*]}" >&2
  echo "Line ~91 should read 'Dockerfile: infra/Dockerfile.api'." >&2
  exit 1
fi
for file in "${DOCKERFILES[@]}"; do
  [ -f "../$file" ] || { echo "Missing $file" >&2; exit 1; }
done
echo "Dockerfiles OK: ${DOCKERFILES[*]}"

# Sequential on purpose: --parallel splits a slow link two ways.
sam build

# --resolve-image-repos creates the ECR repos; --resolve-s3 creates the managed
# bucket for the template. Both are needed; neither implies the other.
sam deploy \
  --stack-name a11y-agent \
  --region "$REGION" \
  --resolve-image-repos \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --disable-rollback \
  --no-confirm-changeset

echo
echo "API URL:"
aws cloudformation describe-stacks \
  --stack-name a11y-agent \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text