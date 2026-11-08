param(
    [string] $Python = "$PSScriptRoot\.venv\Scripts\python.exe",
    [string] $BuildRoot = "$PSScriptRoot\build",
    [string] $DistRoot = "$PSScriptRoot\Windows",
    [switch] $SkipInstall
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$dest = Join-Path $DistRoot 'MusicBridge'
if (Test-Path -LiteralPath "$dest\data") {
    throw 'Existing portable user data found. Copy the project to a fresh build directory before rebuilding; no user data was removed.'
}
function Checked([scriptblock] $Action) {
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE" }
}
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run setup.ps1 first, or pass -Python with a configured virtualenv.' }
if (-not $SkipInstall) { Checked { & $Python -m pip install -r requirements-build.txt } }
$icon = Join-Path $PSScriptRoot 'assets\musicbridge.ico'
Checked { & $Python -m PyInstaller --noconfirm --windowed --onedir --name MusicBridge --manifest "$PSScriptRoot\windows.manifest" --icon $icon --add-data "$($icon):assets" --collect-all qqmusic_api --collect-all niquests --collect-all wassima --collect-all orjson --collect-all jsonpath_ng --distpath $DistRoot --workpath "$BuildRoot\pyinstaller" --specpath $BuildRoot main.py }
New-Item -ItemType Directory -Force -Path "$dest\bridge", "$dest\runtime" | Out-Null
Copy-Item -LiteralPath 'bridge\call.cjs', 'bridge\package.json', 'bridge\package-lock.json' -Destination "$dest\bridge" -Force
Copy-Item -LiteralPath 'bridge\node_modules' -Destination "$dest\bridge" -Recurse -Force
Copy-Item -LiteralPath (Get-Command node).Source -Destination "$dest\runtime\node.exe" -Force
if (Test-Path -LiteralPath 'runtime\NODE-LICENSE') { Copy-Item -LiteralPath 'runtime\NODE-LICENSE' -Destination "$dest\runtime" -Force }
Copy-Item -LiteralPath 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md' -Destination $dest -Force
Copy-Item -LiteralPath 'docs', 'third_party_sources' -Destination $dest -Recurse -Force
New-Item -ItemType Directory -Force -Path "$dest\source" | Out-Null
Copy-Item -LiteralPath 'musicbridge', 'tests', 'assets' -Destination "$dest\source" -Recurse -Force
Copy-Item -LiteralPath 'main.py', 'windows.manifest', 'requirements.txt', 'requirements-build.txt', 'LICENSE' -Destination "$dest\source" -Force
Write-Host "Built: $dest\MusicBridge.exe (keep the entire folder together)"
