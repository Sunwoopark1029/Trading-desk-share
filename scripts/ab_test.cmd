@echo off
REM Candle A/B test for the AI chat. CLOSE THE DASHBOARD FIRST (it uses its own Toss connection).
REM Shows a free cost estimate and asks before spending anything.
setlocal
cd /d "%~dp0.."
if not exist "backend\.venv\Scripts\python.exe" (
  echo The Python environment is missing. Run setup.cmd first.
  pause
  exit /b 1
)
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if not errorlevel 1 (
  echo The dashboard is running. Close the start.cmd window first, then run this again.
  pause
  exit /b 1
)
echo Starting... loading Python libraries can take up to 30 seconds.
pushd backend
.venv\Scripts\python.exe -u -m app.chat.ab_test
popd
pause
