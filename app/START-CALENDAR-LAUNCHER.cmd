@echo off
setlocal
title K12 Calendar Launcher
pushd "%~dp0" || goto folder_error
if not exist "launcher.py" goto incomplete
if not exist "config.example.json" goto incomplete
if not exist "ui\index.html" goto incomplete
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 goto py_launcher
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 goto python_launcher
echo Python 3.10 or newer was not found.
echo See RED_TEAM_GUIDE.html for Python installation and verification.
echo Official Windows installers: https://www.python.org/downloads/windows/
set "LAUNCH_EXIT=2"
goto finish
:py_launcher
py -3 "%~dp0launcher.py" %*
set "LAUNCH_EXIT=%ERRORLEVEL%"
goto finish
:python_launcher
python "%~dp0launcher.py" %*
set "LAUNCH_EXIT=%ERRORLEVEL%"
goto finish
:incomplete
echo Extract the entire ZIP into one folder before starting the launcher.
echo Keep launcher.py, config.json, ui, engine and templates together.
set "LAUNCH_EXIT=2"
goto finish
:folder_error
echo The extracted folder could not be opened.
pause
exit /b 2
:finish
popd
if not "%LAUNCH_EXIT%"=="0" pause
exit /b %LAUNCH_EXIT%
