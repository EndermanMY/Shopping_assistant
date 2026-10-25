param([ValidateRange(1024,65535)][int]$Port = 18765, [string]$InterfaceAlias = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (!$admin) { throw 'Run this script as Administrator to allow the local console through Windows Firewall.' }
if (!$InterfaceAlias) {
    $physicalNames = @(Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Select-Object -ExpandProperty Name)
    $profiles = @(Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq 'Private' -and $_.InterfaceAlias -in $physicalNames })
    if ($profiles.Count -ne 1) { throw 'Specify -InterfaceAlias with one connected Private physical network adapter.' }
    $InterfaceAlias = $profiles[0].InterfaceAlias
}
$profile = Get-NetConnectionProfile -InterfaceAlias $InterfaceAlias
if ($profile.NetworkCategory -ne 'Private') { throw 'This script only allows the service on an existing Private network. It does not change the network category.' }
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen | Select-Object -First 1
$serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
$releaseExe = Join-Path $PSScriptRoot 'ShoppingAssistant.exe'
$isRelease = $serverProcess.ExecutablePath -and ([IO.Path]::GetFullPath($serverProcess.ExecutablePath) -eq [IO.Path]::GetFullPath($releaseExe))
if ((!$isRelease -and $serverProcess.CommandLine -notmatch '-m assistant') -or !$serverProcess.ExecutablePath) { throw 'The listening process is not the shopping assistant.' }

# Authenticate this workspace's service before opening its port.
$code = (Get-Content -LiteralPath 'data\access.json' -Raw | ConvertFrom-Json).code
$base = "http://127.0.0.1:$Port"
Invoke-RestMethod -Uri "$base/api/auth/login" -Method Post -ContentType 'application/json' -Body (@{code=$code} | ConvertTo-Json) -SessionVariable session | Out-Null
Invoke-RestMethod -Uri "$base/api/overview" -WebSession $session | Out-Null
Invoke-RestMethod -Uri "$base/api/auth/logout" -Method Post -WebSession $session | Out-Null

$ruleName = "ShoppingAssistantLocal$Port"
$description = "Shopping Assistant local console: $PSScriptRoot"
$existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Description -ne $description) { throw 'A rule with this name belongs to another installation; it was not changed.' }
    Write-Host "Rule $ruleName already exists. Review it with Get-NetFirewallRule before making changes."
} else {
    New-NetFirewallRule -Name $ruleName -DisplayName "Shopping Assistant local TCP $Port" -Description $description -Direction Inbound -Action Allow -Enabled True -Profile Private -Protocol TCP -LocalPort $Port -Program $serverProcess.ExecutablePath -RemoteAddress LocalSubnet -InterfaceAlias $InterfaceAlias | Out-Null
    Write-Host "Allowed TCP $Port on $InterfaceAlias, Private profile, LocalSubnet only."
}
Write-Host "To undo: Remove-NetFirewallRule -Name $ruleName"
