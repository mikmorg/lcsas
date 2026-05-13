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

REM ----- Single-drive guard: relocate to RAM before continuing ------
REM
REM If this .bat is being interpreted from a read-only optical disc,
REM cmd.exe holds the file open and the user cannot eject -- fatal
REM for anybody with only ONE optical drive.  Detect that case and
REM re-launch ourselves from %TEMP% after copying the script + binary
REM tree to a writable location.  Subsequent disc swaps then work.
REM
REM LCSAS_RELOCATED is the sentinel: when set, we are the in-RAM
REM copy and its value is the original meta-disc drive letter.

if defined LCSAS_RELOCATED goto :after_relocate
if /i "%LCSAS_NO_RELOCATE%"=="1" goto :after_relocate

REM Probe writability of %~dp0 by trying to create a temp file there.
set "PROBE=%~dp0lcsas-probe-%RANDOM%.tmp"
> "%PROBE%" echo. 2>nul
if exist "%PROBE%" (
    del "%PROBE%" 2>nul
    goto :after_relocate
)

REM Pick a relocation directory under %TEMP%.
set "RAMDIR=%TEMP%\lcsas-restore-%RANDOM%-%RANDOM%"
mkdir "%RAMDIR%"     2>nul
mkdir "%RAMDIR%\recovery"        2>nul
mkdir "%RAMDIR%\recovery\scripts" 2>nul
mkdir "%RAMDIR%\recovery\bin"     2>nul
if not exist "%RAMDIR%\recovery\scripts\" (
    echo [lcsas-restore] cannot create %RAMDIR% -- staying on disc.
    goto :after_relocate
)

REM Mirror the on-disc layout in %RAMDIR%.  robocopy is preferred for
REM long paths; fall back to xcopy.
copy /Y "%~f0" "%RAMDIR%\recovery\scripts\restore.bat" >nul
where robocopy >nul 2>nul
if not errorlevel 1 (
    robocopy "%~dp0..\bin" "%RAMDIR%\recovery\bin" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul
) else (
    xcopy /E /I /Y /Q "%~dp0..\bin"  "%RAMDIR%\recovery\bin\"  >nul 2>nul
)
if exist "%~dp0..\catalog.db" copy /Y "%~dp0..\catalog.db" "%RAMDIR%\recovery\catalog.db" >nul

echo [lcsas-restore] copied recovery files to %RAMDIR%
echo [lcsas-restore] you may eject the recovery disc when the binary
echo                 prompts for a data disc.

REM Capture the disc's drive letter (everything up to and including ":\").
set "LCSAS_RELOCATED=%~d0\"

REM cd out of the disc so the new cmd does not inherit it.
cd /d %SystemDrive%\ >nul 2>nul

REM Re-launch the relocated script.  /C exits cmd when done.
call "%RAMDIR%\recovery\scripts\restore.bat" %*
exit /b %ERRORLEVEL%

:after_relocate
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

REM ----- Auto-discover other mounted discs ---------------------------
REM When packs are split across multiple LCSAS volumes, the user may
REM have several discs mounted simultaneously.  Scan drive letters D-Z
REM and pass each one that contains a data\ subdir as --pack-search.
REM Skip the meta-disc drive (LCSAS_RELOCATED) so single-drive
REM recovery can free the drive for data discs.

set "PACK_SEARCH_ARGS="
for %%L in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    set "SKIP_L="
    if defined LCSAS_RELOCATED if /i "%%L:\"=="%LCSAS_RELOCATED%" set "SKIP_L=1"
    if not defined SKIP_L (
        if exist "%%L:\data\" (
            if /i not "%%L:\"=="%RECOVERY%" set "PACK_SEARCH_ARGS=!PACK_SEARCH_ARGS! --pack-search %%L:\"
        )
        if exist "%%L:\repo\data\" (
            if /i not "%%L:\repo"=="%REPO%" set "PACK_SEARCH_ARGS=!PACK_SEARCH_ARGS! --pack-search %%L:\repo"
        )
    )
)

REM Pass --meta-disc through so lcsas-restore.exe excludes the disc
REM from its own search list and drops cwd out of it before prompts.
set "META_DISC_ARG="
if defined LCSAS_RELOCATED set "META_DISC_ARG=--meta-disc %LCSAS_RELOCATED%"

REM Optional --catalog if a catalog.db is present.
set "CATALOG_ARG="
if exist "%RECOVERY%\catalog.db" set "CATALOG_ARG=--catalog %RECOVERY%\catalog.db"
if exist "%REPO%\catalog.db"     set "CATALOG_ARG=--catalog %REPO%\catalog.db"

REM ----- Tier 1: prebuilt lcsas-restore.exe -------------------------
set "BIN=%RECOVERY%\bin\%ARCH%\lcsas-restore.exe"
if exist "%BIN%" (
    echo.
    echo [tier 1] running %BIN%
    "%BIN%" --repo "%REPO%" --password-file "%PWFILE%" --target "%TARGET%" --snapshot latest %PACK_SEARCH_ARGS% %CATALOG_ARG% %META_DISC_ARG%
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
