[CmdletBinding()]
param(
    [ValidateRange(15, 1440)]
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = 'Stop'
$portalRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$portalPython = Join-Path $portalRoot '.venv\Scripts\pythonw.exe'
$portalRunner = Join-Path $portalRoot 'scripts\run_slack_sync.py'
$portalTaskName = 'OpportunityHub-SlackImport'

if (-not (Test-Path -LiteralPath $portalPython) -or -not (Test-Path -LiteralPath $portalRunner)) {
    throw 'The project virtual environment or scheduled Slack importer script is missing.'
}
if (Get-ScheduledTask -TaskName $portalTaskName -ErrorAction SilentlyContinue) {
    throw "Scheduled task $portalTaskName already exists and was left unchanged."
}

$portalAction = New-ScheduledTaskAction -Execute $portalPython `
    -Argument ('-X utf8 "{0}"' -f $portalRunner) -WorkingDirectory $portalRoot
$portalTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes($IntervalMinutes)) `
    -RepetitionInterval ([TimeSpan]::FromMinutes($IntervalMinutes))
$portalSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::FromMinutes(15))
$portalPrincipal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $portalTaskName -Action $portalAction `
    -Trigger $portalTrigger -Settings $portalSettings -Principal $portalPrincipal `
    -Description 'Read configured Slack channels using the local session and add draft opportunities or import review items.'
Get-ScheduledTaskInfo -TaskName $portalTaskName | Select-Object NextRunTime, LastTaskResult
