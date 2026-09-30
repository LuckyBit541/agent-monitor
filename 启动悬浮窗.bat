@echo off
setlocal
rem Launch the Cursor session overlay (cursor-float)
rem ASCII-only on purpose: cmd.exe reads .bat with the OEM codepage.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
