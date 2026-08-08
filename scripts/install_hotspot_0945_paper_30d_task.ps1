[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "DailyStockAnalysis-Hotspot0945Paper30D",
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $WhatIfPreference -and -not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this installer from an elevated PowerShell session."
}

$runner = (Resolve-Path (Join-Path $PSScriptRoot "run_hotspot_0945_paper_30d.ps1")).Path
$powershell = Join-Path $PSHOME "powershell.exe"
$escapedRunner = $runner.Replace("'", "''")
$command = "& '$escapedRunner' -Phase Auto"
$encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
$arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $encodedCommand"
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
$triggers = @(
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "09:38"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "14:55"
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -WakeToRun
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

if ($PSCmdlet.ShouldProcess($TaskName, "Register two-trigger SYSTEM scheduled task")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $taskPrincipal `
        -Description "Run two isolated 30-session 09:45 hotspot paper campaigns at 09:38 and 14:55." `
        -Force | Out-Null
    if ($Start) {
        Start-ScheduledTask -TaskName $TaskName
    }
}

[pscustomobject]@{
    task_name = $TaskName
    runner = $runner
    trigger_count = $triggers.Count
    start_requested = [bool]$Start
}
