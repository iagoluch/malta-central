@echo off
setlocal
rem Atalho de conveniencia para tools\malta_desktop_autostart.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0malta_desktop_autostart.ps1" %*
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
if not "%RC%"=="0" pause
exit /b %RC%
