@echo off
rem ASCII-only on purpose: cmd renders this file in the OEM code page, so
rem non-ASCII text here would be mojibake on some systems.
setlocal
cd /d "%~dp0"

rem Console Python does the checking; the windowless one starts the card, because
rem a pythonw process has no console to show an error on.
set "CHECK="
where python >nul 2>nul
if not errorlevel 1 set "CHECK=python"
if not defined CHECK (
  where py >nul 2>nul
  if not errorlevel 1 set "CHECK=py -3"
)
if not defined CHECK (
  echo [cursor-float] Python 3.10+ not found. Install it and tick "Add to PATH".
  pause
  exit /b 1
)

%CHECK% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
  echo [cursor-float] PySide6 is missing. Installing it now, this needs network
  echo [cursor-float] and may take a few minutes. PySide6 is a few hundred MB.
  %CHECK% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [cursor-float] Install failed. Run this by hand to see why:
    echo     %CHECK% -m pip install PySide6
    pause
    exit /b 1
  )
)

rem Derive the windowless interpreter from the one we just validated, instead of
rem looking it up on PATH: pythonw is not always there even when python is.
set "PYW="
%CHECK% -c "import os,sys;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" > "%TEMP%\cursor-float-pyw.txt" 2>nul
set /p PYW=<"%TEMP%\cursor-float-pyw.txt"
del "%TEMP%\cursor-float-pyw.txt" >nul 2>nul

if defined PYW if exist "%PYW%" (
  rem Redirect the child stdio: otherwise it inherits this console, and running
  rem this script from a terminal would not return until the card is closed.
  start "" "%PYW%" "%~dp0app.py" %* 1>nul 2>nul
  exit /b 0
)

rem No windowless interpreter: run with a console so errors stay visible.
%CHECK% "%~dp0app.py" %*
if errorlevel 1 pause
exit /b 0
