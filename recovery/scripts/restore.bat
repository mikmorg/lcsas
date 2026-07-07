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
REM
REM Split-key archives: if KEY_INFO.txt says the password is split
REM into share cards, rebuild it FIRST with the bundled combiner
REM bin\<arch>\lcsas-keyshare.exe -- see recovery/docs/
REM RECOVER_WINDOWS.txt, "KEY SHARES (SPLIT PASSWORDS)".
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

REM Carry the holographic repo metadata (keys/index/config/snapshots
REM under metadata\<tenant>\) into RAM too.  Without this, the relocated
REM copy's %RECOVERY%\metadata\* probe finds nothing once the meta disc
REM is ejected and discovery fails -- defeating the whole point of
REM relocating.  metadata\ is small (KBs-low MBs), safe for %TEMP%.
REM Two source forms: ..\..\metadata when run from recovery\scripts\,
REM metadata when surfaced at the disc root.
set "META_SRC="
if exist "%~dp0..\..\metadata\" for %%I in ("%~dp0..\..\metadata") do set "META_SRC=%%~fI"
if not defined META_SRC if exist "%~dp0metadata\" for %%I in ("%~dp0metadata") do set "META_SRC=%%~fI"
if defined META_SRC (
    where robocopy >nul 2>nul
    if not errorlevel 1 (
        robocopy "!META_SRC!" "%RAMDIR%\recovery\metadata" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul
    ) else (
        xcopy /E /I /Y /Q "!META_SRC!" "%RAMDIR%\recovery\metadata\" >nul 2>nul
    )
)

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

REM ----- --check-disc: scratched-disc repair path [FMT-01] ----------
REM The Windows equivalent of restore.sh --check-disc.  Runs the bundled
REM in-house RS03 ECC tool (lcsas-ecc.exe) on a disc image to verify and,
REM on damage, offer/perform an in-place repair using the burned parity.
REM No externally installed dvdisaster is required.  Usage:
REM     restore.bat --check-disc C:\path\to\disc.iso
if /i "%~1"=="--check-disc" (
    set "CHECK_IMG=%~2"
    if "!CHECK_IMG!"=="" (
        echo ERROR: --check-disc requires an IMAGE argument.
        pause
        exit /b 3
    )
    if not exist "!CHECK_IMG!" (
        echo ERROR: --check-disc: image not found: !CHECK_IMG!
        pause
        exit /b 3
    )
    set "ECC_BIN=%RECOVERY%\bin\%ARCH%\lcsas-ecc.exe"
    if not exist "!ECC_BIN!" (
        echo ERROR: no lcsas-ecc.exe under %RECOVERY%\bin\%ARCH%\ --
        echo this meta disc cannot repair discs.  Try the newest META disc.
        pause
        exit /b 3
    )
    echo [check-disc] scanning !CHECK_IMG! for damage with !ECC_BIN!
    "!ECC_BIN!" verify "!CHECK_IMG!"
    set "VRC=!ERRORLEVEL!"
    if !VRC! equ 0 (
        echo [check-disc] no damage found; disc image is intact.
        pause
        exit /b 0
    )
    if !VRC! equ 2 (
        echo [check-disc] this image has no RS03 ECC parity layer; nothing to repair.
        pause
        exit /b 2
    )
    if not !VRC! equ 1 (
        echo [check-disc] could not read the image ^(exit !VRC!^).
        pause
        exit /b 3
    )
    echo [check-disc] DAMAGE DETECTED in !CHECK_IMG!.
    set "DOFIX="
    if /i "%LCSAS_CHECK_DISC_AUTOFIX%"=="1" (
        set "DOFIX=1"
    ) else (
        set /p "FIX_ANS=Repair it now using the burned ECC parity? [y/N]: "
        if /i "!FIX_ANS!"=="y"   set "DOFIX=1"
        if /i "!FIX_ANS!"=="yes" set "DOFIX=1"
    )
    if not "!DOFIX!"=="1" (
        echo [check-disc] left unrepaired at your request.
        pause
        exit /b 1
    )
    echo [check-disc] repairing in place with !ECC_BIN! ...
    "!ECC_BIN!" fix "!CHECK_IMG!"
    set "FRC=!ERRORLEVEL!"
    if !FRC! equ 0 (
        echo [check-disc] repair succeeded; !CHECK_IMG! is now intact.
        pause
        exit /b 0
    )
    if !FRC! equ 1 (
        echo [check-disc] damage exceeded the available ECC parity; some
        echo sectors are uncorrectable.  Try another copy of this disc.
        pause
        exit /b 1
    )
    echo [check-disc] repair failed ^(exit !FRC!^).
    pause
    exit /b 3
)

REM ----- Find the restic repo ---------------------------------------
REM A restic repo is any directory holding both keys\ and index\.  The
REM holographic layout LCSAS burns puts a per-tenant repo under
REM metadata\<tenant>\ on EVERY disc (meta and data).  restore.bat is
REM surfaced at the disc root, so %RECOVERY% is <disc>\recovery and the
REM repo material is one level up at <disc>\metadata\<tenant>\ and on any
REM other mounted disc at <drive>:\metadata\<tenant>\.  This mirrors
REM restore.sh's candidate model (legacy direct layouts first, then the
REM holographic layout on this volume, then every other mounted disc).
REM Canonicalise DISC_ROOT (drop the trailing "\.." so later globs and
REM error text are clean, and so interpreters that choke on "..\" path
REM segments still enumerate metadata\).
for %%I in ("%RECOVERY%\..") do set "DISC_ROOT=%%~fI"
set "REPO="
set "NCAND=0"

REM add_candidate <path>: if <path>\keys and <path>\index both exist and
REM its tenant name (last path component) is new, record it.
REM Implemented inline via the :add_cand label because CMD has no funcs.

REM -- Legacy direct layouts (cheap; keep first for old discs).
call :add_cand "%RECOVERY%\repo"
call :add_cand "%RECOVERY%"
REM -- Holographic layout on this volume (disc root and relocated RAM dir).
REM    `dir /ad /b` enumerates the tenant subdirs; it is portable across
REM    CMD interpreters where `for /d` globbing is unreliable.
call :scan_metadata "%DISC_ROOT%\metadata"
call :scan_metadata "%RECOVERY%\metadata"
REM -- Holographic layout on every other mounted disc (data discs carry
REM    metadata\<tenant>\ too).
for %%L in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    call :scan_metadata "%%L:\metadata"
)

if !NCAND! equ 0 (
    echo ERROR: could not find an LCSAS backup set ^(a folder containing keys\ and index\^).
    echo Looked in:
    echo    %RECOVERY%\repo
    echo    %RECOVERY%
    echo    %DISC_ROOT%\metadata\^<name^>\
    echo    D:\metadata\^<name^>\ .. Z:\metadata\^<name^>\
    echo.
    echo Insert the disc labelled LCSAS_META ^(or any LCSAS data disc^), wait
    echo for it to appear in File Explorer, then run restore.bat again.
    pause
    exit /b 1
)

REM -- Select the tenant.  (goto-based; labels must NOT live inside a
REM parenthesised block, so the multi-candidate prompt is its own loop.)
REM See the :add_cand portability note re: %%NCAND%% / `call set`.
set "REPO="
if defined LCSAS_REPO (
    for /l %%I in (1,1,%NCAND%) do (
        call set "_NM=%%CAND_NAME_%%I%%"
        if /i "!_NM!"=="%LCSAS_REPO%" call set "REPO=%%CAND_%%I%%"
    )
    if not defined REPO (
        echo ERROR: LCSAS_REPO=%LCSAS_REPO% is not among the backup sets on this archive:
        for /l %%I in (1,1,%NCAND%) do call echo    %%CAND_NAME_%%I%%
        pause
        exit /b 1
    )
    goto :repo_selected
)
if %NCAND% equ 1 (
    set "REPO=!CAND_1!"
    goto :repo_selected
)

echo.
echo This archive contains more than one backup set:
for /l %%I in (1,1,%NCAND%) do call echo    [%%I] %%CAND_NAME_%%I%%
echo.
:pick_repo
set "REPO_PICK="
set /p "REPO_PICK=Which one do you want to restore? [1]: "
if "!REPO_PICK!"=="" set "REPO_PICK=1"
set "REPO="
for /l %%I in (1,1,%NCAND%) do (
    if "!REPO_PICK!"=="%%I" call set "REPO=%%CAND_%%I%%"
)
if not defined REPO (
    echo Please enter a number between 1 and %NCAND%.
    goto :pick_repo
)

goto :repo_selected

:add_cand
REM %~1 = candidate dir.  Validity: keys\ and index\ both present.
REM De-dup by tenant name (last path component); first hit wins so the
REM meta-disc / earlier-scanned copy is preferred.
REM
REM Portability note: the for-range uses %%NCAND%% (plain expansion, set
REM before the loop) and dereferences CAND_NAME_<n> via `call set`.  Both
REM avoid the `!VAR!`-in-for-range and `!CAND_%%I!`-nested-deref forms,
REM which real CMD honours but wine's cmd does NOT (the documented
REM delayed-expansion divergence) -- this keeps the same code working
REM under the wine smoke and the real-Windows e2e gate.
if not exist "%~1\keys" goto :eof
if not exist "%~1\index" goto :eof
set "_CN=%~nx1"
for /l %%I in (1,1,%NCAND%) do (
    call set "_EXIST=%%CAND_NAME_%%I%%"
    if /i "!_EXIST!"=="!_CN!" goto :eof
)
set /a NCAND+=1
call set "CAND_%%NCAND%%=%~f1"
call set "CAND_NAME_%%NCAND%%=!_CN!"
goto :eof

:scan_metadata
REM %~1 = a "...\metadata" dir.  Enumerate its immediate subdirs (one per
REM tenant) and offer each as a candidate.  `dir /ad /b` is used instead
REM of `for /d` globbing because the latter is unreliable across CMD
REM interpreters (notably wine), whereas `dir /ad /b` lists bare names
REM consistently.  Absent dir -> dir errors -> nothing enumerated.
if not exist "%~1\" goto :eof
for /f "delims=" %%T in ('dir /ad /b "%~1" 2^>nul') do call :add_cand "%~1\%%T"
goto :eof

:repo_selected

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

REM ----- Non-empty-target guard [UX-07] -----------------------------
REM Warn BEFORE the password prompt when %TARGET% already holds the
REM heir's OWN files (same names would silently overwrite live data).
REM Re-running into a prior LCSAS restore is the supported resume from
REM an interrupted run, so we skip the prompt when the hidden marker
REM file .lcsas-restore-marker is present (and when
REM LCSAS_FORCE_NONEMPTY_TARGET=1).  The marker is written after
REM confirmation/creation so subsequent re-runs are silent.
set "MARKER=%TARGET%\.lcsas-restore-marker"
if exist "%TARGET%\" if not exist "%MARKER%" if not "%LCSAS_FORCE_NONEMPTY_TARGET%"=="1" (
    dir /b "%TARGET%" 2>nul | findstr "." >nul
    if not errorlevel 1 (
        echo.
        echo ==========================================================
        echo   WARNING: the folder
        echo       %TARGET%
        echo   already contains files that do not look like a previous
        echo   LCSAS restore.  Restored files with the same names will
        echo   OVERWRITE what is there now.
        echo.
        echo   Safest choice: stop, and restore into a NEW, empty
        echo   folder instead.
        echo ==========================================================
        set /p "OVERWRITE_ANS=Type YES to overwrite, anything else to stop: "
        if /i not "!OVERWRITE_ANS!"=="YES" (
            echo Aborting; nothing was written.
            pause
            exit /b 65
        )
    )
)

if not exist "%TARGET%" mkdir "%TARGET%" 2>nul
if not exist "%TARGET%" (
    echo ERROR: could not create %TARGET%
    pause
    exit /b 1
)
REM Mark this folder so a later resume into it does not re-prompt.
REM +h matches restore.sh's dot-file convention and the docs' "hidden
REM zero-byte marker" -- on Windows a leading dot alone hides nothing,
REM and an unhidden marker reads as restored data to any "did the failed
REM run leave files behind?" check -- issue #384 / windows-e2e.
REM Create-if-absent: cmd cannot `>`-overwrite a file that already has
REM the hidden attribute (access denied), so a resume run must not try.
if not exist "%MARKER%" > "%MARKER%" echo. 2>nul
REM Hide unconditionally (idempotent) so a marker written UNHIDDEN by an
REM older restore.bat is retro-hidden on resume -- else it lingers as a
REM visible file and reads as restored data to a "did the run leave
REM files behind?" check (issue #384 / windows-e2e).
attrib +h "%MARKER%" 2>nul

REM ----- Password prompt --------------------------------------------
REM CMD has no `read -s` equivalent, so the password is visible while
REM typing.  For privacy, run from PowerShell with Read-Host -AsSecure
REM (documented in RECOVER_WINDOWS.txt).
echo.
echo If KEY_INFO.txt says the password is SPLIT into share cards,
echo rebuild it first with bin\%ARCH%\lcsas-keyshare.exe -- see
echo recovery\docs\RECOVER_WINDOWS.txt, "KEY SHARES (SPLIT PASSWORDS)".
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
    REM %PWFILE% is deleted only on the TERMINAL branches below -- the
    REM fall-through to tier 2 needs it alive or rustic-static.exe gets
    REM a --password-file pointing at nothing and can never succeed.
    REM Tier 2 and the no-tier epilogue each do their own del.
    if !RC! equ 0 (
        del "%PWFILE%" 2>nul
        echo.
        echo ============================================================
        echo  Recovery complete.  Files restored to: %TARGET%
        echo ============================================================
        pause
        exit /b 0
    )
    if !RC! equ 77 (
        REM A key file POSITIVELY rejected the password -- issue #384.
        REM Every tier reads the SAME keys with the SAME password, so
        REM tier 2 would only fail identically -- and leave a partial
        REM tree behind first.  Stop here with a clear message instead
        REM of falling through.  NB: no parens in these comments -- an
        REM unescaped ^) inside a REM line in a parenthesized block
        REM ends the block early in cmd.
        del "%PWFILE%" 2>nul
        echo.
        echo ============================================================
        echo  ERROR: could not decrypt the repository with that password.
        echo  Check the password ^(and any split-key shares^) and re-run.
        echo ============================================================
        pause
        exit /b 77
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
