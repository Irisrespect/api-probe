@echo off
rem ---------------------------------------------------------------
rem  Build a single-file windowed exe with PyInstaller.
rem  The first run downloads PyInstaller, so it needs internet.
rem  Keep this file ASCII-only - see the note in the launcher .bat.
rem ---------------------------------------------------------------
cd /d "%~dp0"

echo === Installing / upgrading PyInstaller ===
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto fail

echo.
echo === Building single-file windowed exe ===
python -m PyInstaller --noconfirm --clean --onefile --windowed --name ApiProbe api_probe.py
if errorlevel 1 goto fail

echo.
echo Done. Output: %~dp0dist\ApiProbe.exe
echo Copy that exe anywhere - the target machine does not need Python.
pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1
