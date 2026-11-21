@echo off
REM Sets the login passcode (every device) and the order PIN (phone sends). Typed here, hidden, never shown.
REM Only a scrambled hash is saved, in secrets\login.json.
setlocal
cd /d "%~dp0.."
if not exist "backend\.venv\Scripts\python.exe" (
  echo The Python environment is missing. Run setup.cmd first.
  pause
  exit /b 1
)
pushd backend
.venv\Scripts\python.exe -m app.set_passcode
popd
pause
