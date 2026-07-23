param(
  [switch]$IncludeVnpy,
  [switch]$SkipDependencyInstall,
  [string]$VnpyGatewayPluginsJson = $env:VNPY_GATEWAY_PLUGINS_JSON
)

$ErrorActionPreference = 'Stop'

Write-Host '=== Daily Stock Analysis Desktop Build ==='

& "${PSScriptRoot}\build-backend.ps1" `
  -IncludeVnpy:$IncludeVnpy `
  -SkipDependencyInstall:$SkipDependencyInstall `
  -VnpyGatewayPluginsJson $VnpyGatewayPluginsJson
& "${PSScriptRoot}\build-desktop.ps1"

Write-Host 'All builds completed.'
