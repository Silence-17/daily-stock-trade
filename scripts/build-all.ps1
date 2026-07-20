param(
  [switch]$IncludeVnpy,
  [switch]$SkipDependencyInstall
)

$ErrorActionPreference = 'Stop'

Write-Host '=== Daily Stock Analysis Desktop Build ==='

& "${PSScriptRoot}\build-backend.ps1" `
  -IncludeVnpy:$IncludeVnpy `
  -SkipDependencyInstall:$SkipDependencyInstall
& "${PSScriptRoot}\build-desktop.ps1"

Write-Host 'All builds completed.'
