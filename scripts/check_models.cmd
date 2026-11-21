@echo off
REM Checks the Claude model IDs in config.yaml (free: no tokens used). Never prints the key.
setlocal
cd /d "%~dp0.."
if not exist "backend\.venv\Scripts\python.exe" (
  echo The Python environment is missing. Run setup.cmd first.
  pause
  exit /b 1
)
pushd backend
.venv\Scripts\python.exe -m app.chat.check_models
popd
pause
