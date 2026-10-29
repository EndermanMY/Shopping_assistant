param([ValidateSet('system','direct')][string]$Network = 'system')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$dataRoot = if ($env:SHOPPING_DATA_DIR) { $env:SHOPPING_DATA_DIR } else { Join-Path $PSScriptRoot 'data-api' }
New-Item -ItemType Directory -Force -Path $dataRoot | Out-Null
$settingsPath = Join-Path $dataRoot 'browser-network.json'
$profileName = 'jd'
if (Test-Path -LiteralPath $settingsPath) {
    $current = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    if ($current.profile_name) { $profileName = $current.profile_name }
}
if ($profileName -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,100}$') { throw 'Invalid app-owned profile name' }
@{mode=$Network;connection_mode='native_cdp';profile_name=$profileName} | ConvertTo-Json | Set-Content -LiteralPath $settingsPath -Encoding utf8
Write-Host "Assistant browser network: $Network. Restart the assistant browser to apply."
Write-Host 'System proxy and personal Chrome were not changed.'
