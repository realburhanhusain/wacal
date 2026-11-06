# Give the local wacal a public HTTPS URL that Meta can POST to.
#
#   pwsh -NoProfile -File deploy\tunnel.ps1
#
# Downloads cloudflared if needed (single portable exe, no install, no
# account) and opens a quick tunnel to port 8090. Prints the public URL.
#
# Run this AFTER bootstrap.ps1 is serving, in a second window, and leave it
# open: closing it destroys the URL.
#
# This fails on a machine running the Netskope client -- it closes outbound
# 7844, which is the only port a quick tunnel can use, and it also kills
# ngrok's session after the TLS handshake. That is why the demo runs here and
# not on the work laptop.
param(
    [int]$Port = 8090,
    [string]$ToolDir = "$env:LOCALAPPDATA\wacal-tools"
)

$ErrorActionPreference = 'Stop'
$exe = Join-Path $ToolDir 'cloudflared.exe'

if (-not (Test-Path $exe)) {
    New-Item -ItemType Directory -Force $ToolDir | Out-Null
    $url = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
    Write-Host "downloading cloudflared to $exe"
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
}
Write-Host (& $exe --version)

# Check the local app is actually up first: a tunnel to nothing returns 502
# and looks like a tunnel fault.
try {
    $health = (Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 10 -UseBasicParsing).Content
    Write-Host "local wacal: $health" -ForegroundColor Green
} catch {
    throw "nothing is answering on http://127.0.0.1:$Port/health -- start deploy\bootstrap.ps1 first"
}

$log = Join-Path $ToolDir 'cloudflared.log'
Write-Host "starting tunnel; log at $log" -ForegroundColor Cyan

# --logfile, not --log: the quick-tunnel banner goes to stdout either way,
# and a separate file is what makes the URL greppable if the banner scrolls.
& $exe tunnel --url "http://localhost:$Port" --no-autoupdate --logfile $log
