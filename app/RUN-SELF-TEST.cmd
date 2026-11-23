@echo off
call "%~dp0START-CALENDAR-LAUNCHER.cmd" --self-test
set "SELF_TEST_EXIT=%ERRORLEVEL%"
echo.
echo Offline test exit code: %SELF_TEST_EXIT%
pause
exit /b %SELF_TEST_EXIT%
