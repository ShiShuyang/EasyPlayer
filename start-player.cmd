@echo off
setlocal
set "PLAYER_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PLAYER_PY%" (
    echo The workspace .venv was not found. See README.md.
    pause
    exit /b 1
)
"%PLAYER_PY%" -c "from PySide6 import QtMultimedia, QtMultimediaWidgets" >nul 2>&1
if errorlevel 1 (
    echo Install dependencies from requirements.txt into the workspace .venv first.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0player.py" <nul >nul 2>&1
