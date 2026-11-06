# One-shot setup for the demo machine. Safe to re-run.
#
#   pwsh -NoProfile -File deploy\bootstrap.ps1
#
# Installs dependencies, asks for the three Meta values once, writes them to
# .env (gitignored, never committed), then starts wacal on port 8090.
#
# Run deploy\tunnel.ps1 in a SECOND window afterwards; wacal has to be
# listening before Meta's verification GET arrives.
param(
    [int]$Port = 8090,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repo '.env'

Write-Host "wacal demo bootstrap" -ForegroundColor Cyan
Write-Host "repo: $repo"

# --- python ---------------------------------------------------------------
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "python is not on PATH. Install Python 3.11+ from python.org, ticking 'Add python.exe to PATH'." }
$version = & $python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
Write-Host "python $version at $python"
if ([version]$version -lt [version]'3.10') { throw "wacal needs Python 3.10 or newer; found $version" }

& $python -m pip install --quiet --disable-pip-version-check -r (Join-Path $repo 'requirements.txt')
Write-Host "dependencies installed"

# --- .env -----------------------------------------------------------------
# These four ids are not secret and are already verified against the live
# account, so they are defaults rather than questions.
$known = [ordered]@{
    WA_TRANSPORT         = 'cloud'
    WA_APP_ID            = '1477783504400676'
    WA_WABA_ID           = '2232461004084313'
    WA_PHONE_NUMBER_ID   = '1387507791118752'
    WA_GRAPH_VERSION     = 'v21.0'
    WA_VERIFY_TOKEN      = 'wacal-verify-2026'
    WA_TEMPLATE_LANGUAGE = 'en_US'
    ADMIN_KEY            = 'local-admin-key-2026'
}

if (Test-Path $envFile) {
    Write-Host ".env exists; leaving it alone" -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "Two secrets are needed. Both are typed hidden and go only into .env:" -ForegroundColor Cyan
    Write-Host "  1. System-user access token  (Business settings > Users > System users > burhan)"
    Write-Host "     A token is shown ONCE, so if you do not have it, generate a new one:"
    Write-Host "     scopes whatsapp_business_messaging + whatsapp_business_management, expiry Never."
    Write-Host "  2. App secret                (Meta app > Settings > Basic > App secret > Show)"
    Write-Host ""
    $token = Read-Host -AsSecureString "WA_TOKEN"
    $secret = Read-Host -AsSecureString "WA_APP_SECRET"
    $tokenPlain = [System.Net.NetworkCredential]::new('', $token).Password
    $secretPlain = [System.Net.NetworkCredential]::new('', $secret).Password
    if (-not $tokenPlain -or -not $secretPlain) { throw "both values are required" }

    $lines = @(
        '# Written by deploy/bootstrap.ps1. Gitignored -- never commit this.',
        '# Ids verified live against the Meta test number +1 555-197-7042.'
    )
    foreach ($k in $known.Keys) { $lines += "$k=$($known[$k])" }
    $lines += "WA_TOKEN=$tokenPlain"
    $lines += "WA_APP_SECRET=$secretPlain"
    $lines += ''
    $lines += '# No wacal template is approved yet, so a notification to anyone outside'
    $lines += '# their 24-hour window is held in the outbox instead of silently dropped.'
    $lines += '# Fill these in once the three templates are approved (see deploy/DEMO.md).'
    $lines += 'WA_TEMPLATE_BOOKING_CONFIRMED='
    $lines += 'WA_TEMPLATE_BOOKING_CANCELLED='
    $lines += 'WA_TEMPLATE_OTP='
    Set-Content -Path $envFile -Value $lines -Encoding utf8
    Remove-Variable tokenPlain, secretPlain
    Write-Host "wrote $envFile" -ForegroundColor Green
}

# --- sanity check ---------------------------------------------------------
# Proves the token works before anything depends on it. Read-only.
Write-Host ""
Write-Host "checking the token against the live account..." -ForegroundColor Cyan
& $python (Join-Path $repo 'deploy\run.py') --check
if ($LASTEXITCODE -ne 0) { throw "the token did not verify; fix .env before continuing" }

if ($NoStart) { Write-Host "`n-NoStart given; not starting the server."; exit 0 }

Write-Host ""
Write-Host "starting wacal on port $Port -- leave this window open" -ForegroundColor Green
Write-Host "next: open a SECOND window and run  pwsh -NoProfile -File deploy\tunnel.ps1"
Write-Host ""
& $python (Join-Path $repo 'deploy\run.py') --serve --port $Port
