@echo off
rem Restart the local AIGent server after a code update: run the checks, then stop/start.
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
timeout /t 2 /nobreak > nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT%\scripts\start.ps1" -NoBrowser >> "%LOG%" 2>&1
echo restart done >> "%LOG%"
echo Server restarted. Log: %LOG%
popd
timeout /t 5
