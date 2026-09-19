# One-command deploy on Windows. Docker Desktop must be running.
#
# Model configuration is passed as CloudFormation parameters so it survives
# every redeploy. Set these in your shell before running:
#   $env:MODEL_PROVIDER = "gemini,groq,mock"
#   $env:GEMINI_API_KEY = "<your Google AI Studio key>"
#   $env:GROQ_API_KEY   = "<your console.groq.com key>"
# Keys are never written to a file and never reach the repo.
#
# MODEL_PROVIDER takes a comma-separated fallback chain, tried in order. Use one
# for anything you are demoing: a free-tier 503 "high demand" has killed a run
# before, and a second provider is the only thing that helps.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Region = if ($env:AWS_REGION) { $env:AWS_REGION } else { "us-east-1" }
$ModelProvider = if ($env:MODEL_PROVIDER) { $env:MODEL_PROVIDER } else { "bedrock" }
$GeminiApiKey = if ($env:GEMINI_API_KEY) { $env:GEMINI_API_KEY } else { "" }
$GeminiModel = if ($env:GEMINI_MODEL) { $env:GEMINI_MODEL } else { "gemini-flash-latest" }
$GroqApiKey = if ($env:GROQ_API_KEY) { $env:GROQ_API_KEY } else { "" }
$GroqModel = if ($env:GROQ_MODEL) { $env:GROQ_MODEL } else { "llama-3.3-70b-versatile" }

# CloudFormation rejects spaces in the chain, and "gemini, groq" is the natural
# thing to type. Fix it here rather than failing 40 minutes into a build.
$ModelProvider = ($ModelProvider -replace "\s", "")
$ProviderChain = $ModelProvider -split ","

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

if (($ProviderChain -contains "gemini") -and -not $GeminiApiKey) {
    Write-Error "gemini is in MODEL_PROVIDER but GEMINI_API_KEY is not set in this shell."
}
if (($ProviderChain -contains "groq") -and -not $GroqApiKey) {
    Write-Error "groq is in MODEL_PROVIDER but GROQ_API_KEY is not set in this shell."
}
Write-Host "Model provider chain: $($ProviderChain -join ' -> ')`n"

# The files on disk and the files you think are on disk are different claims.
# A chain like "groq,gemini,mock" fails CreateChangeSet against a template that
# still declares AllowedValues -- twenty minutes of build, then a validation
# error. Check it now instead.
if ($ProviderChain.Count -gt 1) {
    if (Select-String -Path .\template.yaml -Pattern "AllowedValues:\s*\[bedrock" -Quiet) {
        Write-Error @"
template.yaml still has 'AllowedValues: [bedrock, gemini, ...]' on ModelProvider.
That rejects a comma-separated chain, and the deploy would fail at the changeset.
This file is the OLD version -- replace infra/template.yaml, then re-run.
"@
    }
}
if (-not (Select-String -Path .\template.yaml -Pattern "RUN_BUDGET_SECONDS" -Quiet)) {
    Write-Error "template.yaml has no RUN_BUDGET_SECONDS -- it is the OLD version. Replace infra/template.yaml, then re-run."
}
if (-not (Select-String -Path ..\agent\model_provider.py -Pattern "resolve_provider_chain" -Quiet)) {
    Write-Error "agent/model_provider.py has no resolve_provider_chain -- it is the OLD version. Replace it, then re-run."
}
Write-Host "Source files are the current versions.`n"

# $ErrorActionPreference = "Stop" does NOT stop on a native executable's exit
# code in Windows PowerShell -- only on PowerShell's own errors. Without these
# explicit checks, `sam deploy` can fail with "Parameter 'ModelProvider' must be
# one of AllowedValues" and the script carries on to print an API URL, which
# reads exactly like a successful deploy. That cost us an hour.
function Assert-LastExitCode([string]$what) {
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Error "$what failed with exit code $LASTEXITCODE. NOTHING WAS DEPLOYED -- the error is above."
        exit $LASTEXITCODE
    }
}

# Sequential on purpose: --parallel splits a slow link two ways.
sam build
Assert-LastExitCode "sam build"

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
  --parameter-overrides "ModelProvider=$ModelProvider" "GeminiApiKey=$GeminiApiKey" "GeminiModel=$GeminiModel" "GroqApiKey=$GroqApiKey" "GroqModel=$GroqModel"

Assert-LastExitCode "sam deploy"

Write-Host "`nAPI URL:"
aws cloudformation describe-stacks `
  --stack-name a11y-agent `
  --region $Region `
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" `
  --output text

# Read back what is actually running. "I edited the file" and "the Lambda is
# running that file" are different claims, and three hours were lost to
# assuming the first implied the second.
Write-Host "`nDeployed agent configuration:"
$agentFn = aws lambda list-functions `
  --region $Region `
  --query "Functions[?starts_with(FunctionName, 'a11y-agent-AgentFunction')].FunctionName | [0]" `
  --output text

if ($agentFn -and $agentFn -ne "None") {
    aws lambda get-function-configuration `
      --function-name $agentFn `
      --region $Region `
      --query "{lastModified: LastModified, provider: Environment.Variables.MODEL_PROVIDER, runBudget: Environment.Variables.RUN_BUDGET_SECONDS}" `
      --output table

    $budget = aws lambda get-function-configuration `
      --function-name $agentFn --region $Region `
      --query "Environment.Variables.RUN_BUDGET_SECONDS" --output text

    if (-not $budget -or $budget -eq "None") {
        Write-Warning "RUN_BUDGET_SECONDS is absent -- the stack did not pick up the current template. The deploy did not take."
    } else {
        Write-Host "Deploy verified: the running Lambda has the current configuration." -ForegroundColor Green
    }
} else {
    Write-Warning "Could not find the agent Lambda to verify against."
}