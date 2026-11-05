#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Make Mabel's helper services on the Dottie PC reachable over Tailscale, and add Kokoro (voice).

.DESCRIPTION
  1. Shows what listens on the embedder (8082) and reranker (8083) ports and how it was started.
  2. Offers to relaunch them listening on all interfaces (same program and arguments; only the
     host changes) and to register logon tasks so that survives reboots.
  3. Adds ONE firewall rule allowing 8082, 8083 and 8880 ONLY from the tailnet (100.64.0.0/10).
     Nothing on the LAN or the internet can reach them.
  4. Installs Kokoro-FastAPI v0.9.2 (GPU) on port 8880: Docker if Docker Desktop works,
     otherwise natively (uv + eSpeak NG + the project's own start-gpu.ps1), as a logon task.
  5. Checks every service locally and prints a summary.

  Safe to re-run: each step checks what's already there.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\dottie_services.ps1
  powershell -ExecutionPolicy Bypass -File .\dottie_services.ps1 -SkipKokoro
#>
param(
    [switch]$SkipKokoro,
    [ValidateSet("auto", "docker", "native")][string]$KokoroMode = "auto"
)

$ErrorActionPreference = "Stop"
$Tailnet = "100.64.0.0/10"
$Ports = @{ 8082 = "embedder"; 8083 = "reranker" }
$KokoroVersion = "v0.9.2"
$KokoroImage = "ghcr.io/remsky/kokoro-fastapi-gpu:$KokoroVersion"
$Home2 = Join-Path $env:USERPROFILE "Mabel"
New-Item -ItemType Directory -Force -Path $Home2 | Out-Null

function Section($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ask($question) { (Read-Host "$question [y/N]") -match '^(y|yes|j|ja)$' }

function Split-CommandLine([string]$line) {
    if ($line -match '^\s*"([^"]+)"\s*(.*)$') { return @($Matches[1], $Matches[2]) }
    if ($line -match '^\s*(\S+)\s*(.*)$') { return @($Matches[1], $Matches[2]) }
    return @($line, "")
}

# --- 1. What's listening ------------------------------------------------------------------
Section "1. Embedder and reranker"
$found = @{}
foreach ($port in $Ports.Keys | Sort-Object) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) {
        Write-Warning "Nothing listens on $port ($($Ports[$port])). Start it the way you normally do, then re-run this script."
        continue
    }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)"
    Write-Host ("{0} ({1}): listening on {2}" -f $port, $Ports[$port], $conn.LocalAddress)
    Write-Host ("   started as: {0}" -f $proc.CommandLine)
    $found[$port] = @{ Conn = $conn; Proc = $proc }
}

# --- 2. Relaunch on all interfaces ----------------------------------------------------------
Section "2. Listen on all interfaces (the firewall limits it to the tailnet)"
foreach ($port in $found.Keys | Sort-Object) {
    $item = $found[$port]
    $address = $item.Conn.LocalAddress
    if ($address -in @("0.0.0.0", "::")) {
        Write-Host "$port already listens on all interfaces."
        continue
    }
    $exe, $argline = Split-CommandLine $item.Proc.CommandLine
    if ((Split-Path $exe -Leaf) -notmatch '^llama-server(\.exe)?$') {
        Write-Warning "$port is served by $(Split-Path $exe -Leaf), not llama-server; change its host/bind setting to 0.0.0.0 yourself."
        continue
    }
    if ($argline -match '(--host|-host)\s+\S+') { $newArgs = $argline -replace '(--host|-host)\s+\S+', '--host 0.0.0.0' }
    else { $newArgs = "$argline --host 0.0.0.0".Trim() }
    Write-Host "$port will be restarted as:"
    Write-Host "   `"$exe`" $newArgs"
    if (-not (Ask "Restart the $($Ports[$port]) like that now?")) { continue }
    Stop-Process -Id $item.Proc.ProcessId -Force
    Start-Sleep -Seconds 2
    Start-Process -FilePath $exe -ArgumentList $newArgs -WorkingDirectory (Split-Path $exe) -WindowStyle Hidden
    Write-Host "   restarted."
    if (Ask "Also start it like that automatically at logon (scheduled task 'Mabel $($Ports[$port])')?") {
        $action = New-ScheduledTaskAction -Execute $exe -Argument $newArgs -WorkingDirectory (Split-Path $exe)
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName "Mabel $($Ports[$port])" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Write-Host "   logon task registered. If you also start it another way (a .bat, startup folder), remove that one."
    }
}

# --- 3. Firewall: tailnet only -------------------------------------------------------------
Section "3. Firewall (tailnet only)"
$ruleName = "Mabel services (tailnet only)"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort 8082, 8083, 8880 `
    -RemoteAddress $Tailnet -Action Allow -Profile Any | Out-Null
Write-Host "Allowed TCP 8082, 8083, 8880 from $Tailnet only."

# --- 4. Kokoro -----------------------------------------------------------------------------
if (-not $SkipKokoro) {
    Section "4. Kokoro voice ($KokoroVersion, port 8880)"
    $dockerOk = $false
    if ($KokoroMode -ne "native" -and (Get-Command docker -ErrorAction SilentlyContinue)) {
        docker info *> $null
        $dockerOk = ($LASTEXITCODE -eq 0)
    }
    if ($KokoroMode -eq "docker" -and -not $dockerOk) { throw "Docker isn't available; use -KokoroMode native" }
    if ($dockerOk) {
        docker rm -f mabel-kokoro *> $null
        docker run -d --name mabel-kokoro --gpus all --restart unless-stopped -p 8880:8880 $KokoroImage
        if ($LASTEXITCODE -ne 0) { throw "docker run failed" }
        Write-Host "Kokoro container started (restarts with Docker)."
    }
    else {
        Write-Host "Docker not available: installing natively."
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        }
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        }
        if (-not (Test-Path "C:\Program Files\eSpeak NG\libespeak-ng.dll")) {
            $msi = Join-Path $env:TEMP "espeak-ng.msi"
            Invoke-WebRequest "https://github.com/espeak-ng/espeak-ng/releases/download/1.52.0/espeak-ng.msi" -OutFile $msi
            Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn" -Wait
        }
        $dir = Join-Path $Home2 "Kokoro-FastAPI"
        if (-not (Test-Path $dir)) { git clone --depth 1 --branch $KokoroVersion https://github.com/remsky/Kokoro-FastAPI.git $dir }
        # start-gpu.ps1 runs "uv pip install", which needs a virtual environment (Python from .python-version).
        if (-not (Test-Path (Join-Path $dir ".venv"))) { Push-Location $dir; uv venv; Pop-Location }
        $action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$dir\start-gpu.ps1`"" -WorkingDirectory $dir
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName "Mabel Kokoro" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName "Mabel Kokoro"
        Write-Host "Kokoro installed as logon task 'Mabel Kokoro'. The first start installs packages and downloads the model; give it a few minutes."
    }
}

# --- 5. Check --------------------------------------------------------------------------------
Section "5. Check"
$checks = @(@{ Port = 8082; Name = "embedder"; Path = "/v1/models" }, @{ Port = 8083; Name = "reranker"; Path = "/v1/models" })
if (-not $SkipKokoro) { $checks += @{ Port = 8880; Name = "kokoro"; Path = "/v1/models" } }
foreach ($c in $checks) {
    $ok = $false
    foreach ($try in 1..12) {
        try { Invoke-RestMethod "http://127.0.0.1:$($c.Port)$($c.Path)" -TimeoutSec 5 | Out-Null; $ok = $true; break }
        catch { if ($c.Name -ne "kokoro") { break }; Start-Sleep -Seconds 10 }
    }
    $listen = (Get-NetTCPConnection -LocalPort $c.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).LocalAddress
    "{0,-9} port {1}: {2} (listening on {3})" -f $c.Name, $c.Port, $(if ($ok) { "OK" } else { "not answering yet" }), $(if ($listen) { $listen } else { "-" })
}
$tsip = (& tailscale ip -4 2>$null | Select-Object -First 1)
Write-Host ""
Write-Host "Done. Tell Mabel's operator (or Claude) this PC's tailnet address: $tsip" -ForegroundColor Green
