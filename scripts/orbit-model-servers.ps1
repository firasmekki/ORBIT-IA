# Native model servers (llama-server.exe) of ORBIT AI: one definition of how
# each is launched, shared by start-orbit-ai.ps1 and orbit-models.ps1.
#
#   llm     Qwen3-30B-A3B (port 8096) - chat and reports
#   vision  Qwen3-VL-8B   (port 8097) - image analysis
#
# Lifecycle: STOPPED -> STARTING -> READY (loaded) <-> SLEEPING (unloaded by
# llama-server itself after *_SLEEP_IDLE_SECONDS without a request; the next
# request reloads it). A server is only ever stopped by its PID, after
# checking that it is the expected llama-server and that it is not
# answering a request (unless -Force) - never "all llama-server processes".
#
# Dot-source it: . "$PSScriptRoot\scripts\orbit-model-servers.ps1"

$OrbitRoot = Split-Path -Parent $PSScriptRoot
$OrbitServerExe = Join-Path $OrbitRoot "local-models\bin\llama-b11474-cuda12.4\llama-server.exe"
$OrbitLogDir = Join-Path $OrbitRoot "local-models\logs"
$OrbitEnvPath = Join-Path $OrbitRoot ".env"

function Get-OrbitEnvValue([string]$name, [string]$fallback) {
    $envText = if (Test-Path -LiteralPath $OrbitEnvPath) { Get-Content -Raw -LiteralPath $OrbitEnvPath } else { "" }
    $match = [regex]::Match($envText, "(?m)^$name=(.*)$")
    if (-not $match.Success -or [string]::IsNullOrWhiteSpace($match.Groups[1].Value)) { return $fallback }
    return $match.Groups[1].Value.Trim().Trim('"').Trim("'")
}

function Get-OrbitModelConfig([ValidateSet('llm', 'vision')][string]$Model) {
    $apiKey = Get-OrbitEnvValue "LLM_API_KEY" ""
    if ($Model -eq 'llm') {
        $modelSetting = Get-OrbitEnvValue "MODEL_PATH" "local-models\gguf\Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf"
        $modelPath = if ([System.IO.Path]::IsPathRooted($modelSetting)) { $modelSetting } else { Join-Path $OrbitRoot ($modelSetting.Replace('/', '\')) }
        $modelPath = [System.IO.Path]::GetFullPath($modelPath)
        $alias = "Qwen3-30B-A3B-Instruct-2507"
        # Every layer on the GPU (attention, KV cache), MoE experts of the
        # first LLM_CPU_MOE layers in system RAM: far faster than a partial
        # offload on a 12 GB RTX 3060.
        $ctx = Get-OrbitEnvValue "LLM_CONTEXT_SIZE" "16384"
        $gpuLayers = Get-OrbitEnvValue "LLM_GPU_LAYERS" "99"
        $cpuMoe = Get-OrbitEnvValue "LLM_CPU_MOE" "48"
        $threads = Get-OrbitEnvValue "LLM_THREADS" "8"
        # Prompt cache kept in RAM between requests (llama-server default:
        # 8192 MiB). Useful when a conversation comes back; bounded.
        $cacheRam = Get-OrbitEnvValue "LLM_CACHE_RAM_MB" "2048"
        # -1 = always loaded: a chat answer never waits for a ~40 s reload.
        $sleepIdle = Get-OrbitEnvValue "LLM_SLEEP_IDLE_SECONDS" "-1"
        $arguments = "--model `"$modelPath`" --alias $alias --host 0.0.0.0 --port 8096 --api-key $apiKey --ctx-size $ctx --n-gpu-layers $gpuLayers --n-cpu-moe $cpuMoe --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --threads $threads --parallel 1 --batch-size 2048 --ubatch-size 512 --cache-ram $cacheRam --sleep-idle-seconds $sleepIdle"
        return [pscustomobject]@{
            Name = 'llm'; Port = 8096; Alias = $alias; Files = @($modelPath); Arguments = $arguments
            StdOut = Join-Path $OrbitLogDir "qwen3-server.stdout.log"; StdErr = Join-Path $OrbitLogDir "qwen3-server.stderr.log"
            ReadyMinutes = 30; CacheRamMb = $cacheRam; SleepIdleSeconds = $sleepIdle; ApiKey = $apiKey
        }
    }
    $dir = Join-Path $OrbitRoot "local-models\models\Qwen3-VL-8B-Instruct-GGUF"
    $modelPath = Join-Path $dir "Qwen3VL-8B-Instruct-Q4_K_M.gguf"
    $projector = Join-Path $dir "mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"
    $alias = "qwen3-vl-8b"
    # Every image is a different prompt: a RAM prompt cache only holds memory
    # (measured: 8 GB of private memory after 390 images with the default).
    $cacheRam = Get-OrbitEnvValue "VISION_CACHE_RAM_MB" "0"
    # Unloaded after this idle period: gives back ~6 GB of VRAM and ~7 GB of
    # RAM; the next image reloads it (2-20 s).
    $sleepIdle = Get-OrbitEnvValue "VISION_SLEEP_IDLE_SECONDS" "300"
    $arguments = "--model `"$modelPath`" --mmproj `"$projector`" --alias $alias --host 0.0.0.0 --port 8097 --api-key $apiKey --ctx-size 4096 --n-gpu-layers 99 --threads 6 --parallel 1 --batch-size 128 --ubatch-size 64 --image-min-tokens 1024 --cache-ram $cacheRam --sleep-idle-seconds $sleepIdle"
    return [pscustomobject]@{
        Name = 'vision'; Port = 8097; Alias = $alias; Files = @($modelPath, $projector); Arguments = $arguments
        StdOut = Join-Path $OrbitLogDir "qwen3-vl.stdout.log"; StdErr = Join-Path $OrbitLogDir "qwen3-vl.stderr.log"
        ReadyMinutes = 15; CacheRamMb = $cacheRam; SleepIdleSeconds = $sleepIdle; ApiKey = $apiKey
    }
}

function Invoke-OrbitModelApi($config, [string]$path) {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$($config.Port)$path" -Headers @{ Authorization = "Bearer $($config.ApiKey)" } -TimeoutSec 5
    } catch {
        return $null
    }
}

function Get-OrbitModelProcesses($config) {
    # The llama-server(s) launched for this port, listening or still loading.
    @(Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "--port $($config.Port)(\s|$)" })
}

function Get-OrbitModelState($config) {
    $processes = Get-OrbitModelProcesses $config
    $listener = Get-NetTCPConnection -LocalPort $config.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    $state = 'STOPPED'
    if ($listener) {
        $health = Invoke-OrbitModelApi $config "/health"
        if (-not $health) { $state = 'STARTING' }
        else {
            $models = Invoke-OrbitModelApi $config "/v1/models"
            if (@($models.data | ForEach-Object { $_.id }) -notcontains $config.Alias) { $state = 'FOREIGN' }
            else {
                # /props does not wake a sleeping model (/slots would).
                $props = Invoke-OrbitModelApi $config "/props"
                $state = if ($props -and $props.is_sleeping) { 'SLEEPING' } else { 'READY' }
            }
        }
    } elseif ($processes.Count -gt 0) {
        $state = 'STARTING'
    }
    $proc = if ($listener) { Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue } elseif ($processes.Count -gt 0) { Get-Process -Id $processes[0].ProcessId -ErrorAction SilentlyContinue } else { $null }
    [pscustomobject]@{
        Model = $config.Name; Port = $config.Port; State = $state
        Pid = if ($proc) { $proc.Id } else { $null }
        Processes = $processes.Count
        WorkingSetGB = if ($proc) { [math]::Round($proc.WorkingSet64 / 1GB, 2) } else { $null }
        PrivateGB = if ($proc) { [math]::Round($proc.PrivateMemorySize64 / 1GB, 2) } else { $null }
        CacheRamMb = $config.CacheRamMb; SleepIdleSeconds = $config.SleepIdleSeconds
    }
}

function Start-OrbitModel([ValidateSet('llm', 'vision')][string]$Model) {
    $config = Get-OrbitModelConfig $Model
    foreach ($file in $config.Files) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Fichier de modèle absent : $file" }
    }
    if (-not (Test-Path -LiteralPath $OrbitServerExe -PathType Leaf)) { throw "llama-server.exe absent : $OrbitServerExe" }
    $current = Get-OrbitModelState $config
    if ($current.State -eq 'FOREIGN') { throw "Le port $($config.Port) est occupé par un autre service. Arrêtez-le d'abord." }
    # Never a second instance: already running (or already loading) = nothing to do.
    if ($current.State -ne 'STOPPED') {
        Write-Host "$Model : déjà lancé (PID $($current.Pid), état $($current.State)) - aucune nouvelle instance."
        return $current
    }
    New-Item -ItemType Directory -Force -Path $OrbitLogDir | Out-Null
    $started = Get-Date
    $process = Start-Process -FilePath $OrbitServerExe -ArgumentList $config.Arguments -RedirectStandardOutput $config.StdOut -RedirectStandardError $config.StdErr -PassThru -WindowStyle Hidden
    $deadline = $started.AddMinutes($config.ReadyMinutes)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
            Get-Content $config.StdErr -Tail 40 -ErrorAction SilentlyContinue
            throw "llama-server ($Model) s'est arrêté pendant le chargement. Voir $($config.StdErr)"
        }
        $state = Get-OrbitModelState $config
        if ($state.State -eq 'READY' -or $state.State -eq 'SLEEPING') {
            Write-Host ("{0} : prêt en {1:N0} s (PID {2}, RAM privée {3} Go)" -f $Model, ((Get-Date) - $started).TotalSeconds, $process.Id, $state.PrivateGB)
            return $state
        }
        Start-Sleep -Seconds 2
    }
    throw "$Model n'est pas prêt après $($config.ReadyMinutes) minutes. Voir $($config.StdErr)"
}

function Stop-OrbitModel([ValidateSet('llm', 'vision')][string]$Model, [switch]$Force) {
    $config = Get-OrbitModelConfig $Model
    $state = Get-OrbitModelState $config
    if ($state.State -eq 'STOPPED') { Write-Host "$Model : déjà arrêté."; return }
    if ($state.State -eq 'FOREIGN') { throw "Le port $($config.Port) n'est pas servi par $($config.Alias) : rien n'est arrêté." }
    if ($state.State -eq 'READY' -and -not $Force) {
        # Awake: is it answering a request right now? (asleep = certainly not)
        $slots = Invoke-OrbitModelApi $config "/slots"
        if (@($slots | Where-Object { $_.is_processing }).Count -gt 0) {
            throw "$Model traite une requête en ce moment : arrêt refusé. Réessayez à la fin, ou -Force (la tâche en cours sera remise en file par le backend)."
        }
    }
    foreach ($p in Get-OrbitModelProcesses $config) {
        Stop-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline -and (Get-NetTCPConnection -LocalPort $config.Port -State Listen -ErrorAction SilentlyContinue)) { Start-Sleep -Milliseconds 500 }
    Write-Host "$Model : arrêté (PID $($state.Pid))."
}
