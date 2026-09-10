# Start Alfred.
#
# Prints the pairing token so a new device can be paired without digging
# through the data directory.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "No virtualenv found. Run:  py -3.13 -m venv .venv"
}

$tokenFile = Join-Path $env:LOCALAPPDATA "Alfred\secrets\auth_token"
if (Test-Path $tokenFile) {
    Write-Host ""
    Write-Host "  Pairing token: " -NoNewline
    Write-Host (Get-Content $tokenFile -Raw).Trim() -ForegroundColor Yellow
    Write-Host ""
}

& $python -m app.main
