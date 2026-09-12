@echo off
rem Clean reset: kill every AIGent server/supervisor for THIS project, then start one supervised
rem instance. Use this to break a restart loop or duplicate-instance fight over port 8787.
setlocal
set "PROJECT=%~dp0.."
set "LOG=%PROJECT%\.local\reset.log"
echo [%date% %time%] reset requested > "%LOG%"
echo Stopping every AIGent python process for this project ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$proj='%PROJECT%'; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*'+$proj+'*run.py*' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force; Write-Output ('stopped PID '+$_.ProcessId) } catch { } }" >> "%LOG%" 2>&1
rem Also free the port from any leftover listener owned by this project.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$c=Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue; foreach($l in $c){ $p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$l.OwningProcess); if($p.CommandLine -like '*%PROJECT%*'){ try{Stop-Process -Id $p.ProcessId -Force}catch{} } }" >> "%LOG%" 2>&1
timeout /t 3 /nobreak > nul
echo Starting one supervised instance ...
start "" /min "%PROJECT%\.venv\Scripts\python.exe" "%PROJECT%\run.py" --supervise --no-browser
timeout /t 12 /nobreak > nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 http://127.0.0.1:8787/healthz; Write-Output ('healthz '+$r.StatusCode+' '+$r.Content) } catch { Write-Output ('healthz FAILED: '+$_.Exception.Message) }" >> "%LOG%" 2>&1
echo Done. Log: %LOG%   Open http://127.0.0.1:8787/
type "%LOG%"
timeout /t 8
