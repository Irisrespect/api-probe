@echo off
rem ---------------------------------------------------------------
rem  Run the built-in self test (local mock server, no internet).
rem  Keep this file ASCII-only - see the note in the launcher .bat.
rem ---------------------------------------------------------------
cd /d "%~dp0"
python "%~dp0api_probe.py" --selftest
echo.
pause
