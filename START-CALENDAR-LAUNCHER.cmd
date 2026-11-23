@echo off
setlocal
call "%~dp0app\START-CALENDAR-LAUNCHER.cmd" %*
exit /b %ERRORLEVEL%
