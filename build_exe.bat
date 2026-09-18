@echo off
rem Build a standalone .exe with PyInstaller. ASCII-only, CRLF endings.
setlocal
cd /d "%~dp0"
title Build standalone exe

echo This installs PyInstaller and builds a single-file exe.
echo The result appears in the dist folder; Python is not needed on the target PC.
echo.

python -m pip install --upgrade pyinstaller || goto :fail
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "EAS Mail Exporter" ^
    --collect-submodules eas ^
    app_gui.pyw || goto :fail

echo.
echo Done: dist\EAS Mail Exporter.exe
pause
exit /b 0

:fail
echo.
echo Build failed. Check the messages above.
pause
exit /b 1
