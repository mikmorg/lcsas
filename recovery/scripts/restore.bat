@echo off
REM ====================================================================
REM restore.bat -- LCSAS recovery driver for Windows.
REM
REM Run this file by double-clicking it in File Explorer, or by typing
REM `restore.bat` from a CMD or PowerShell prompt at the disc root.
REM
REM This is the Windows equivalent of recovery/scripts/restore.sh.  The
REM tier order is:
REM
REM   Tier 1.  bin\<arch>\lcsas-restore.exe   (prebuilt, static)
REM   Tier 2.  bin\<arch>\rustic-static.exe   (vendored cross-check)
REM
REM If both tiers are missing or fail, the script exits non-zero with a
REM clear error including a manual-recovery hint.  The pure-Python
REM standalone restorer ships on the disc but is NOT orchestrated from
REM this .bat (it would depend on a Python install that is not
REM guaranteed on headless-recovery Windows hosts); see
REM recovery/docs/RECOVER_WINDOWS.txt for the manual invocation.
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

REM ----- Detect target (arch + OS) ----------------------------------
REM
REM Phase 21.1 aligned the bundled-binary path to a single rust-style
REM target triple: bin\x86_64-pc-windows-gnu\.  The variable is named
REM ARCH for historical reasons; treat it as the target triple.
REM
REM Override with LCSAS_TARGET if auto-detection misfires (e.g. running
REM under an x86 emulator on Windows ARM64).
set "ARCH="
if defined LCSAS_TARGET (
    set "ARCH=%LCSAS_TARGET%"
) else (
    if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64"   set "ARCH=x86_64-pc-windows-gnu"
    if /i "%PROCESSOR_ARCHITECTURE%"=="x86"     set "ARCH=x86_64-pc-windows-gnu"
    if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64"   set "ARCH=x86_64-pc-windows-gnu"
    if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" (
        echo ERROR: Windows ARM64 is not yet supported in the bundled toolchain.
        echo Reason: upstream rustic does not ship aarch64-pc-windows-msvc.
        echo Workaround: install rustic via winget or build from source, then run
        echo  rustic.exe --repository "%RECOVERY%\repo" --password-file PWFILE ^
        echo                restore latest TARGETDIR
        pause
        exit /b 1
    )
)

if "%ARCH%"=="" (
    echo ERROR: unsupported processor architecture: %PROCESSOR_ARCHITECTURE%
    echo Supported: AMD64 / x86 ^(both map to x86_64-pc-windows-gnu^)
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
REM The meta-disc deliberately carries NO catalog.db (it would always
REM be stale at burn time).  Scan every drive letter for catalog.db,
REM keep whichever was most recently modified.
set "CATALOG_ARG="
set "CATALOG_PICK="
if exist "%RECOVERY%\catalog.db" set "CATALOG_PICK=%RECOVERY%\catalog.db"
if exist "%REPO%\catalog.db"     set "CATALOG_PICK=%REPO%\catalog.db"
for %%L in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    if exist "%%L:\catalog.db" (
        set "CATALOG_PICK=%%L:\catalog.db"
    )
)
if defined CATALOG_PICK (
    set "CATALOG_ARG=--catalog %CATALOG_PICK%"
    echo [lcsas-restore] using catalog %CATALOG_PICK%
)

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
    echo [tier 2] failed with exit code !RC!.
)

del "%PWFILE%" 2>nul
echo.
echo ============================================================
echo  ERROR: no working recovery method on this system.
echo.
echo  Looked for:
echo    %RECOVERY%\bin\%ARCH%\lcsas-restore.exe
echo    %RECOVERY%\bin\%ARCH%\rustic-static.exe
echo.
echo  A manual recovery path using the on-disc standalone restorer
echo  (requires a Python 3 install on this host) is described in
echo  %RECOVERY%\docs\RECOVER_WINDOWS.txt -- this .bat does not
echo  launch it for you.
echo ============================================================
pause
exit /b 1
