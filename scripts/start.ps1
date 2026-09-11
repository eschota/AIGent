param([int]$Port = 8787, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$project = Split-Path $PSScriptRoot -Parent
$python = Join-Path $project '.venv\Scripts\python.exe'
$runtime = Join-Path $project '.local'
New-Item -ItemType Directory -Force $runtime | Out-Null
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) { Write-Output "Port $Port is already listening; no duplicate started."; exit 0 }
$arguments = @('"' + (Join-Path $project 'run.py') + '"', '--port', $Port)
if ($NoBrowser) { $arguments += '--no-browser' }
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $project -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'server.out.log') -RedirectStandardError (Join-Path $runtime 'server.err.log') -PassThru
Write-Output "AIGent launcher PID $($process.Id); http://127.0.0.1:$Port/"
