param([int]$Port = 8787)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Split-Path $PSScriptRoot -Parent)).Path
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($process.CommandLine -notlike "*$project*run.py*") { throw "Port belongs to another service; refusing to stop it." }
    Stop-Process -Id $process.ProcessId
    Write-Output "Stopped AIGent PID $($process.ProcessId)."
}
