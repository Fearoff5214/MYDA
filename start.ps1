# Start Jarvis on this machine.
#
#   .\start.ps1            hub + the node agent for this PC
#   .\start.ps1 -Hub       hub only
#   .\start.ps1 -Node      node agent only (a machine that is not the hub)
#   .\start.ps1 -Listen    also start the microphone client
#   .\start.ps1 -Check     run doctor.py and exit
#
# Each part opens in its own window so you can read its log and close one
# without killing the others.

[CmdletBinding()]
param(
    [switch]$Hub,
    [switch]$Node,
    [switch]$Listen,
    [switch]$Check,
    [string]$HubUrl,
    [switch]$Wake
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host "No virtualenv at $py" -ForegroundColor Red
    Write-Host "Create one with:  py -3.13 -m venv .venv"
    Write-Host "                  .venv\Scripts\pip install -r requirements-hub.txt"
    exit 1
}

if ($Check) { & $py (Join-Path $root 'scripts\doctor.py') --hub; exit $LASTEXITCODE }

# Nothing named means hub + node, which is what you want on the hub machine.
if (-not ($Hub -or $Node -or $Listen)) { $Hub = $true; $Node = $true }

# Where the clients should connect. Read from the config the hub actually
# uses, so this cannot drift out of sync with it.
if (-not $HubUrl) {
    $HubUrl = & $py -c @"
import sys, pathlib
sys.path.insert(0, r'$root')
from hub.server import load_config
s = load_config()['server']
print(f""ws://{s['host']}:{s['port']}"")
"@
}
Write-Host "hub address: $HubUrl" -ForegroundColor Cyan

function Start-Part([string]$Title, [string[]]$Args) {
    Write-Host "  starting $Title"
    Start-Process -FilePath $py -ArgumentList $Args -WorkingDirectory $root
}

if ($Hub) {
    # Two hubs cannot bind the same port; clear any stale one first.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -EA SilentlyContinue |
        Where-Object { $_.CommandLine -like '*hub.server*' } |
        ForEach-Object {
            Write-Host "  stopping an already-running hub (pid $($_.ProcessId))"
            Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue
        }
    Start-Part 'hub' @('-m', 'hub.server')
    Write-Host "  (Whisper takes ~20s to load; wait for 'hub ready')" -ForegroundColor DarkGray
    Start-Sleep -Seconds 20
}

if ($Node) {
    if (-not (Test-Path (Join-Path $root 'node\allowlist.yaml'))) {
        Write-Host "  skipping node: no node\allowlist.yaml on this machine." -ForegroundColor Yellow
        Write-Host "  Copy node\allowlist.example.yaml and edit it first." -ForegroundColor Yellow
    } else {
        Start-Part 'node agent' @('-m', 'node.agent', '--hub', $HubUrl)
    }
}

if ($Listen) {
    $listenArgs = @('-m', 'client.listener', '--hub', $HubUrl)
    if ($Wake) { $listenArgs += '--wake' }
    Start-Part 'voice client' $listenArgs
    Write-Host "  hold ctrl+alt+space to talk" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Test it without a microphone:" -ForegroundColor Green
Write-Host "  .venv\Scripts\python.exe scripts\say.py --hub $HubUrl ""turn on the bedroom lights"""
