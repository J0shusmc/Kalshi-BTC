# Serves the local, read-only paper dashboard at http://127.0.0.1:8787.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot 'venv\Scripts\python.exe'

if (-not (Test-Path $Python)) {
    throw 'The Python environment is missing. Run windows\setup-paper.ps1 first.'
}

Set-Location $ProjectRoot
& $Python 'scripts\mobile_viewer.py' --snapshot 'local\paper_latest.json'
exit $LASTEXITCODE
