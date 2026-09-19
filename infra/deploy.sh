#!/usr/bin/env bash
# One-command deploy. Docker must be running.
#
# Model configuration is passed as CloudFormation parameters so it survives
# every redeploy. Export these before running:
#   export MODEL_PROVIDER=gemini,groq,mock
#   export GEMINI_API_KEY=<your Google AI Studio key>
#   export GROQ_API_KEY=<your console.groq.com key>
#
# MODEL_PROVIDER takes a comma-separated fallback chain, tried in order. Use
# one for anything you are demoing: a free-tier 503 "high demand" has killed a
# run before, and a second provider is the only thing that helps.
set -euo pipefail
cd "$(dirname "$0")"

REGION="${AWS_REGION:-us-east-1}"
MODEL_PROVIDER="${MODEL_PROVIDER:-bedrock}"
GEMINI_API_KEY="${GEMINI_API_KEY:-}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-flash-latest}"
GROQ_API_KEY="${GROQ_API_KEY:-}"
GROQ_MODEL="${GROQ_MODEL:-llama-3.3-70b-versatile}"

# CloudFormation rejects spaces in the chain, and "gemini, groq" is the natural
# thing to type. Fix it here rather than failing 40 minutes into a build.
MODEL_PROVIDER="${MODEL_PROVIDER// /}"

# Both functions must use different Dockerfiles, or the API image ships without api/.
mapfile -t DOCKERFILES < <(grep -oP 'Dockerfile:\s*\K\S+' template.yaml)
UNIQUE=$(printf '%s\n' "${DOCKERFILES[@]}" | sort -u | wc -l)
if [ "$UNIQUE" -lt 2 ]; then
  echo "template.yaml points both functions at the same Dockerfile: ${DOCKERFILES[*]}" >&2
  exit 1
fi
for file in "${DOCKERFILES[@]}"; do
  [ -f "../$file" ] || { echo "Missing $file" >&2; exit 1; }
done
echo "Dockerfiles OK: ${DOCKERFILES[*]}"

case ",$MODEL_PROVIDER," in
  *,gemini,*) [ -n "$GEMINI_API_KEY" ] || {
      echo "gemini is in MODEL_PROVIDER but GEMINI_API_KEY is not exported." >&2
      exit 1; } ;;
esac
case ",$MODEL_PROVIDER," in
  *,groq,*) [ -n "$GROQ_API_KEY" ] || {
      echo "groq is in MODEL_PROVIDER but GROQ_API_KEY is not exported." >&2
      exit 1; } ;;
esac
echo "Model provider chain: ${MODEL_PROVIDER//,/ -> }"

# sam deploy rejects "GroqApiKey=" outright -- an empty value is not valid
# --parameter-overrides syntax. So an unset key is OMITTED and CloudFormation
# falls back to the template default (an empty string for every key here).
#
# The catch worth knowing: omitting a parameter does NOT preserve whatever the
# stack currently has, it resets it to the template default. A key you do not
# export in this shell is a key you are clearing from the deployed stack.
PARAMS=(
  "ModelProvider=$MODEL_PROVIDER"
  "GeminiModel=$GEMINI_MODEL"
  "GroqModel=$GROQ_MODEL"
)
[ -n "$GEMINI_API_KEY" ] && PARAMS+=("GeminiApiKey=$GEMINI_API_KEY") \
  || echo "warning: GEMINI_API_KEY not exported -- it will be CLEARED on the stack" >&2
[ -n "$GROQ_API_KEY" ] && PARAMS+=("GroqApiKey=$GROQ_API_KEY") \
  || echo "warning: GROQ_API_KEY not exported -- it will be CLEARED on the stack" >&2

# Sequential on purpose: --parallel splits a slow link two ways.
sam build

# `sam deploy` exits 1 for "No changes to deploy", which is not a failure -- it
# means a previous deploy already put this exact template on the stack. `set -e`
# would abort here, so the exit code is captured and inspected instead.
DEPLOY_LOG="$(mktemp)"
set +e
sam deploy \
  --stack-name a11y-agent \
  --region "$REGION" \
  --resolve-image-repos \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --disable-rollback \
  --no-confirm-changeset \
  --parameter-overrides "${PARAMS[@]}" 2>&1 | tee "$DEPLOY_LOG"
DEPLOY_STATUS=${PIPESTATUS[0]}
set -e

if [ "$DEPLOY_STATUS" -ne 0 ]; then
  if grep -q "No changes to deploy" "$DEPLOY_LOG"; then
    echo
    echo "No changes to deploy - the stack already matches this template."
  else
    echo
    echo "sam deploy failed with exit code $DEPLOY_STATUS. NOTHING WAS DEPLOYED." >&2
    rm -f "$DEPLOY_LOG"
    exit "$DEPLOY_STATUS"
  fi
fi
rm -f "$DEPLOY_LOG"

echo
echo "API URL:"
aws cloudformation describe-stacks \
  --stack-name a11y-agent \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text