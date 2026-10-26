<#
.SYNOPSIS
  Contrôle du moteur IA d'ORBIT AI (serveurs de modèles et file d'analyse visuelle).

.EXAMPLE
  .\orbit-models.ps1 status                 # état des modèles, GPU, RAM, files
  .\orbit-models.ps1 start vision           # démarre le serveur vision (jamais en double)
  .\orbit-models.ps1 stop vision            # arrêt propre (refusé s'il répond à une requête)
  .\orbit-models.ps1 stop vision -Force     # arrêt même en cours de tâche (l'image est remise en file)
  .\orbit-models.ps1 restart llm            # relance avec les réglages actuels du .env
  .\orbit-models.ps1 pause-vision           # la file d'analyse visuelle ne prend plus d'image
  .\orbit-models.ps1 resume-vision          # elle reprend où elle en était

  Fermer le navigateur n'arrête rien : les serveurs se mettent en veille seuls
  après *_SLEEP_IDLE_SECONDS (voir .env.example) et se rechargent à la demande.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][ValidateSet('status', 'start', 'stop', 'restart', 'pause-vision', 'resume-vision')][string]$Action = 'status',
    [Parameter(Position = 1)][ValidateSet('llm', 'vision', 'all')][string]$Model = 'all',
    [switch]$Force
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scripts\orbit-model-servers.ps1")

$models = if ($Model -eq 'all') { @('llm', 'vision') } else { @($Model) }

function Invoke-OrbitSql([string]$sql) {
    Push-Location $OrbitRoot
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"  # a psql error must not abort the status
    try {
        $out = & docker compose exec -T postgres psql -U orbitia -d orbitia -At -F "|" -c $sql 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return $out
    } finally { $ErrorActionPreference = $previous; Pop-Location }
}

function Set-VisionPaused([bool]$paused) {
    # Same switch as the administration page (runtime_settings): survives
    # a backend restart; the image in progress, if any, finishes.
    $value = if ($paused) { 'true' } else { 'false' }
    $result = Invoke-OrbitSql "INSERT INTO runtime_settings (key, value, updated_by, updated_at) VALUES ('vision_queue', jsonb_build_object('paused', $value), 'orbit-models.ps1', now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now() RETURNING value"
    if ($null -eq $result) { throw "Base de données injoignable (Docker démarré ?)." }
    Write-Host ("File d'analyse visuelle : {0}." -f $(if ($paused) { 'en pause (aucune nouvelle image ; la file est conservée)' } else { 'reprise' }))
}

switch ($Action) {
    'start' { foreach ($m in $models) { Start-OrbitModel $m | Out-Null } }
    'stop' { foreach ($m in $models) { Stop-OrbitModel $m -Force:$Force } }
    'restart' { foreach ($m in $models) { Stop-OrbitModel $m -Force:$Force; Start-OrbitModel $m | Out-Null } }
    'pause-vision' { Set-VisionPaused $true }
    'resume-vision' { Set-VisionPaused $false }
    'status' {
        Write-Host "== Serveurs de modèles"
        $models | ForEach-Object { Get-OrbitModelState (Get-OrbitModelConfig $_) } |
            Format-Table Model, Port, State, Pid, Processes, @{n = 'RAM privée (Go)'; e = { $_.PrivateGB } }, @{n = 'Working set (Go)'; e = { $_.WorkingSetGB } }, @{n = 'Cache RAM (Mo)'; e = { $_.CacheRamMb } }, @{n = 'Veille après (s)'; e = { $_.SleepIdleSeconds } } -AutoSize | Out-String | Write-Host
        Write-Host "== GPU"
        $gpu = & nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu,pstate --format=csv,noheader 2>$null
        Write-Host $(if ($gpu) { "utilisation, VRAM utilisée, VRAM totale, puissance, température, état : $gpu" } else { "nvidia-smi indisponible" })
        Write-Host "== RAM Windows"
        $os = Get-CimInstance Win32_OperatingSystem
        Write-Host ("libre {0:N1} Go sur {1:N1} Go" -f ($os.FreePhysicalMemory / 1MB), ($os.TotalVisibleMemorySize / 1MB))
        $vm = Get-Process vmmemWSL -ErrorAction SilentlyContinue
        if ($vm) { Write-Host ("VM Docker/WSL2 (vmmemWSL) : {0:N1} Go - inclut le cache de fichiers Linux" -f ($vm.PrivateMemorySize64 / 1GB)) }
        Write-Host "== Files de traitement"
        $rows = Invoke-OrbitSql "SELECT status, count(*) FROM documents WHERE is_active AND status IN ('queued','extracting','indexing','visual_queued','visual_running','failed') GROUP BY status ORDER BY status"
        if ($null -eq $rows) { Write-Host "base de données injoignable (Docker démarré ?)" }
        elseif (-not $rows) { Write-Host "aucune tâche en attente" }
        else { $rows | ForEach-Object { $s = $_ -split '\|'; Write-Host ("{0,-16} {1}" -f $s[0], $s[1]) } }
        $paused = Invoke-OrbitSql "SELECT coalesce((value->>'paused')::boolean, false) || ' (' || coalesce(updated_by, '?') || ')' FROM runtime_settings WHERE key = 'vision_queue'"
        Write-Host ("Analyse visuelle en pause : {0}" -f $(if ($paused) { $paused } else { 'false' }))
    }
}
