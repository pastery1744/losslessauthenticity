@echo off
rem Launches the Hi-Res Authenticity Checker (no console window).
rem You can also drag files/folders onto this .bat to check them straight away.
setlocal
set "HERE=%~dp0"
set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not exist "%PYW%" (
  for %%P in (pythonw.exe) do set "PYW=%%~$PATH:P"
)
if not defined PYW (
  echo Python 3 was not found. Install it with:  winget install Python.Python.3.12
  pause
  exit /b 1
)
start "" "%PYW%" "%HERE%app.py" %*
