# Run on Windows after setup to create a portable monitor executable.
# The executable is deliberately built from the current source on the target OS.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot 'venv\Scripts\python.exe'

if (-not (Test-Path $Python)) {
    throw 'The Python environment is missing. Run windows\setup-paper.ps1 first.'
}

Set-Location $ProjectRoot
& $Python -m pip install pyinstaller
& $Python -m PyInstaller --noconfirm --clean --onefile --name KalshiBTCMonitor --distpath 'dist\windows' --workpath 'build\windows' --specpath 'build\windows' 'scripts\btc15_live_monitor.py'
Write-Host 'Built dist\windows\KalshiBTCMonitor.exe. Copy .env and kalshi_private.key beside the .exe, then run it with --paper.' -ForegroundColor Green
