[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "DailyStockAnalysis-Hotspot0945Paper30D",
    [string]$ExitWatchTaskName = "DailyStockAnalysis-Hotspot0927ExitWatch30D",
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
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "09:28"
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

$exitWatchCommand = "& '$escapedRunner' -Phase Watch-Exit"
$exitWatchEncodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($exitWatchCommand))
$exitWatchArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $exitWatchEncodedCommand"
$exitWatchAction = New-ScheduledTaskAction -Execute $powershell -Argument $exitWatchArguments
$exitWatchTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "09:27"
$exitWatchSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -WakeToRun

if ($PSCmdlet.ShouldProcess($TaskName, "Register two-trigger SYSTEM scheduled task")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $taskPrincipal `
        -Description "Prepare at 09:28, watch entries from 09:30 to 09:35, and value two isolated 30-session hotspot paper campaigns at 14:55." `
        -Force | Out-Null
    if ($Start) {
        Start-ScheduledTask -TaskName $TaskName
    }
}

if ($PSCmdlet.ShouldProcess($ExitWatchTaskName, "Register 09:27 SYSTEM exit-watch task")) {
    Register-ScheduledTask `
        -TaskName $ExitWatchTaskName `
        -Action $exitWatchAction `
        -Trigger $exitWatchTrigger `
        -Settings $exitWatchSettings `
        -Principal $taskPrincipal `
        -Description "Apply the 09:27 auction exit rule and monitor eligible holdings through 14:54." `
        -Force | Out-Null
    if ($Start) {
        Start-ScheduledTask -TaskName $ExitWatchTaskName
    }
}

[pscustomobject]@{
    task_name = $TaskName
    exit_watch_task_name = $ExitWatchTaskName
    runner = $runner
    trigger_count = $triggers.Count
    exit_watch_trigger_count = 1
    start_requested = [bool]$Start
}
