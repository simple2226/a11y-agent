# One-command deploy on Windows. Docker Desktop must be running.
#
# Model configuration is passed as CloudFormation parameters so it survives
# every redeploy. Set these in your shell before running:
#   $env:MODEL_PROVIDER = "gemini"
#   $env:GEMINI_API_KEY = "<your Google AI Studio key>"
# The key is never written to a file and never reaches the repo.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Region = if ($env:AWS_REGION) { $env:AWS_REGION } else { "us-east-1" }
$ModelProvider = if ($env:MODEL_PROVIDER) { $env:MODEL_PROVIDER } else { "bedrock" }
$GeminiApiKey = if ($env:GEMINI_API_KEY) { $env:GEMINI_API_KEY } else { "" }
$GeminiModel = if ($env:GEMINI_MODEL) { $env:GEMINI_MODEL } else { "gemini-flash-latest" }

# Guard against the mistake that costs 40 minutes: both functions pointing at
# the same Dockerfile means the API image ships without api/ in it.
$dockerfiles = Select-String -Path .\template.yaml -Pattern "Dockerfile:\s*(\S+)" |
    ForEach-Object { $_.Matches[0].Groups[1].Value }
if (($dockerfiles | Select-Object -Unique).Count -lt 2) {
    Write-Error "template.yaml points both functions at the same Dockerfile: $dockerfiles`nLine ~91 should read 'Dockerfile: infra/Dockerfile.api'."
}
foreach ($file in $dockerfiles) {
    if (-not (Test-Path (Join-Path ".." $file))) { Write-Error "Missing $file" }
}
Write-Host "Dockerfiles OK: $dockerfiles"

if ($ModelProvider -eq "gemini" -and -not $GeminiApiKey) {
    Write-Error "MODEL_PROVIDER is gemini but GEMINI_API_KEY is not set in this shell."
}
Write-Host "Model provider: $ModelProvider`n"

# Sequential on purpose: --parallel splits a slow link two ways.
sam build

# --resolve-image-repos creates the ECR repos for the container images.
# --resolve-s3 creates the managed bucket for the CloudFormation template.
# Both are needed; neither implies the other.
sam deploy `
  --stack-name a11y-agent `
  --region $Region `
  --resolve-image-repos `
  --resolve-s3 `
  --capabilities CAPABILITY_IAM `
  --disable-rollback `
  --no-confirm-changeset `
  --parameter-overrides "ModelProvider=$ModelProvider" "GeminiApiKey=$GeminiApiKey" "GeminiModel=$GeminiModel"

Write-Host "`nAPI URL:"
aws cloudformation describe-stacks `
  --stack-name a11y-agent `
  --region $Region `
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" `
  --output text