@echo off
REM ====================================================================
REM restore.bat -- LCSAS recovery driver for Windows.
REM
REM Run this file by double-clicking it in File Explorer, or by typing
REM `restore.bat` from a CMD or PowerShell prompt at the disc root.
REM
REM This is the Windows equivalent of recovery/scripts/restore.sh.  The
REM tier order matches the POSIX driver:
REM
REM   Tier 1.  bin\<arch>\lcsas-restore.exe   (prebuilt, static)
REM   Tier 2.  bin\<arch>\rustic-static.exe   (vendored cross-check)
REM
REM Tiers 3 and 4 (rebuild from source) are not attempted on stock
REM Windows because Windows does not ship a C compiler or POSIX make.
REM Run them from WSL or a Linux/macOS host if you need to.
REM
REM Tier 5 (Python fallback) is attempted if `py` (the Python launcher)
REM is on PATH and standalone_restorer.py is available.  Set the env
REM var LCSAS_ALLOW_PYTHON_TIER=0 to forbid it.
REM ====================================================================

setlocal enabledelayedexpansion

REM ----- Auto-discover recovery root from this script's location ----
set "SCRIPT_DIR=%~dp0"
set "RECOVERY="
if exist "%SCRIPT_DIR%..\bin" set "RECOVERY=%SCRIPT_DIR%.."
if exist "%SCRIPT_DIR%recovery\bin" set "RECOVERY=%SCRIPT_DIR%recovery"
if "%RECOVERY%"=="" set "RECOVERY=%SCRIPT_DIR%"

REM Normalise (remove trailing backslash).
if "%RECOVERY:~-1%"=="\" set "RECOVERY=%RECOVERY:~0,-1%"

REM ----- Detect architecture ----------------------------------------
set "ARCH="
if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64"   set "ARCH=x86_64-windows"
if /i "%PROCESSOR_ARCHITECTURE%"=="x86"     set "ARCH=x86_64-windows"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64"   set "ARCH=aarch64-windows"
if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64"   set "ARCH=x86_64-windows"

if "%ARCH%"=="" (
    echo ERROR: unsupported processor architecture: %PROCESSOR_ARCHITECTURE%
    pause
    exit /b 1
)

REM ----- Find the restic repo ---------------------------------------
set "REPO="
if exist "%RECOVERY%\repo\keys" if exist "%RECOVERY%\repo\index" set "REPO=%RECOVERY%\repo"
if "%REPO%"=="" if exist "%RECOVERY%\keys" if exist "%RECOVERY%\index" set "REPO=%RECOVERY%"

if "%REPO%"=="" (
    echo ERROR: no restic repo (keys\ + index\) found under %RECOVERY%
    pause
    exit /b 1
)

REM ----- Ask for target directory -----------------------------------
set "DEFAULT_TARGET=%USERPROFILE%\Documents\restored"
echo.
echo ============================================================
echo                LCSAS Recovery (Windows)
echo ============================================================
echo.
echo Recovery root: %RECOVERY%
echo Repo:          %REPO%
echo Architecture:  %ARCH%
echo.
set /p "TARGET=Restore to which folder? [%DEFAULT_TARGET%]: "
if "%TARGET%"=="" set "TARGET=%DEFAULT_TARGET%"

if not exist "%TARGET%" mkdir "%TARGET%" 2>nul
if not exist "%TARGET%" (
    echo ERROR: could not create %TARGET%
    pause
    exit /b 1
)

REM ----- Password prompt --------------------------------------------
REM CMD has no `read -s` equivalent, so the password is visible while
REM typing.  For privacy, run from PowerShell with Read-Host -AsSecure
REM (documented in RECOVER_WINDOWS.txt).
echo.
set /p "LCSAS_PW=Password: "
if "%LCSAS_PW%"=="" (
    echo ERROR: empty password
    pause
    exit /b 1
)

REM Write password to a transient temp file (deleted on exit).
set "PWFILE=%TEMP%\lcsas-pw-%RANDOM%-%TIME:~6,2%%TIME:~9,2%.txt"
> "%PWFILE%" echo !LCSAS_PW!
set "LCSAS_PW="

REM Ensure the temp file is cleaned up no matter how we exit.
REM (CMD has no real trap; we delete after each branch.)

REM ----- Tier 1: prebuilt lcsas-restore.exe -------------------------
set "BIN=%RECOVERY%\bin\%ARCH%\lcsas-restore.exe"
if exist "%BIN%" (
    echo.
    echo [tier 1] running %BIN%
    "%BIN%" --repo "%REPO%" --password-file "%PWFILE%" --target "%TARGET%" --snapshot latest
    set "RC=!ERRORLEVEL!"
    del "%PWFILE%" 2>nul
    if !RC! equ 0 (
        echo.
        echo ============================================================
        echo  Recovery complete.  Files restored to: %TARGET%
        echo ============================================================
        pause
        exit /b 0
    )
    echo [tier 1] failed with exit code !RC!; trying tier 2...
)

REM ----- Tier 2: vendored rustic-static.exe -------------------------
set "BIN=%RECOVERY%\bin\%ARCH%\rustic-static.exe"
if exist "%BIN%" (
    echo.
    echo [tier 2] running %BIN%
    "%BIN%" --repository "%REPO%" --password-file "%PWFILE%" restore latest "%TARGET%"
    set "RC=!ERRORLEVEL!"
    del "%PWFILE%" 2>nul
    if !RC! equ 0 (
        echo.
        echo ============================================================
        echo  Recovery complete.  Files restored to: %TARGET%
        echo ============================================================
        pause
        exit /b 0
    )
    echo [tier 2] failed with exit code !RC!; trying tier 5...
)

REM ----- Tier 5: Python fallback (optional) -------------------------
if /i "%LCSAS_ALLOW_PYTHON_TIER%"=="0" goto :no_python

where py >nul 2>nul
if errorlevel 1 goto :no_python

set "PYREST="
if exist "%RECOVERY%\standalone_restorer.py" set "PYREST=%RECOVERY%\standalone_restorer.py"
if "%PYREST%"=="" if exist "%RECOVERY%\..\standalone_restorer.py" set "PYREST=%RECOVERY%\..\standalone_restorer.py"
if "%PYREST%"=="" goto :no_python

echo.
echo [tier 5] falling back to py %PYREST%
py "%PYREST%" "%REPO%" "%TARGET%" --password-file "%PWFILE%"
set "RC=!ERRORLEVEL!"
del "%PWFILE%" 2>nul
if !RC! equ 0 (
    echo.
    echo ============================================================
    echo  Recovery complete (via Python fallback).
    echo  Files restored to: %TARGET%
    echo ============================================================
    pause
    exit /b 0
)

:no_python
del "%PWFILE%" 2>nul
echo.
echo ============================================================
echo  ERROR: no working recovery method on this system.
echo.
echo  Looked for:
echo    %RECOVERY%\bin\%ARCH%\lcsas-restore.exe
echo    %RECOVERY%\bin\%ARCH%\rustic-static.exe
echo    Python (py) + standalone_restorer.py
echo.
echo  See %RECOVERY%\docs\RECOVER_WINDOWS.txt for manual options.
echo ============================================================
pause
exit /b 1
