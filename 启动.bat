@echo off
rem ---------------------------------------------------------------
rem  Launch the API Probe GUI.
rem  IMPORTANT: keep this file ASCII-only. cmd.exe parses .bat files
rem  using the OEM code page, so non-ASCII text here can be mangled
rem  into bogus commands (e.g. "'st' is not recognized...").
rem ---------------------------------------------------------------
cd /d "%~dp0"

set "PYW="
where pythonw.exe >nul 2>nul && set "PYW=pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"

if defined PYW (
  start "" "%PYW%" "%~dp0api_probe.py"
  exit /b 0
)

echo pythonw.exe not found - falling back to python (this window will stay open).
echo.
python "%~dp0api_probe.py"
if errorlevel 1 (
  echo.
  echo Failed to start. Is Python installed and available on PATH?
  echo Try:  python --version
  pause
)
