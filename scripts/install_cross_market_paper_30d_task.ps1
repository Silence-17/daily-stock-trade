[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "DailyStockAnalysis-CrossMarketPaper30D",
    [ValidateRange(1, 65535)]
    [int]$Port = 8001,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $WhatIfPreference -and -not $isAdministrator) {
    throw "Run this installer from an elevated PowerShell session."
}

$runner = (Resolve-Path (Join-Path $PSScriptRoot "run_cross_market_paper_30d.ps1")).Path
$powershell = Join-Path $PSHOME "powershell.exe"
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Port {1}' -f $runner, $Port
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory (Split-Path $PSScriptRoot -Parent)

$triggers = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "07:45"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "08:50"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "09:23"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "10:38"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "13:28"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "14:28"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "14:55"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "21:14"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "21:25"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "22:14"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "22:25"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "23:25"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Tuesday, Wednesday, Thursday, Friday, Saturday -At "00:55"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Tuesday, Wednesday, Thursday, Friday, Saturday -At "01:55"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Tuesday, Wednesday, Thursday, Friday, Saturday -At "03:55"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Tuesday, Wednesday, Thursday, Friday, Saturday -At "04:55"
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -WakeToRun
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$description = "Run the 30-session cross-market paper campaign on localhost:$Port with market-window wake triggers."
$startedNow = $false

if ($PSCmdlet.ShouldProcess($TaskName, "Register SYSTEM scheduled task")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $taskPrincipal `
        -Description $description `
        -Force | Out-Null
    if ($Start) {
        $registeredTask = Get-ScheduledTask -TaskName $TaskName
        if ($registeredTask.State -ne "Running") {
            Start-ScheduledTask -TaskName $TaskName
            $startedNow = $true
        }
    }
}

[pscustomobject]@{
    task_name = $TaskName
    runner = $runner
    port = $Port
    start_requested = [bool]$Start
    started_now = $startedNow
    wake_trigger_count = $triggers.Count - 1
}
