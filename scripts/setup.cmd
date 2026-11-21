@echo off
REM One-time setup: Python environment + packages (+ frontend packages once the frontend exists).
setlocal
cd /d "%~dp0.."
echo.
echo === Trading Desk setup ===
echo.

if exist "backend\.venv\Scripts\python.exe" goto :venv_ok
echo [1/3] Creating Python 3.15 environment in backend\.venv ...
py -V:3.15 -m venv "backend\.venv"
if errorlevel 1 (
  echo ERROR: could not create the Python environment.
  echo Check that Python 3.15 is installed: run "py --list" in a terminal.
  goto :fail
)
goto :pip
:venv_ok
echo [1/3] Python environment already exists - skipping.

:pip
if not exist "backend\requirements.txt" (
  echo ERROR: backend\requirements.txt is missing.
  goto :fail
)
echo [2/3] Installing Python packages ...
"backend\.venv\Scripts\python.exe" -m pip install -r "backend\requirements.txt"
if errorlevel 1 (
  echo ERROR: Python package install failed. Read the messages above.
  goto :fail
)

if not exist "frontend\package.json" (
  echo [3/3] Frontend not created yet - skipping npm install.
  goto :done
)
echo [3/3] Installing frontend packages ...
pushd frontend
call npm.cmd install
if errorlevel 1 (
  popd
  echo ERROR: npm install failed. Check that Node.js is installed: run "node -v".
  goto :fail
)
popd

:done
echo.
echo Setup finished OK. Next: double-click start.cmd
echo.
pause
exit /b 0

:fail
echo.
echo Setup FAILED. Nothing was started. Fix the error above and run setup.cmd again.
echo.
pause
exit /b 1
