@echo off
REM Starts the dashboard on this computer only (127.0.0.1:8000): builds the screen, then runs one server.
REM Open http://127.0.0.1:8000 in the browser. Press Ctrl+C in this window to stop.
setlocal
cd /d "%~dp0.."

if not exist "backend\.venv\Scripts\python.exe" (
  echo The Python environment is missing. Run setup.cmd first.
  pause
  exit /b 1
)
if not exist "frontend\node_modules\.bin\concurrently.cmd" (
  echo The frontend packages are missing. Run setup.cmd first.
  pause
  exit /b 1
)

if not exist "secrets\login.json" (
  echo No login passcode yet. Run set_passcode.cmd first, then start again.
  pause
  exit /b 1
)

netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if not errorlevel 1 (
  echo Port 8000 is already in use - the dashboard may already be running in another window.
  echo Close that window, or find the program with: netstat -ano ^| findstr :8000
  pause
  exit /b 1
)

echo Building the screen ...
pushd frontend
call npm.cmd run build
if errorlevel 1 (
  popd
  echo The screen build failed - see the messages above.
  pause
  exit /b 1
)
popd

echo.
echo Dashboard: http://127.0.0.1:8000   (this computer only; your phone reaches it through Tailscale serve)
echo Press Ctrl+C here to stop.
backend\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
pause
