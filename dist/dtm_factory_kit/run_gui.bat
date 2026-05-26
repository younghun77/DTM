@echo off
setlocal EnableExtensions
set "HERE=%~dp0"

set "PY=py -3"
where py >nul 2>nul
if errorlevel 1 set "PY=python"

%PY% --version >nul 2>nul
if errorlevel 1 goto :nopy

%PY% -m pip show pyserial >nul 2>nul
if errorlevel 1 call :install
%PY% -m pip show paramiko >nul 2>nul
if errorlevel 1 call :install

%PY% "%HERE%tools\dtm_rx_runner.py"
goto :eof

:install
echo Installing required Python packages (pyserial, paramiko) ...
%PY% -m pip install --quiet pyserial paramiko
goto :eof

:nopy
echo.
echo [ERROR] Python 3 is not installed or not on PATH.
echo Install Python 3.x from https://www.python.org/downloads/
echo and make sure to check "Add Python to PATH" during setup.
echo.
pause
goto :eof