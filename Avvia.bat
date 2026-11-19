@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Ambiente Python non trovato. Apri un terminale in questa cartella ed esegui:
    echo python -m venv .venv
    pause
    exit /b 1
)

start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py" %*
exit /b 0
