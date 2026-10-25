$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$Host.UI.RawUI.WindowTitle = 'Shopping Assistant - Source Service Logs'
Write-Host 'SOURCE SERVICE - LIVE LOG VIEW' -ForegroundColor Cyan
Write-Host 'The original service is running in the background.'
Write-Host 'Closing this log window does not stop purchase tasks.' -ForegroundColor Yellow
Write-Host ''
Get-Content -LiteralPath 'data\server.log' -Encoding utf8 -Tail 20
Write-Host ''
Get-Content -LiteralPath 'data\server-error.log' -Encoding utf8 -Tail 20 -Wait
