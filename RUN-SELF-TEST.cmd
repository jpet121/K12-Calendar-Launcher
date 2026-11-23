@echo off
setlocal
call "%~dp0app\RUN-SELF-TEST.cmd" %*
exit /b %ERRORLEVEL%
