$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtimeLock = Get-Content -LiteralPath 'browser-runtime.lock.json' -Raw | ConvertFrom-Json
if ($runtimeLock.version -notmatch '^\d+\.\d+\.\d+\.\d+$' -or $runtimeLock.archive_sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid browser version lock' }
$expectedUrl = "https://storage.googleapis.com/chrome-for-testing-public/$($runtimeLock.version)/win64/chrome-win64.zip"
$relativePath = "chrome-stable-$($runtimeLock.version)/chrome-win64/chrome.exe"
if ($runtimeLock.source -ne $expectedUrl -or $runtimeLock.relative_path -ne $relativePath) { throw 'Only the pinned official project browser is allowed' }
$browserRoot = Join-Path $PSScriptRoot 'data\browsers'
$target = Join-Path $browserRoot "chrome-stable-$($runtimeLock.version)"
$binary = Join-Path $browserRoot $relativePath
if (!(Test-Path -LiteralPath $binary)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'data\tools') | Out-Null
    $archive = Join-Path $PSScriptRoot "data\tools\chrome-stable-$($runtimeLock.version).zip"
    if (!(Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $runtimeLock.archive_sha256) {
        Invoke-WebRequest -Uri $expectedUrl -OutFile $archive -TimeoutSec 300
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $runtimeLock.archive_sha256) { throw 'Official browser archive checksum mismatch' }
    if (Test-Path -LiteralPath $target) { throw 'Incomplete browser directory exists; preserve it and inspect before retrying' }
    Expand-Archive -LiteralPath $archive -DestinationPath $target
}
if ((Get-Item -LiteralPath $binary).VersionInfo.ProductVersion -ne $runtimeLock.version) { throw 'Browser version does not match the lock' }
@{version=$runtimeLock.version;relative_path=$relativePath} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $browserRoot 'runtime.json') -Encoding utf8
Write-Host "Pinned independent browser ready: $($runtimeLock.version)"
