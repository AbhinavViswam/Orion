@echo off
rem Makes .venv here (if missing) and runs Setup.py (packages + Playwright browsers). Run again any time.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Python 3.11 or newer was not found. Install it from python.org, then run this again.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" Setup.py
if errorlevel 1 ( pause & exit /b 1 )
echo.
echo Ready. Start Orion with start.bat.
pause
