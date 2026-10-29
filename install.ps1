param([string]$PythonPath = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PIP_INDEX_URL = 'https://pypi.org/simple'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot 'data\browsers'
$taskTemp = Join-Path $PSScriptRoot 'data\tmp'
New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:TEMP = $taskTemp
$env:TMP = $taskTemp

function Check-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)." }
}

if (!(Get-Command node -ErrorAction SilentlyContinue) -or !(Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw 'Install Node.js 22.12+ or 24+ from https://nodejs.org/ first.'
}
if (!$PythonPath) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $PythonPath = & py -3.12 -c 'import sys; print(sys.executable)' 2>$null
    }
    if (!$PythonPath -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $PythonPath = (Get-Command python).Source
    }
}
if (!$PythonPath) { throw 'Python 3.12+ is required. Or pass -PythonPath with a full interpreter path.' }
& $PythonPath -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"'
Check-Exit 'Python version check'
if (!(Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $PythonPath -m venv .venv
    Check-Exit 'Virtual environment creation'
}
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $venvPython -m ensurepip --upgrade
Check-Exit 'pip setup'
& $venvPython -m pip install --index-url https://pypi.org/simple -r requirements.lock.txt
Check-Exit 'Python dependency installation'
& $venvPython -m pip install --index-url https://pypi.org/simple --no-deps -e .
Check-Exit 'Application installation'
& $venvPython -m playwright install chromium
Check-Exit 'Chromium installation'
& (Join-Path $PSScriptRoot 'install-browser.ps1')
Push-Location -LiteralPath (Join-Path $PSScriptRoot 'frontend')
try {
    & npm.cmd ci --registry=https://registry.npmjs.org --no-audit --no-fund
    Check-Exit 'Frontend dependency installation'
    & npm.cmd run build
    Check-Exit 'Frontend build'
} finally { Pop-Location }
Write-Host 'Setup complete. Run start.bat or .\start.ps1.' -ForegroundColor Green
