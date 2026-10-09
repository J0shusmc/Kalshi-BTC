# Starts the BTC monitor in paper mode only. Ctrl+C stops it safely.
[CmdletBinding()]
param(
    [ValidateRange(1, 100)]
    [double]$RiskPercent = 20,
    [ValidateRange(1, 999999999)]
    [int]$StartingBalanceCents = 11500
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot 'venv\Scripts\python.exe'

if (-not (Test-Path $Python)) {
    throw 'The Python environment is missing. Run windows\setup-paper.ps1 first.'
}
if (-not (Test-Path (Join-Path $ProjectRoot '.env'))) {
    throw 'Missing .env. Run windows\setup-paper.ps1, then add your Kalshi credentials.'
}

Set-Location $ProjectRoot
New-Item -ItemType Directory -Force -Path 'local' | Out-Null
& $Python 'scripts\btc15_live_monitor.py' --paper --risk-pct $RiskPercent --starting-balance-cents $StartingBalanceCents --trade-log 'local\paper_trade_log.json' --json-out 'local\paper_latest.json'
exit $LASTEXITCODE
