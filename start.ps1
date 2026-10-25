param([ValidateRange(1024,65535)][int]$Port = 18765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $venvPython) -or !(Test-Path -LiteralPath 'frontend\dist\index.html')) {
    throw 'Please run install.bat or .\install.ps1 first.'
}
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot 'data\browsers'
$env:SHOPPING_PORT = $Port
& $venvPython -m assistant
exit $LASTEXITCODE
