[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$backendHealthUrl = "http://127.0.0.1:8095/health"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI was not found. Start or install Docker Desktop first."
}

# The API key protecting both model servers: created once, kept in .env.
$envPath = Join-Path $projectRoot ".env"
$envText = if (Test-Path -LiteralPath $envPath) { Get-Content -Raw -LiteralPath $envPath } else { "" }
$keyMatch = [regex]::Match($envText, '(?m)^LLM_API_KEY=(.+)$')
if (-not $keyMatch.Success -or [string]::IsNullOrWhiteSpace($keyMatch.Groups[1].Value)) {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $apiKey = [BitConverter]::ToString($bytes).Replace("-", "").ToLowerInvariant()
    [System.IO.File]::AppendAllText($envPath, "`nLLM_API_KEY=$apiKey`n", [System.Text.UTF8Encoding]::new($false))
}

# How each llama-server is launched (context, GPU layers, RAM prompt cache,
# idle unload...) lives in scripts\orbit-model-servers.ps1, shared with
# orbit-models.ps1 (status / start / stop / pause of the engine). A server
# already running is reused, never started twice.
. (Join-Path $projectRoot "scripts\orbit-model-servers.ps1")
$llm = Start-OrbitModel llm

# Vision is an optional sidecar: the backend only enables image requests
# when it is verified ready. It unloads itself after VISION_SLEEP_IDLE_SECONDS.
$env:VISION_ENABLED = "false"
$visionConfig = Get-OrbitModelConfig vision
if (@($visionConfig.Files | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }).Count -eq 0) {
    try {
        $vision = Start-OrbitModel vision
        if ($vision.State -in @('READY', 'SLEEPING')) { $env:VISION_ENABLED = "true" }
    } catch {
        Write-Warning "Vision sidecar did not become ready. Chat will continue with text only; review $($visionConfig.StdErr). $_"
    }
} else {
    Write-Host "Vision model files are not present; image analysis remains disabled."
}

$backendListener = Get-NetTCPConnection -LocalPort 8095 -State Listen -ErrorAction SilentlyContinue
if ($backendListener) {
    try {
        $backendHealth = Invoke-RestMethod -Uri $backendHealthUrl -TimeoutSec 3
        $backendOpenApi = Invoke-RestMethod -Uri "http://127.0.0.1:8095/openapi.json" -TimeoutSec 3
        if ($backendHealth.status -ne "ok" -or $backendOpenApi.info.title -ne "Orbitia - Private Enterprise AI Agent") {
            throw "Unexpected service on port 8095."
        }
    } catch {
        throw "Port 8095 is occupied by another service. Stop it before starting ORBIT AI."
    }
}

Push-Location $projectRoot
try {
    & docker compose up -d --build --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        & docker compose logs --tail 80 backend
        throw "docker compose up failed with exit code $LASTEXITCODE."
    }

    $backendDeadline = (Get-Date).AddMinutes(5)
    $backendReady = $false
    while ((Get-Date) -lt $backendDeadline) {
        try {
            $health = Invoke-RestMethod -Uri $backendHealthUrl -TimeoutSec 3
            if ($health.status -eq "ok") {
                $backendReady = $true
                break
            }
        } catch { }
        Start-Sleep -Seconds 3
    }
    if (-not $backendReady) {
        & docker compose logs --tail 100 backend
        throw "Backend did not become ready on port 8095 within 5 minutes."
    }

    $llmConfig = Get-OrbitModelConfig llm
    Write-Host "ORBIT AI READY"
    Write-Host "Backend: http://127.0.0.1:8095"
    Write-Host "Health:  $backendHealthUrl"
    Write-Host "LLM:     $($llmConfig.Alias) (port 8096, RAM cache=$($llmConfig.CacheRamMb) MiB, idle unload=$($llmConfig.SleepIdleSeconds)s)"
    Write-Host "Vision:  $(if ($env:VISION_ENABLED -eq 'true') { "Qwen3-VL-8B at 8097 (idle unload=$($visionConfig.SleepIdleSeconds)s)" } else { 'disabled' })"
    Write-Host "UI:      http://127.0.0.1:8081"
    Write-Host "Engine:  .\orbit-models.ps1 status"
} finally {
    Pop-Location
}
