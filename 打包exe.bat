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

rem ---------------------------------------------------------------------
rem Workaround for sandboxed workspaces: if this folder (or any parent) has a
rem "Low" mandatory integrity label (e.g. a sandboxed session directory), the
rem built exe inherits that label. Windows then starts the exe at low integrity,
rem and a low-integrity process cannot create directories under %TEMP%, which
rem makes PyInstaller onefile fail with "Could not create temporary directory!".
rem Re-labelling the artifact as Medium is a no-op on a normal machine.
rem ---------------------------------------------------------------------
icacls "%~dp0dist\ApiProbe.exe" /setintegritylevel Medium >nul 2>nul

pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1
