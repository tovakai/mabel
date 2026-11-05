#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Run a reranker for Mabel on the Dottie PC: bge-reranker-v2-m3 on llama-server, port 8083.

.DESCRIPTION
  Downloads bge-reranker-v2-m3 (Q8_0 GGUF, ~600 MB) once, starts llama-server in reranking mode on
  0.0.0.0:8083 (the "Mabel services (tailnet only)" firewall rule limits it to the tailnet), registers
  a logon task "Mabel reranker" so it survives reboots, and checks the /v1/rerank endpoint.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\dottie_reranker.ps1
  powershell -ExecutionPolicy Bypass -File .\dottie_reranker.ps1 -LlamaServer "C:\path\to\llama-server.exe"
#>
param(
    [string]$LlamaServer = "",
    [int]$Port = 8083
)
$ErrorActionPreference = "Stop"
$ModelUrl = "https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q8_0.gguf"
$Dir = Join-Path $env:USERPROFILE "Mabel\models"
$Model = Join-Path $Dir "bge-reranker-v2-m3-Q8_0.gguf"

if (-not $LlamaServer) {
    $LlamaServer = (Get-ChildItem $env:USERPROFILE -Recurse -Filter llama-server.exe -ErrorAction SilentlyContinue |
                    Select-Object -First 1).FullName
}
if (-not $LlamaServer -or -not (Test-Path $LlamaServer)) { throw "llama-server.exe not found; pass -LlamaServer <path>" }
Write-Host "llama-server: $LlamaServer"

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
if (-not (Test-Path $Model) -or (Get-Item $Model).Length -lt 100MB) {
    Write-Host "Downloading bge-reranker-v2-m3 (~600 MB)..."
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest $ModelUrl -OutFile $Model
}
Write-Host ("model: {0} ({1:N0} MB)" -f $Model, ((Get-Item $Model).Length / 1MB))

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
    Write-Warning "Something already listens on $Port (pid $($existing.OwningProcess)); stop it first."
    exit 1
}

$serverArgs = "-m `"$Model`" --reranking --host 0.0.0.0 --port $Port --ctx-size 8192 --alias bge-reranker-v2-m3"
$action = New-ScheduledTaskAction -Execute $LlamaServer -Argument $serverArgs -WorkingDirectory (Split-Path $LlamaServer)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "Mabel reranker" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
# Start it through Task Scheduler, not as a child of this shell: processes started inside an SSH
# session are killed when the session closes.
Start-ScheduledTask -TaskName "Mabel reranker"
Write-Host "Registered and started logon task 'Mabel reranker'."

$body = @{ model = "bge-reranker-v2-m3"; query = "anime studio"; documents = @("Toei Animation makes anime", "a recipe for soup") } | ConvertTo-Json
foreach ($try in 1..20) {
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$Port/v1/rerank" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10
        Write-Host "rerank OK:" ($r.results | ForEach-Object { "doc $($_.index) = $([math]::Round($_.relevance_score, 3))" }) -ForegroundColor Green
        exit 0
    } catch { Start-Sleep -Seconds 3 }
}
Write-Warning "llama-server didn't answer on $Port yet; check it with: Get-NetTCPConnection -LocalPort $Port"
