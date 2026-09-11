$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Split-Path $PSScriptRoot -Parent)).Path
$action = New-ScheduledTaskAction -Execute (Join-Path $project '.venv\Scripts\python.exe') -Argument ('"' + (Join-Path $project 'run.py') + '" --no-browser') -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName 'AIGent' -Action $action -Trigger $trigger -Settings $settings -Description 'Local AIGent Telegram agent connector' -Force
