@echo off
rem Restart the local AIGent server after a code update: run the checks, then stop/start.
rem If a supervisor (run.py --supervise) is already alive, only the stale listener is stopped:
rem the supervisor starts the new revision itself.
setlocal
set "PROJECT=%~dp0.."
pushd "%PROJECT%"
set "LOG=%PROJECT%\.local\restart-last.log"
echo [%date% %time%] restart requested > "%LOG%"
echo Running checks (log: %LOG%) ...
".venv\Scripts\python.exe" -m pytest -q -x >> "%LOG%" 2>&1
set "TESTS=%ERRORLEVEL%"
".venv\Scripts\python.exe" -m ruff check connector tests run.py >> "%LOG%" 2>&1
set "LINT=%ERRORLEVEL%"
echo tests=%TESTS% lint=%LINT% >> "%LOG%"
if not "%TESTS%"=="0" (
  echo Tests failed (exit %TESTS%). Server NOT restarted. See %LOG%
  echo restart skipped: tests failed >> "%LOG%"
  popd
  pause
  exit /b %TESTS%
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT%\scripts\stop.ps1" >> "%LOG%" 2>&1
timeout /t 3 /nobreak > nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s='%PROJECT%\.local\supervisor.json'; if (Test-Path $s) { $age=(Get-Date)-(Get-Item $s).LastWriteTime; if ($age.TotalSeconds -lt 180) { Write-Output 'supervisor alive: it will start the new revision'; exit 10 } }; exit 0" >> "%LOG%" 2>&1
if "%ERRORLEVEL%"=="10" (
  echo Supervisor is running; waiting for it to bring the new revision up ...
  timeout /t 45 /nobreak > nul
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT%\scripts\start.ps1" -NoBrowser >> "%LOG%" 2>&1
  timeout /t 8 /nobreak > nul
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 http://127.0.0.1:8787/healthz; Write-Output ('healthz ' + $r.StatusCode + ' ' + $r.Content) } catch { Write-Output ('healthz FAILED: ' + $_.Exception.Message) }" >> "%LOG%" 2>&1
echo restart done >> "%LOG%"
echo Done. Log: %LOG%
popd
timeout /t 5
