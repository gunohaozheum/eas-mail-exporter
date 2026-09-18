@echo off
rem Launch the GUI. Keep this file ASCII-only with CRLF line endings:
rem cmd.exe mis-parses batch files that use UTF-8 plus LF endings.
setlocal
cd /d "%~dp0"
title EAS Mail Exporter

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0app_gui.pyw"
    exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0app_gui.pyw"
    if errorlevel 1 pause
    exit /b 0
)

echo.
echo   Python not found.
echo   Please install Python 3.9 or newer: https://www.python.org/downloads/
echo   During setup, tick "Add Python to PATH".
echo.
pause
