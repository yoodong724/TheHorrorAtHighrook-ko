@echo off
setlocal DisableDelayedExpansion
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0steam-auto.ps1" -Action Restore
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Restore failed. Read the error above.
pause
exit /b %RESULT%
