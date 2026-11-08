$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
function Checked([scriptblock] $Action) {
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE" }
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Install Python 3.11+ from python.org with Tcl/Tk and PATH enabled.' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Install Node.js 22+ LTS from nodejs.org, then reopen this launcher.' }
Checked { python -c "import sys,tkinter; assert sys.version_info >= (3,11)" }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) { Checked { python -m venv .venv } }
Checked { .\.venv\Scripts\python.exe -m pip install -r requirements.txt }
Push-Location -LiteralPath 'bridge'
try { Checked { npm.cmd ci --ignore-scripts } } finally { Pop-Location }
Write-Host 'Setup complete. Double-click Start.cmd.'
