# One-command deploy on Windows. Docker Desktop must be running.
#
# Model configuration is passed as CloudFormation parameters so it survives
# every redeploy. Set these in the SAME shell you run this from:
#   $env:MODEL_PROVIDER = "groq,gemini,mock"
#   $env:GROQ_API_KEY   = "<your console.groq.com key>"
#   $env:GEMINI_API_KEY = "<your Google AI Studio key>"
# Keys are never written to a file and never reach the repo.
#
# MODEL_PROVIDER takes a comma-separated fallback chain, tried in order. Use one
# for anything you are demoing: a free-tier 503 "high demand" and a 402 "credits
# depleted" have each killed a run, and a second provider is the only real cure.
#
# PowerShell note, learned the hard way: this script deliberately does NOT set
# $ErrorActionPreference = "Stop". sam writes all its progress to stderr, and
# under "Stop" the first stderr line of a redirected native command becomes a
# terminating NativeCommandError -- the deploy dies before it starts. Failures
# are handled explicitly below instead, which is more reliable anyway: "Stop"
# never caught a native command's exit code to begin with.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Fail([string]$message) {
    Write-Host ""
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit 1
}

$Region = if ($env:AWS_REGION) { $env:AWS_REGION } else { "us-east-1" }
$ModelProvider = if ($env:MODEL_PROVIDER) { $env:MODEL_PROVIDER } else { "bedrock" }
$GeminiApiKey = if ($env:GEMINI_API_KEY) { $env:GEMINI_API_KEY } else { "" }
$GeminiModel = if ($env:GEMINI_MODEL) { $env:GEMINI_MODEL } else { "gemini-flash-latest" }
$GroqApiKey = if ($env:GROQ_API_KEY) { $env:GROQ_API_KEY } else { "" }
$GroqModel = if ($env:GROQ_MODEL) { $env:GROQ_MODEL } else { "llama-3.3-70b-versatile" }

# CloudFormation rejects spaces in the chain, and "groq, gemini" is the natural
# thing to type. Fix it here rather than failing 20 minutes into a build.
$ModelProvider = ($ModelProvider -replace "\s", "")
$ProviderChain = @($ModelProvider -split ",")

# ---------------------------------------------------------------- preflight

# Both functions must use different Dockerfiles, or the API image ships without
# api/ in it and every request 500s on an import error.
$dockerfiles = Select-String -Path .\template.yaml -Pattern "Dockerfile:\s*(\S+)" |
    ForEach-Object { $_.Matches[0].Groups[1].Value }
if (($dockerfiles | Select-Object -Unique).Count -lt 2) {
    Fail "template.yaml points both functions at the same Dockerfile: $dockerfiles"
}
foreach ($file in $dockerfiles) {
    if (-not (Test-Path (Join-Path ".." $file))) { Fail "Missing $file" }
}
Write-Host "Dockerfiles OK: $dockerfiles"

if (($ProviderChain -contains "gemini") -and -not $GeminiApiKey) {
    Fail "gemini is in MODEL_PROVIDER but GEMINI_API_KEY is not set in this shell."
}
if (($ProviderChain -contains "groq") -and -not $GroqApiKey) {
    Fail "groq is in MODEL_PROVIDER but GROQ_API_KEY is not set in this shell."
}
Write-Host "Model provider chain: $($ProviderChain -join ' -> ')`n"

# The files on disk and the files you think are on disk are different claims.
# A chain like "groq,gemini,mock" fails CreateChangeSet against a template that
# still declares AllowedValues -- a full build, then a validation error.
if ($ProviderChain.Count -gt 1 -and
    (Select-String -Path .\template.yaml -Pattern "AllowedValues:\s*\[bedrock" -Quiet)) {
    Fail "template.yaml still constrains ModelProvider with AllowedValues, which rejects a chain. Replace infra/template.yaml with the current version."
}
if (-not (Select-String -Path .\template.yaml -Pattern "RUN_BUDGET_SECONDS" -Quiet)) {
    Fail "template.yaml has no RUN_BUDGET_SECONDS -- it is the OLD version. Replace infra/template.yaml."
}
# Every Python file the agent imports at cold start, with a marker that only
# exists in its current version. A mismatch here used to surface as
# "Runtime.ImportModuleError: cannot import name X from agent.graph" AFTER a
# full build and deploy -- the Lambda dying before a line of our code ran.
$sourceMarkers = @(
    @{ Path = "..\agent\model_provider.py"; Marker = "resolve_provider_chain" },
    @{ Path = "..\agent\graph.py";          Marker = "RUN_BUDGET_SECONDS" },
    @{ Path = "..\agent\graph.py";          Marker = "node_baseline" },
    @{ Path = "..\agent\graph.py";          Marker = "unverified" },
    @{ Path = "..\agent\deterministic.py";  Marker = "deterministic_edits" },
    @{ Path = "..\agent\handler.py";        Marker = "_record_failure" },
    @{ Path = "..\audit\runner.py";         Marker = "_check_data" },
    @{ Path = "..\audit\runner.py";         Marker = "chromium_launch_args\(attempt" },
    @{ Path = "..\audit\runner.py";         Marker = "sweep_scratch_space" },
    @{ Path = "..\infra\template.yaml";     Marker = "EphemeralStorage" }
)
foreach ($check in $sourceMarkers) {
    if (-not (Test-Path $check.Path)) {
        Fail "$($check.Path) is missing. The agent will not import without it."
    }
    if (-not (Select-String -Path $check.Path -Pattern $check.Marker -Quiet)) {
        Fail "$($check.Path) has no '$($check.Marker)' -- it is an OLD version. Replace it, then re-run."
    }
}
Write-Host "Source files are the current versions.`n"

# ------------------------------------------------------------- parameters

# sam deploy rejects "GroqApiKey=" outright -- an empty value is not valid
# --parameter-overrides syntax. So an unset key is OMITTED and CloudFormation
# falls back to the template default (an empty string for every key here).
#
# Worth knowing: omitting a parameter does NOT preserve what the stack currently
# has, it resets it to the template default. A key you do not export in this
# shell is a key you are clearing from the deployed stack.
$ParameterOverrides = @(
    "ModelProvider=$ModelProvider",
    "GeminiModel=$GeminiModel",
    "GroqModel=$GroqModel"
)
$ClearedKeys = @()

foreach ($secret in @(
    @{ Name = "GeminiApiKey"; Value = $GeminiApiKey },
    @{ Name = "GroqApiKey";   Value = $GroqApiKey }
)) {
    if ($secret.Value) {
        $ParameterOverrides += "$($secret.Name)=$($secret.Value)"
    } else {
        $ClearedKeys += $secret.Name
    }
}

if ($ClearedKeys.Count) {
    Write-Warning "Not set in this shell, so it will be CLEARED on the stack: $($ClearedKeys -join ', ')"
    Write-Warning "Environment variables do not survive a new terminal."
}

# ------------------------------------------------------------------ build

# Sequential on purpose: --parallel splits a slow link two ways.
sam build
if ($LASTEXITCODE -ne 0) { Fail "sam build failed with exit code $LASTEXITCODE. NOTHING WAS DEPLOYED." }

# ----------------------------------------------------------------- deploy

# --resolve-image-repos creates the ECR repos for the container images.
# --resolve-s3 creates the managed bucket for the CloudFormation template.
# Both are needed; neither implies the other.
$DeployLogPath = Join-Path ([System.IO.Path]::GetTempPath()) "a11y-agent-sam-deploy.log"

$deployArgs = @(
    "deploy",
    "--stack-name", "a11y-agent",
    "--region", $Region,
    "--resolve-image-repos",
    "--resolve-s3",
    "--capabilities", "CAPABILITY_IAM",
    "--disable-rollback",
    "--no-confirm-changeset",
    "--parameter-overrides"
) + $ParameterOverrides

& sam @deployArgs 2>&1 | Tee-Object -FilePath $DeployLogPath
$DeployExitCode = $LASTEXITCODE

# sam deploy exits 1 for "No changes to deploy", which is not a failure -- it
# means a previous deploy already put this exact template on the stack.
if ($DeployExitCode -ne 0) {
    if (Select-String -Path $DeployLogPath -Pattern "No changes to deploy" -Quiet) {
        Write-Host "`nNo changes to deploy - the stack already matches this template." -ForegroundColor Green
    } else {
        Fail "sam deploy failed with exit code $DeployExitCode. NOTHING WAS DEPLOYED -- the error is above."
    }
}

# ----------------------------------------------------------------- verify

Write-Host "`nAPI URL:"
aws cloudformation describe-stacks `
  --stack-name a11y-agent `
  --region $Region `
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" `
  --output text

# Read back what is actually running. "I edited the file" and "the Lambda is
# running that file" are different claims, and hours were lost to assuming the
# first implied the second.
Write-Host "`nDeployed agent configuration:"
$agentFn = aws lambda list-functions `
  --region $Region `
  --query "Functions[?starts_with(FunctionName, 'a11y-agent-AgentFunction')].FunctionName | [0]" `
  --output text

if (-not $agentFn -or $agentFn -eq "None") {
    Fail "Could not find the agent Lambda to verify against."
}

aws lambda get-function-configuration `
  --function-name $agentFn `
  --region $Region `
  --query "{lastModified: LastModified, provider: Environment.Variables.MODEL_PROVIDER, runBudget: Environment.Variables.RUN_BUDGET_SECONDS}" `
  --output table

$budget = aws lambda get-function-configuration `
  --function-name $agentFn --region $Region `
  --query "Environment.Variables.RUN_BUDGET_SECONDS" --output text
$liveProvider = aws lambda get-function-configuration `
  --function-name $agentFn --region $Region `
  --query "Environment.Variables.MODEL_PROVIDER" --output text

if (-not $budget -or $budget -eq "None") {
    Fail "RUN_BUDGET_SECONDS is absent on the running Lambda -- the stack did not pick up the current template."
}
if ($liveProvider -ne $ModelProvider) {
    Write-Warning "Deployed provider chain is '$liveProvider' but this shell asked for '$ModelProvider'."
}

Write-Host "Deploy verified: the running Lambda has the current configuration." -ForegroundColor Green