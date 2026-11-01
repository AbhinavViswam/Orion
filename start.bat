@echo off
rem Starts Orion from .venv, with no console window. Run setup.bat first.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" ( echo Run setup.bat first. & pause & exit /b 1 )
start "" ".venv\Scripts\pythonw.exe" "%~dp0Main.py"
