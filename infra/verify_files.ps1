# Which project files are the current versions? Run from the repo root.
$checks = @(
    @{ File = "infra\template.yaml";        Marker = "RUN_BUDGET_SECONDS" },
    @{ File = "infra\template.yaml";        Marker = "GroqApiKey" },
    @{ File = "infra\deploy.ps1";           Marker = "GroqApiKey" },
    @{ File = "agent\model_provider.py";    Marker = "resolve_provider_chain" },
    @{ File = "agent\model_provider.py";    Marker = "ProviderConfigError" },
    @{ File = "agent\deterministic.py";     Marker = "deterministic_edits" },
    @{ File = "agent\graph.py";             Marker = "node_baseline" },
    @{ File = "agent\graph.py";             Marker = "cluster_best_html" },
    @{ File = "audit\runner.py";            Marker = "_check_data" },
    @{ File = "audit\mirror.py";            Marker = "MirrorError" },
    @{ File = "agent\bedrock_client.py";    Marker = "provider: str" },
    @{ File = "agent\graph.py";             Marker = "RUN_BUDGET_SECONDS" },
    @{ File = "agent\handler.py";           Marker = "_record_failure" },
    @{ File = "audit\mirror.py";            Marker = "PAGE_FETCH_ATTEMPTS" },
    @{ File = "storage\dynamo_store.py";    Marker = "update_page_fields" },
    @{ File = "api\main.py";                Marker = "pagesFailed" },
    @{ File = "scripts\diagnose_run.py";    Marker = "show_deployment" }
)

foreach ($check in $checks) {
    if (-not (Test-Path $check.File)) {
        Write-Host ("MISSING  {0}" -f $check.File) -ForegroundColor Red
        continue
    }
    $found = Select-String -Path $check.File -Pattern $check.Marker -Quiet
    if ($found) {
        Write-Host ("CURRENT  {0,-28} ({1})" -f $check.File, $check.Marker) -ForegroundColor Green
    } else {
        Write-Host ("STALE    {0,-28} (missing: {1})" -f $check.File, $check.Marker) -ForegroundColor Red
    }
}

Write-Host "`nAlso check ModelProvider is not constrained to a fixed list:"
if (Select-String -Path infra\template.yaml -Pattern "AllowedValues:\s*\[bedrock" -Quiet) {
    Write-Host "STALE    infra\template.yaml still has AllowedValues -- this is what failed the deploy" -ForegroundColor Red
} else {
    Write-Host "CURRENT  infra\template.yaml uses AllowedPattern" -ForegroundColor Green
}