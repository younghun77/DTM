@echo off
REM ----------------------------------------------------------------------
REM Push DTM Factory Kit source + release zip to
REM     https://github.com/younghun77/DTM
REM
REM Prereqs:
REM   - git installed and in PATH
REM   - You have push rights to younghun77/DTM (PAT or SSH key configured)
REM
REM Usage:
REM   publish_to_github.bat            (uses HTTPS)
REM   publish_to_github.bat ssh        (uses git@github.com:younghun77/DTM.git)
REM ----------------------------------------------------------------------
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
pushd "%ROOT%"

set "REMOTE_HTTPS=https://github.com/younghun77/DTM.git"
set "REMOTE_SSH=git@github.com:younghun77/DTM.git"
set "REMOTE=%REMOTE_HTTPS%"
if /I "%~1"=="ssh" set "REMOTE=%REMOTE_SSH%"

where git >nul 2>nul || (echo [ERR] git not found in PATH & exit /b 1)

REM Init repo if missing
if not exist ".git" (
    echo [INIT] git init
    git init -b main
)

REM Make sure we are on main
git checkout -B main 2>nul

REM Create/refresh .gitignore so we don't push huge build outputs
> .gitignore (
    echo build/
    echo .west/
    echo __pycache__/
    echo *.pyc
    echo *.bak
    echo *.tmp
    echo dist/dtm_factory_kit/private_key.pem
    echo dist/dtm_factory_kit/tools/private_key.pem
    echo tools/private_key.pem
    echo tools/private_key.ppk
    echo notify.ini
    echo tools/notify.ini
    echo dist/dtm_factory_kit/tools/notify.ini
    REM dtm_factory_kit.zip bundles the DUT SSH private key - never publish it.
    echo dist/dtm_factory_kit.zip
)

REM Stage source (the release zip is intentionally NOT pushed: it embeds the
REM DUT SSH private key. Distribute the kit through a private channel instead.)
git add -A

git -c user.name="DTM Factory Kit" -c user.email="dtm@local" ^
    commit -m "Publish DTM Factory Kit (source + release zip)" || echo [INFO] nothing to commit

REM Configure remote
git remote remove origin 2>nul
git remote add origin %REMOTE%
echo [PUSH] origin -> %REMOTE%
git push -u origin main

popd
endlocal
