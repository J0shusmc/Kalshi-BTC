# Creates a local Python environment and a paper-mode configuration on Windows.
# Run from Explorer or PowerShell: .\windows\setup-paper.ps1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot 'venv\Scripts\python.exe'

Set-Location $ProjectRoot

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python Launcher (py.exe) was not found. Install Python 3.11 or newer from python.org, select "Add python.exe to PATH", then run this again.'
}

& py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw 'Python 3.11 or newer is required. Install it, then rerun this script.'
}

if (-not (Test-Path $VenvPython)) {
    & py -3 -m venv venv
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env. Add your Kalshi API Key ID and private-key path before starting the bot.' -ForegroundColor Yellow
} else {
    Write-Host 'Existing .env preserved.' -ForegroundColor Green
}

New-Item -ItemType Directory -Force -Path 'local' | Out-Null
Write-Host ''
Write-Host 'Setup complete. Put your private key in this folder, update .env, then double-click windows\start-paper.cmd.' -ForegroundColor Green
