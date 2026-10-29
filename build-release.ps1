param([string]$Version = '0.2.0', [switch]$SkipTests)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$bundleName = "ShoppingAssistant-$Version-windows-x64"
$dist = Join-Path $PSScriptRoot 'release'
if (Test-Path -LiteralPath (Join-Path $dist $bundleName)) { throw 'Version directory already exists. Keep it intact and choose a new version or output directory.' }
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot 'data\browsers'
& $python -m pip install --index-url https://pypi.org/simple -r requirements-build.lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed' }
npm --prefix frontend ci --registry https://registry.npmjs.org
if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed' }
npm --prefix frontend run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
if (!$SkipTests) {
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; release was not built' }
}
$basePythonDir = & $python -c 'import sys; print(sys.base_prefix)'
$binaryOptions = @()
foreach ($dllName in @('ffi.dll', 'sqlite3.dll')) {
    $dllPath = Join-Path $basePythonDir "Library\bin\$dllName"
    if (Test-Path -LiteralPath $dllPath) { $binaryOptions += @('--add-binary', "$dllPath;.") }
}
& $python -m PyInstaller --noconfirm --onedir --console --name $bundleName --paths $PSScriptRoot --distpath $dist --workpath build\pyinstaller --specpath build --collect-submodules uvicorn --add-data "$(Join-Path $PSScriptRoot 'frontend\dist');frontend\dist" --version-file (Join-Path $PSScriptRoot 'scripts\windows_version.txt') @binaryOptions scripts\release_entry.py
if ($LASTEXITCODE -ne 0) { throw 'Executable build failed' }
& $python scripts\package_release.py --version $Version
if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed' }
& $python scripts\smoke_release.py (Join-Path $dist $bundleName)
if ($LASTEXITCODE -ne 0) { throw 'Compiled runtime smoke check failed; do not distribute this build' }
& $python scripts\validate_release.py (Join-Path $dist $bundleName)
if ($LASTEXITCODE -ne 0) { throw 'Release integrity or private-data check failed' }
Write-Host "Built release\$bundleName.zip"
