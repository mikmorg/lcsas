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
REM Tier 2 runs only for a backup set whose folder holds its own data\
REM (a set copied whole to one folder).  A set read from the discs keeps
REM its packs on the data discs, which only tier 1 can ask for one at a
REM time, so when tier 1 stops there the script stops too: it keeps what
REM is restored and says how to carry on (#615).
REM
REM Run from a disc, the script first copies what this computer needs
REM into the tools folder, %LOCALAPPDATA%\LCSAS-restore (LCSAS_TOOLS_DIR
REM names another), and runs that copy, so the META disc can come out
REM of the drive.  The folder stays until the restore has finished.  A
REM restore that stopped carries on from it, with no META disc, through
REM "Resume LCSAS restore.bat" in that folder and on the Desktop (#609).
REM
REM If both tiers are missing or fail, the script exits non-zero with a
REM clear error including a manual-restore hint.  The tier-3 Python
REM reader ships on the disc but is NOT orchestrated from
REM this .bat (it would depend on a Python install that is not
REM guaranteed on a Windows host used for a restore); see
REM recovery/docs/RECOVER_WINDOWS.txt for the manual invocation.
REM
REM Split-password archives: if PASSWORD_INFO.txt says the password is split
REM into share cards, rebuild it FIRST with the bundled combiner
REM bin\<arch>\lcsas-keyshare.exe -- see recovery/docs/
REM RECOVER_WINDOWS.txt, "SHARE CARDS (SPLIT PASSWORDS)".
REM ====================================================================

REM setlocal, taking LCSAS_RELOCATED (set by the copy on the disc when it
REM relaunches this one, see the end of the guard below) into the local
REM scope and out of the caller's environment.  The relaunch runs after
REM the copy on the disc has ended, so it sets the variable in the caller's
REM own environment; without this a later run from the same prompt
REM would think it had already been relocated.
for %%R in ("%LCSAS_RELOCATED%") do (set "LCSAS_RELOCATED=" & setlocal enabledelayedexpansion & set "LCSAS_RELOCATED=%%~R")

REM ----- A click in this window pauses the restore (#613) ------------
REM Windows 10's console has QuickEdit on: a click or a tap in the
REM window starts selecting text, and while it selects, the console
REM stops whatever writes to it, so the restore looks hung.  The script
REM does not change the reader's console settings to turn it off; it
REM says so first, before the copy.  The copy that runs after the
REM relocation does not say it again.
if not defined LCSAS_RELOCATED (
    echo.
    echo  NOTE: do not click inside this window while the restore runs.
    echo  A click selects text and PAUSES the restore: the window title
    echo  then starts with "Select".  If that happens, press Esc or
    echo  right-click inside the window, and the restore carries on.
    echo.
)

REM --resume: "Resume LCSAS restore.bat" runs the copy in the tools
REM folder this way, to carry on a restore that stopped (#609).
set "RESUME="
if /i "%~1"=="--resume" set "RESUME=1"

REM ----- Detect target (arch + OS) ----------------------------------
REM
REM Phase 21.1 aligned the bundled-binary path to a single rust-style
REM target triple: bin\x86_64-pc-windows-gnu\.  The variable is named
REM ARCH for historical reasons; treat it as the target triple.  It is
REM known before the relocation, which copies only this triple (#612).
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
        echo  rustic.exe --repository "D:\metadata\<set>" --password-file PWFILE
        echo             restore latest TARGETDIR
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

REM ----- The tools folder (#609) -------------------------------------
REM The copy of this script that does the restore lives there, with this
REM computer's programs, the backup sets' metadata, the pack cache, a
REM copy of the catalog, resume.cfg and "Resume LCSAS restore.bat".
REM That copy is TOOLS\recovery\scripts\restore.bat, so it finds TOOLS
REM two folders up.  Otherwise TOOLS is LCSAS_TOOLS_DIR, else
REM %LOCALAPPDATA%\LCSAS-restore: not %TEMP%, which Disk Cleanup and
REM Storage Sense empty.  A run that does not relocate keeps only its
REM pack cache and its catalog copy there.
if defined LCSAS_RELOCATED goto :tools_from_script
if defined RESUME goto :tools_from_script
set "TOOLS=%USERPROFILE%\AppData\Local\LCSAS-restore"
if defined LOCALAPPDATA set "TOOLS=%LOCALAPPDATA%\LCSAS-restore"
if defined LCSAS_TOOLS_DIR set "TOOLS=%LCSAS_TOOLS_DIR%"
goto :tools_known
:tools_from_script
set "TOOLS=%~dp0..\.."
:tools_known
for %%I in ("%TOOLS%") do set "TOOLS=%%~fI"
if "%TOOLS:~-1%"=="\" set "TOOLS=%TOOLS:~0,-1%"
REM .done means "the restore has finished: delete this folder", and is
REM written only after a good restore.  One left over by a run whose
REM delete failed must not delete the folder after a stop this time.
del /f /q "%TOOLS%\.done" >nul 2>nul

REM A restore that stopped: take the backup set, the folder and the
REM drive letters from resume.cfg, and the META disc's letter as the
REM relocation sets it.  The password is asked again: it is never kept.
if not defined RESUME goto :resume_loaded
call :read_resume
if not defined RS_set (
    echo ERROR: there is no stopped restore to carry on in
    echo    !TOOLS!
    echo To restore, put the META disc in and run restore.bat from it.
    pause
    exit /b 1
)
set "LCSAS_RELOCATED=!RS_meta!"
set "LCSAS_REPO=!RS_set!"
set "RESUME_TARGET=!RS_target!"
set "RESUME_LETTERS=!RS_letters!"
:resume_loaded

REM ----- Single-drive guard: copy the tools off the disc ------------
REM
REM If this .bat is being interpreted from a read-only optical disc,
REM cmd.exe holds the file open and the heir cannot eject -- fatal
REM for anybody with only ONE optical drive.  Detect that case, copy
REM what the restore needs to the tools folder, and re-launch from
REM there.  Subsequent disc swaps then work.
REM
REM LCSAS_RELOCATED is the sentinel: when set, we are the copy in the
REM tools folder and its value is the original meta disc drive letter.

if defined LCSAS_RELOCATED goto :after_relocate
if /i "%LCSAS_NO_RELOCATE%"=="1" goto :after_relocate
REM --check-disc reads an image file, not the disc in the drive: it runs
REM from the disc and leaves no tools folder behind.
if /i "%~1"=="--check-disc" goto :after_relocate

REM Probe writability of %~dp0 by trying to create a temp file there.
REM The 2>nul comes FIRST: cmd itself reports a redirect it cannot open,
REM so with the 2>nul at the end every start from a disc printed
REM "Access is denied." (#611).
set "PROBE=%~dp0lcsas-probe-%RANDOM%.tmp"
2>nul > "%PROBE%" echo.
if exist "%PROBE%" (
    del "%PROBE%" 2>nul
    goto :after_relocate
)

REM Find the recovery root on the disc the way :after_relocate does,
REM for both places this script lives: recovery\scripts\restore.bat
REM (the root is ..) and the copy at the disc root, E:\restore.bat (the
REM root is recovery\).  Copying "%~dp0..\bin" was right only for the
REM first: from E:\ it named E:\bin, the RAM copy got no binaries, and
REM the restore.bat an heir double-clicks ended in "no working recovery
REM method" (#531).
set "SRC_ROOT="
if exist "%~dp0..\bin\" for %%I in ("%~dp0..") do set "SRC_ROOT=%%~fI"
if exist "%~dp0recovery\bin\" for %%I in ("%~dp0recovery") do set "SRC_ROOT=%%~fI"

REM The repository metadata (keys/index/config/snapshots under
REM metadata\<repo>\) goes too.  Without it the copy finds no backup
REM set once the meta disc is ejected -- defeating the whole point of
REM relocating.  metadata\ is small (KBs-low MBs).  Two source forms:
REM ..\..\metadata when run from recovery\scripts\, metadata when
REM surfaced at the disc root.
set "META_SRC="
if exist "%~dp0..\..\metadata\" for %%I in ("%~dp0..\..\metadata") do set "META_SRC=%%~fI"
if not defined META_SRC if exist "%~dp0metadata\" for %%I in ("%~dp0metadata") do set "META_SRC=%%~fI"

REM A folder named in LCSAS_TOOLS_DIR that already holds other files is
REM left alone: the tools folder is deleted once the restore is done.
if defined LCSAS_TOOLS_DIR if exist "%TOOLS%\" if not exist "%TOOLS%\recovery\" if not exist "%TOOLS%\pack-cache\" (
    dir /b /a "%TOOLS%" 2>nul | findstr "." >nul
    if not errorlevel 1 goto :tools_taken
)

REM An earlier run left a whole copy of this very script (restore.bat is
REM copied last): run that, with the metadata, catalog copy and pack
REM cache it has.  fc /b compares the bytes.
if not exist "%TOOLS%\recovery\scripts\restore.bat" goto :tools_fresh
if not exist "%TOOLS%\recovery\bin\%ARCH%\lcsas-restore.exe" goto :tools_fresh
fc /b "%~f0" "%TOOLS%\recovery\scripts\restore.bat" >nul 2>nul
if errorlevel 1 goto :tools_fresh
echo [lcsas-restore] using the recovery files already copied to !TOOLS!
goto :relaunch

:tools_fresh
REM Otherwise remove what an earlier copy left there -- one from another
REM version, or one cut short -- by name, as LCSAS_TOOLS_DIR may hold
REM other files, and copy afresh.  Its pack cache stays only when its
REM tier-1 program is the very same as this disc's: the cache is that
REM program's.
if not exist "%TOOLS%\pack-cache\" goto :tools_cleared
set "KEEP_CACHE="
if defined SRC_ROOT if exist "%TOOLS%\recovery\bin\%ARCH%\lcsas-restore.exe" (
    fc /b "!SRC_ROOT!\bin\%ARCH%\lcsas-restore.exe" "%TOOLS%\recovery\bin\%ARCH%\lcsas-restore.exe" >nul 2>nul
    if not errorlevel 1 set "KEEP_CACHE=1"
)
if not defined KEEP_CACHE rmdir /s /q "%TOOLS%\pack-cache" 2>nul
:tools_cleared
if exist "%TOOLS%\recovery\" rmdir /s /q "%TOOLS%\recovery" 2>nul
if exist "%TOOLS%\metadata\" rmdir /s /q "%TOOLS%\metadata" 2>nul
del /f /q "%TOOLS%\catalog.db" "%TOOLS%\resume.cfg" "%TOOLS%\Resume LCSAS restore.bat" >nul 2>nul

mkdir "%TOOLS%\recovery\scripts" 2>nul
mkdir "%TOOLS%\recovery\bin\%ARCH%" 2>nul
mkdir "%TOOLS%\recovery\docs" 2>nul
if not exist "%TOOLS%\recovery\bin\%ARCH%\" (
    echo [lcsas-restore] cannot create !TOOLS! -- staying on disc.
    echo                 Do not eject this disc until the restore has finished.
    goto :after_relocate
)

REM Only what a restore on this computer runs (#612): the three programs
REM in bin\<arch>\, and rustic-static.exe only when a backup set here
REM holds its own data\ (tier 2 cannot run otherwise); this computer's
REM guide; the backup sets' metadata.  Up to rc5 this was the whole bin
REM tree, 744 MB for six kinds of computer, with nothing on screen.
REM copy and xcopy, not robocopy: wine runs them too (its robocopy exits
REM 16 on these flags), so the local tests check the copy.  restore.bat
REM is copied last, so a copy cut short is never taken for a whole one.
set "T2_EXE="
if defined META_SRC for /f "delims=" %%T in ('dir /ad /b "%META_SRC%" 2^>nul') do if exist "%META_SRC%\%%T\data\" set "T2_EXE=rustic-static.exe"
set "COPY_BAD="
echo [lcsas-restore] copying the restore programs for this computer to
echo                 !TOOLS!
echo                 so that the META disc can come out of the drive:
for %%F in (lcsas-restore.exe lcsas-keyshare.exe lcsas-ecc.exe %T2_EXE%) do call :copy_one "bin\%ARCH%\%%F"
call :copy_one "docs\RECOVER_WINDOWS.txt"
if defined META_SRC for /f "delims=" %%T in ('dir /ad /b "%META_SRC%" 2^>nul') do (
    echo                   metadata\%%T
    mkdir "!TOOLS!\metadata\%%T" 2>nul
    xcopy /e /i /q /y "%META_SRC%\%%T" "!TOOLS!\metadata\%%T" >nul 2>nul
    if errorlevel 4 set "COPY_BAD=1"
)
if defined SRC_ROOT if exist "!SRC_ROOT!\catalog.db" copy /Y "!SRC_ROOT!\catalog.db" "!TOOLS!\catalog.db" >nul 2>nul
echo                   recovery\scripts\restore.bat
copy /Y "%~f0" "%TOOLS%\recovery\scripts\restore.bat" >nul 2>nul
if errorlevel 1 set "COPY_BAD=1"

REM A copy with no programs in it, or one cut short, could only fail.
REM Stay on the disc instead: the restore still works, but the disc
REM cannot be ejected until it has finished.  The copy goes, so the
REM next run does not take it for a whole one.
dir /b "%TOOLS%\recovery\bin\%ARCH%\*.exe" >nul 2>nul
if errorlevel 1 set "COPY_BAD=1"
if defined COPY_BAD (
    echo [lcsas-restore] could not copy the recovery programs to !TOOLS! -- staying on disc.
    echo                 Do not eject this disc until the restore has finished.
    rmdir /s /q "!TOOLS!\recovery" 2>nul
    goto :after_relocate
)

echo [lcsas-restore] copied recovery files to !TOOLS!
echo [lcsas-restore] you may eject the meta disc when the binary
echo                 prompts for a data disc.

REM Re-launch the copy in the tools folder, and end THIS one first.
REM While a batch file is running, cmd goes back to it when a `call`
REM returns -- even with `exit /b` on the same line -- and by then the
REM heir has ejected the disc it is on: "The system cannot find the path
REM specified." and exit 1 after a good restore (#532).  `(goto) 2>nul`
REM ends this batch at once; the rest of the line, already read and
REM %%-expanded, still runs in the caller's context, and the exit code
REM is the copy's.  Ending the batch also undoes setlocal, which puts
REM the cwd back on the disc (blocking Explorer's Eject), so cd off it
REM here, and set LCSAS_RELOCATED (the disc's drive letter, e.g. "E:\")
REM here too.  Measured on windows-latest with every other placement of
REM the exit.
REM
REM When the copy returns, the tools folder goes only if the restore
REM finished: the copy then leaves .done in it (a running batch file
REM cannot delete its own folder).  A restore that stopped keeps the
REM folder, for "Resume LCSAS restore.bat" (#609).  A command after the
REM `call` replaces its exit code (a trailing `set` turned 7 into 0), so
REM keep the code in LCSAS_RC first and end with `cmd /c exit` to hand
REM it back.  `call` expands %%ERRORLEVEL%% after the copy has returned;
REM the child cmd expands %%LCSAS_RC%%.  Not `exit`, which would close
REM the prompt window of an heir who started this from one.
REM
REM wine's cmd does not end a batch at (goto): it prints "Argument
REM missing", skips the rest of the line and goes on with the next one,
REM so under wine the run carries on from the disc, not relocated.  The
REM wine tests check the copy that way; windows-e2e runs this line.
:relaunch
(goto) 2>nul & cd /d %SystemDrive%\ & set "LCSAS_RELOCATED=%~d0\" & call "%TOOLS%\recovery\scripts\restore.bat" %* & call set "LCSAS_RC=%%ERRORLEVEL%%" & (if exist "%TOOLS%\.done" rmdir /s /q "%TOOLS%") 2>nul & cmd /c exit %%LCSAS_RC%%

:after_relocate
REM ----- Auto-discover recovery root from this script's path --------
set "SCRIPT_DIR=%~dp0"
set "RECOVERY="
if exist "%SCRIPT_DIR%..\bin" set "RECOVERY=%SCRIPT_DIR%.."
if exist "%SCRIPT_DIR%recovery\bin" set "RECOVERY=%SCRIPT_DIR%recovery"
if "%RECOVERY%"=="" set "RECOVERY=%SCRIPT_DIR%"

REM Normalise (remove trailing backslash).
if "%RECOVERY:~-1%"=="\" set "RECOVERY=%RECOVERY:~0,-1%"

REM ----- --check-disc: scratched-disc repair path [FMT-01] ----------
REM The Windows equivalent of restore.sh --check-disc.  Runs the bundled
REM in-house RS03 ECC tool (lcsas-ecc.exe) on a disc image to check it and,
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
        echo ERROR: no lcsas-ecc.exe under !RECOVERY!\bin\%ARCH%\ --
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
        echo sectors are uncorrectable.  Try another disc with the same label.
        pause
        exit /b 1
    )
    echo [check-disc] repair failed ^(exit !FRC!^).
    pause
    exit /b 3
)

REM ----- Find the restic repository ---------------------------------
REM A restic repository is any directory holding both keys\ and index\.  The
REM layout LCSAS burns puts each repository's metadata under
REM metadata\<repo>\ on EVERY disc (meta disc and data discs).  restore.bat is
REM surfaced at the disc root, so %RECOVERY% is <disc>\recovery and the
REM repository metadata is one level up at <disc>\metadata\<repo>\ and on any
REM other mounted disc at <drive>:\metadata\<repo>\.  This follows
REM restore.sh's candidate model (legacy direct layouts first, then the
REM metadata\ layout on this disc, then every other mounted disc).
REM In the tools folder, <disc> is TOOLS: its metadata\ is the copy.
REM Canonicalise DISC_ROOT (drop the trailing "\.." so later globs and
REM error text are clean, and so interpreters that choke on "..\" path
REM segments still enumerate metadata\).
for %%I in ("%RECOVERY%\..") do set "DISC_ROOT=%%~fI"
set "REPO="
set "NCAND=0"

REM add_candidate <path>: if <path>\keys and <path>\index both exist and
REM its repository name (last path component) is new, record it.
REM Implemented inline via the :add_cand label because CMD has no funcs.

REM -- Legacy direct layouts (cheap; keep first for old discs).
call :add_cand "%RECOVERY%\repo"
call :add_cand "%RECOVERY%"
REM -- Repository metadata on this disc (disc root and relocated RAM dir).
REM    `dir /ad /b` enumerates the repository subdirs; it is portable across
REM    CMD interpreters where `for /d` globbing is unreliable.
call :scan_metadata "%DISC_ROOT%\metadata"
call :scan_metadata "%RECOVERY%\metadata"
REM -- Repository metadata on every other mounted disc (data discs carry
REM    metadata\<repo>\ too).
for %%L in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    call :scan_metadata "%%L:\metadata"
)

if !NCAND! equ 0 (
    echo ERROR: could not find an LCSAS backup set ^(a folder containing keys\ and index\^).
    echo Looked in:
    echo    !RECOVERY!\repo
    echo    !RECOVERY!
    echo    !DISC_ROOT!\metadata\^<name^>\
    echo    D:\metadata\^<name^>\ .. Z:\metadata\^<name^>\
    echo.
    echo Insert the disc labelled LCSAS_META ^(or any LCSAS data disc^), wait
    echo for it to appear in File Explorer, then run restore.bat again.
    pause
    exit /b 1
)

REM -- Select the repository.  (goto-based; labels must NOT live inside a
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
REM De-dup by repository name (last path component); first hit wins so the
REM meta disc / earlier-scanned copy is preferred.
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
REM repository) and offer each as a candidate.  `dir /ad /b` is used instead
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
echo                 LCSAS Restore (Windows)
echo ============================================================
echo.
echo Recovery root: %RECOVERY%
echo Repo:          %REPO%
echo Architecture:  %ARCH%
echo.
REM A restore that carries on goes into the folder it was going into.
if not defined RESUME goto :ask_target
set "TARGET=!RESUME_TARGET!"
echo Carrying on the restore into:
echo    !TARGET!
goto :target_chosen
REM Clear TARGET first: `set /p` keeps the old value when the answer is
REM empty, so a TARGET left in the caller's environment would silently
REM win over the default (#615).
:ask_target
set "TARGET="
set /p "TARGET=Restore to which folder? [%DEFAULT_TARGET%]: "
REM Drop any quotes typed around the folder, then any trailing backslash.
REM The programs get the folder as --target "<folder>", and a backslash
REM right before the closing quote escapes it: the program then reads the
REM rest of the line as part of the folder name (#615).
if defined TARGET set "TARGET=!TARGET:"=!"
:strip_target
if defined TARGET if "!TARGET:~-1!"=="\" (
    set "TARGET=!TARGET:~0,-1!"
    goto :strip_target
)
if not defined TARGET set "TARGET=%DEFAULT_TARGET%"
REM A whole drive, such as E:\ (E: by now), is refused and the question
REM asked again.  Tier 1 makes the folder, and each folder inside it,
REM with mkdir and accepts only "already exists".  For the top of a
REM drive Windows answers "access denied", so the restore would stop the
REM same way on every run (#615).  Drop this check once tier 1 accepts a
REM folder that is already there.
if "!TARGET:~-1!"==":" (
    echo.
    echo  !TARGET!\ is a whole drive, not a folder.  Choose a folder on
    echo  it instead, for example !TARGET!\restored
    echo.
    goto :ask_target
)
:target_chosen

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
        echo       !TARGET!
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
    echo ERROR: could not create !TARGET!
    pause
    exit /b 1
)
REM Mark this folder so a later resume into it does not re-prompt.
REM Kept intentionally simple: a bare redirect, no `if`/`attrib` around
REM it.  Anything more here sits between the target prompt and the
REM password prompt, and a subtle cmd parse of a conditional redirect
REM can swallow the next piped stdin line -- which silently ate the
REM password on the windows-e2e runner (issue #384).  The marker is a
REM known filename; callers that must ignore it (the windows-e2e
REM negative case, the shell/wine tests) exclude it BY NAME rather than
REM relying on a hidden attribute.  2>nul first, as for the probe: a
REM folder that cannot be written to says so below, not with "Access is
REM denied." here (#611).
2>nul > "%MARKER%" echo.

REM ----- Pack cache and the local copy of the catalog (#610) ---------
REM Both live in the tools folder, TOOLS (see "The tools folder" above).
REM
REM The pack cache: the first time tier 1 finds a pack on a disc, it
REM copies every pack on that disc into this folder, so each disc is
REM asked for about once instead of once per file.  restore.bat makes
REM the folder itself and checks it is there: tier 1 can only make the
REM last folder of the path.  (cmd cannot set a variable to an empty
REM value, so unlike restore.sh there is no "LCSAS_PACK_CACHE_DIR="
REM switch to turn the cache off; set it to another folder instead.)
set "CACHE_OK="
set "CACHE_SET_HERE="
if not defined LCSAS_PACK_CACHE_DIR (
    set "LCSAS_PACK_CACHE_DIR=%TOOLS%\pack-cache"
    set "CACHE_SET_HERE=1"
)
if not exist "%LCSAS_PACK_CACHE_DIR%\" mkdir "%LCSAS_PACK_CACHE_DIR%" 2>nul
if exist "%LCSAS_PACK_CACHE_DIR%\" set "CACHE_OK=1"
if not defined CACHE_OK (
    echo [lcsas-restore] cannot create the pack cache folder !LCSAS_PACK_CACHE_DIR!
    echo                 so a disc may be asked for more than once.
    if defined CACHE_SET_HERE set "LCSAS_PACK_CACHE_DIR="
    set "CACHE_SET_HERE="
)

REM ----- Where tier 1 looks for discs (#604) --------------------------
REM Tier 1 looks for packs in each --pack-search folder.  At each disc
REM prompt it also re-reads each --mount-parent folder, to find the disc
REM just put in and the catalog.db on it.  It goes back to the same drive
REM letters after every swap, so it gets:
REM
REM  * The META disc's own letter (LCSAS_RELOCATED), always.  With ONE
REM    optical drive that is the only drive: every data disc goes into
REM    it.  --meta-disc makes tier 1 skip the letter only while the META
REM    disc is still in it (it checks for recovery\scripts\restore.sh,
REM    #143).  Up to rc5 the letter was left out, so a one-drive restore
REM    could never find a data disc (#604).  The very same string goes
REM    to --meta-disc, --pack-search and --mount-parent: tier 1 compares
REM    them byte for byte, so "d:\" would not match "D:\".
REM  * A restore that carries on: the letters the stopped run searched.
REM    A drive that is empty now is not seen below (`if exist X:\` is
REM    false), and it is where the next disc goes (#609).
REM  * Every other letter that holds a data disc at launch: one with a
REM    data\ folder, or with a volume_info.json and no
REM    recovery\scripts\restore.bat (that is a META disc).
REM
REM The letters go unquoted: "D:\" would end in \" and the program would
REM read it as a quote and swallow the rest of the line.
REM
REM --mount-parent only once the pack cache folder exists.  Without the
REM cache, tier 1 opens a catalog.db it finds there in place, and that
REM disc cannot come out of the drive until the restore ends.
for %%L in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do set "_SEEN_%%L="
set "DISC_LETTERS="
if defined LCSAS_RELOCATED set "DISC_LETTERS= %LCSAS_RELOCATED%"
if defined LCSAS_RELOCATED set "_SEEN_%LCSAS_RELOCATED:~0,1%=1"
REM (`if defined` first: wine runs a for over an empty list once.)
if defined RESUME_LETTERS for %%L in (%RESUME_LETTERS%) do if not defined _SEEN_%%L (
    set "_SEEN_%%L=1"
    set "DISC_LETTERS=!DISC_LETTERS! %%L:\"
)
set "PACK_SEARCH_ARGS="
for %%L in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    set "ADD_L="
    if exist "%%L:\data\" set "ADD_L=1"
    if exist "%%L:\volume_info.json" if not exist "%%L:\recovery\scripts\restore.bat" set "ADD_L=1"
    if defined _SEEN_%%L set "ADD_L="
    if defined ADD_L set "DISC_LETTERS=!DISC_LETTERS! %%L:\"
    REM Old discs kept the packs under repo\data\.
    if exist "%%L:\repo\data\" set "PACK_SEARCH_ARGS=!PACK_SEARCH_ARGS! --pack-search %%L:\repo"
)
if not defined DISC_LETTERS goto :letters_done
for %%D in (%DISC_LETTERS%) do (
    set "PACK_SEARCH_ARGS=!PACK_SEARCH_ARGS! --pack-search %%D"
    if defined CACHE_OK set "PACK_SEARCH_ARGS=!PACK_SEARCH_ARGS! --mount-parent %%D"
)
echo [lcsas-restore] drives searched for discs:%DISC_LETTERS%
:letters_done

REM --meta-disc: tier 1 skips the META disc's letter while the META disc
REM is in it, and moves its own working folder off the disc before it
REM asks for one, so the disc can come out.
set "META_DISC_ARG="
if defined LCSAS_RELOCATED set "META_DISC_ARG=--meta-disc %LCSAS_RELOCATED%"

REM LCSAS_INTERACTIVE=on/off/auto is passed to tier 1 as --interactive.
REM Its own default, auto, asks at the disc prompt only when it reads
REM from a console; a test that feeds the answers through a pipe sets on.
set "INTERACTIVE_ARG="
if /i "!LCSAS_INTERACTIVE!"=="on"   set "INTERACTIVE_ARG=--interactive on"
if /i "!LCSAS_INTERACTIVE!"=="off"  set "INTERACTIVE_ARG=--interactive off"
if /i "!LCSAS_INTERACTIVE!"=="auto" set "INTERACTIVE_ARG=--interactive auto"

REM Optional --catalog if a catalog.db is present.  It lets the disc
REM prompt name the disc that holds a pack.
REM The meta disc deliberately carries NO catalog.db (it would always
REM be stale at burn time).  Scan every drive letter for catalog.db and
REM keep the LAST one found (highest drive letter), not the newest:
REM plain cmd has no locale-proof way to compare file times (#482).  It
REM only feeds the disc-swap hints; with an older catalog, hints for
REM packs burned after it say "no record", and the restore still works.
REM With no disc catalog in a drive, the copy an earlier run left in
REM TOOLS is used (a restore that carries on, #609).
REM Tier 1 gets a COPY in TOOLS, never the file on a disc: it keeps the
REM catalog open until it ends, and an open file holds the disc in the
REM drive (#610).  `copy` keeps the file's date, which tier 1 compares
REM with any catalog it finds later.
set "CATALOG_ARG="
set "CATALOG_PICK="
set "CATALOG_COPIED="
if exist "%TOOLS%\catalog.db" set "CATALOG_PICK=%TOOLS%\catalog.db"
if exist "%RECOVERY%\catalog.db" set "CATALOG_PICK=%RECOVERY%\catalog.db"
if exist "%REPO%\catalog.db"     set "CATALOG_PICK=%REPO%\catalog.db"
for %%L in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    if exist "%%L:\catalog.db" (
        set "CATALOG_PICK=%%L:\catalog.db"
    )
)
if not defined CATALOG_PICK goto :catalog_done
for %%I in ("%CATALOG_PICK%") do set "CATALOG_PICK=%%~fI"
set "CATALOG_COPY=%TOOLS%\catalog.db"
if /i "%CATALOG_PICK%"=="%CATALOG_COPY%" goto :catalog_copied
del "%CATALOG_COPY%" >nul 2>nul
copy /Y "%CATALOG_PICK%" "%CATALOG_COPY%" >nul 2>nul
set "CATALOG_COPIED=1"
:catalog_copied
if not exist "%CATALOG_COPY%" (
    echo [lcsas-restore] could not copy !CATALOG_PICK! to !TOOLS!,
    echo                 so the disc prompts will not name the discs.
    goto :catalog_done
)
set "CATALOG_ARG=--catalog "!CATALOG_COPY!""
if defined CATALOG_COPIED echo [lcsas-restore] using catalog !CATALOG_PICK! (a copy in !TOOLS!)
if not defined CATALOG_COPIED echo [lcsas-restore] using the catalog copy in !TOOLS!
:catalog_done

REM ----- How to carry on if the restore stops (#609) ----------------
REM Relocated, the tools folder outlives this run until the restore has
REM finished.  resume.cfg says what the run was doing (never the
REM password), and "Resume LCSAS restore.bat" carries on from it with no
REM META disc.  Written before the password is asked, so every way the
REM run can stop from here on leaves a way to carry on.
set "RESUME_CMD="
set "DESK_LAUNCHER="
if defined LCSAS_RELOCATED call :save_resume

REM ----- Password prompt --------------------------------------------
REM CMD has no `read -s` equivalent, so the password is visible while
REM typing.  For privacy, run from PowerShell with Read-Host -AsSecure
REM (documented in RECOVER_WINDOWS.txt).
echo.
echo If PASSWORD_INFO.txt says the password is SPLIT into share cards,
echo rebuild it first with bin\%ARCH%\lcsas-keyshare.exe -- see
echo recovery\docs\RECOVER_WINDOWS.txt, "SHARE CARDS (SPLIT PASSWORDS)".
echo.
set /p "LCSAS_PW=Password: "
REM `if not defined`, not `if "%LCSAS_PW%"==""`: a '"' in the password
REM broke that comparison's syntax (#482).
if not defined LCSAS_PW (
    echo ERROR: empty password
    pause
    exit /b 1
)

REM Write password to a transient temp file (deleted on exit).
set "PWFILE=%TEMP%\lcsas-pw-%RANDOM%-%TIME:~6,2%%TIME:~9,2%.txt"
REM `echo !LCSAS_PW!` runs a password of exactly "on", "off" or "/?" as
REM an echo argument and writes no password (#482).  Those three go
REM through `set /p`, which writes them as-is (without a newline).  Not
REM `echo(`: Windows takes it, but wine writes a literal "(" and the
REM wine tests drive this script.
set "PW_PLAIN_ECHO=1"
if /i "!LCSAS_PW!"=="on" set "PW_PLAIN_ECHO="
if /i "!LCSAS_PW!"=="off" set "PW_PLAIN_ECHO="
if "!LCSAS_PW!"=="/?" set "PW_PLAIN_ECHO="
if defined PW_PLAIN_ECHO (
    > "%PWFILE%" echo !LCSAS_PW!
) else (
    > "%PWFILE%" <nul set /p "=!LCSAS_PW!"
)
set "PW_PLAIN_ECHO="
set "LCSAS_PW="

REM Ensure the temp file is cleaned up no matter how we exit.
REM (CMD has no real trap; we delete after each branch.)

REM ----- Tier 1: prebuilt lcsas-restore.exe -------------------------
set "BIN=%RECOVERY%\bin\%ARCH%\lcsas-restore.exe"
if exist "%BIN%" (
    echo.
    echo [tier 1] running !BIN!
    "%BIN%" --repo "%REPO%" --password-file "%PWFILE%" --target "%TARGET%" --snapshot latest %PACK_SEARCH_ARGS% %CATALOG_ARG% %META_DISC_ARG% %INTERACTIVE_ARG%
    set "RC=!ERRORLEVEL!"
    REM %PWFILE% is deleted only on the TERMINAL branches below -- the
    REM fall-through to tier 2 needs it alive or rustic-static.exe gets
    REM a --password-file pointing at nothing and can never succeed.
    REM Tier 2, the no-tier epilogue and :tier1_stopped each do their
    REM own del.
    if !RC! equ 0 (
        del "%PWFILE%" 2>nul
        goto :restore_done
    )
    REM A key file POSITIVELY rejected the password (issue #384).  Every
    REM tier reads the same keys with the same password, so tier 2 would
    REM only fail identically (and leave a partial tree behind first);
    REM stop here instead of falling through.  Exit 77 must be raised at
    REM TOP LEVEL: a bare "exit /b 77" INSIDE this parenthesized
    REM `if exist "%BIN%"` block does NOT propagate to `cmd /c` on real
    REM Windows (cmd /c returns 0), so goto a top-level label to exit.
    REM The tools folder stays: the restore has not finished (#609).
    if !RC! equ 77 (
        del "%PWFILE%" 2>nul
        echo.
        echo ============================================================
        echo  ERROR: could not decrypt the repository with that password.
        echo  Check the password ^(and any share cards^) and re-run.
        if defined RESUME_CMD echo  To try again, run "!RESUME_CMD!"
        echo ============================================================
        pause
        goto :wrong_password
    )
    REM 75: the reader typed q at the disc prompt, and tier 1 paused the
    REM restore.  That ends this run whatever the set's layout (#615).
    if !RC! equ 75 goto :tier1_stopped
    REM Any other exit.  Tier 2, rustic-static, reads packs only from
    REM the data\ folder of the backup set.  A set found under a
    REM metadata\ folder has none: its packs are on the data discs.
    REM Tier 2 could only fail there, and then report "no working
    REM recovery method" although tier 1 ran fine (#615).  Stop instead
    REM and keep what is restored, as restore.sh does (#227).
    if not exist "%REPO%\data\" goto :tier1_stopped
    echo [tier 1] failed with exit code !RC!; trying tier 2...
)

REM ----- Tier 2: vendored rustic-static.exe -------------------------
set "BIN=%RECOVERY%\bin\%ARCH%\rustic-static.exe"
if exist "%BIN%" (
    echo.
    echo [tier 2] running !BIN!
    "%BIN%" --repository "%REPO%" --password-file "%PWFILE%" restore latest "%TARGET%"
    set "RC=!ERRORLEVEL!"
    del "%PWFILE%" 2>nul
    if !RC! equ 0 goto :restore_done
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
echo  A manual restore with the tier-3 Python reader on the disc
echo  (requires a Python 3 install on this host) is described in
echo  %RECOVERY%\docs\RECOVER_WINDOWS.txt -- this .bat does not
echo  launch it for you.
echo ============================================================
pause
exit /b 1

REM ----- The restore has finished -------------------------------------
REM Reached only via `goto` from tier 1 or tier 2 when they exit 0.
:restore_done
del "%PWFILE%" 2>nul
REM The pack cache is no longer needed.  Only remove a folder this
REM script chose; one named in LCSAS_PACK_CACHE_DIR stays.
if defined CACHE_SET_HERE rmdir /s /q "!LCSAS_PACK_CACHE_DIR!" 2>nul
REM Nothing is left to carry on, so the launcher on the Desktop goes.
REM It ended its own batch with (goto) before it ran this copy, so it
REM can be deleted while this copy runs.  The one in TOOLS goes with
REM the folder.
if defined DESK_LAUNCHER del /f /q "!DESK_LAUNCHER!" >nul 2>nul
REM Relocated, this copy runs from TOOLS and cannot delete it: .done
REM tells the copy on the disc (or the launcher) to, once this copy has
REM returned (#609).  Not relocated, the run kept only its pack cache
REM and its catalog copy there, and the folder goes if that is all.
if defined LCSAS_RELOCATED (
    2>nul > "%TOOLS%\.done" echo.
) else (
    if defined CATALOG_COPIED del /f /q "!CATALOG_COPY!" >nul 2>nul
    rmdir "!TOOLS!" >nul 2>nul
)
echo.
echo ============================================================
echo  Restore complete.  Files restored to: !TARGET!
echo ============================================================
pause
exit /b 0

REM ----- Subroutines --------------------------------------------------

:copy_one
REM %~1 = a path under the recovery folder.  Copy it from the disc into
REM the tools folder and name it on screen.  A copy that fails or comes
REM out a different size marks the whole copy bad (COPY_BAD).
if not exist "%SRC_ROOT%\%~1" goto :eof
echo                   recovery\%~1
copy /Y "%SRC_ROOT%\%~1" "%TOOLS%\recovery\%~1" >nul 2>nul
if errorlevel 1 set "COPY_BAD=1"
for %%S in ("%SRC_ROOT%\%~1") do for %%D in ("%TOOLS%\recovery\%~1") do if not "%%~zS"=="%%~zD" set "COPY_BAD=1"
goto :eof

:read_resume
REM Read TOOLS\resume.cfg (key=value lines; # starts a comment) into
REM RS_set, RS_target, RS_meta and RS_letters.
for %%K in (set target meta letters) do set "RS_%%K="
if not exist "%TOOLS%\resume.cfg" goto :eof
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%TOOLS%\resume.cfg") do set "RS_%%A=%%B"
goto :eof

:save_resume
REM resume.cfg: the backup set's folder name, the restore folder, the
REM META disc's drive letter and the letters searched for discs.  NEVER
REM the password.  The letters are kept because a drive that is empty
REM when the restore carries on is not seen at all.  Then the launcher,
REM in TOOLS and on the Desktop.  All of it is written by echo, so it
REM has CRLF line ends like every .bat.
for %%I in ("%REPO%") do set "SAVE_SET=%%~nxI"
set "SAVE_LETTERS="
for %%D in (%DISC_LETTERS%) do (
    set "_L=%%D"
    set "SAVE_LETTERS=!SAVE_LETTERS! !_L:~0,1!"
)
set "CFG=%TOOLS%\resume.cfg"
> "%CFG%" echo # What "Resume LCSAS restore.bat" carries on with.  No password.
>>"%CFG%" echo set=!SAVE_SET!
>>"%CFG%" echo target=!TARGET!
>>"%CFG%" echo meta=!LCSAS_RELOCATED!
>>"%CFG%" echo letters=!SAVE_LETTERS!
REM Read it back: a folder name that cmd cannot carry through a file
REM (one with a "!" in it) must not send a resumed restore elsewhere.
call :read_resume
if not "!RS_set!"=="!SAVE_SET!" goto :resume_unsaved
if not "!RS_target!"=="!TARGET!" goto :resume_unsaved
if not "!RS_meta!"=="!LCSAS_RELOCATED!" goto :resume_unsaved
REM The launcher.  (goto) ends it before the restore runs, as the
REM relaunch above, so this copy may delete the one on the Desktop at
REM the end; it removes the tools folder once .done is there.
set "LAUNCHER=%TOOLS%\Resume LCSAS restore.bat"
> "%LAUNCHER%" echo @echo off
>>"%LAUNCHER%" echo REM Carries on the LCSAS restore that stopped.  restore.bat wrote
>>"%LAUNCHER%" echo REM this file, and deletes it once the restore has finished.  The
>>"%LAUNCHER%" echo REM META disc is not needed: the restore runs from the folder below.
>>"%LAUNCHER%" echo setlocal
>>"%LAUNCHER%" echo set "TOOLS=!TOOLS!"
>>"%LAUNCHER%" echo if exist "%%TOOLS%%\resume.cfg" goto :carry_on
>>"%LAUNCHER%" echo echo There is no stopped LCSAS restore to carry on in %%TOOLS%%
>>"%LAUNCHER%" echo echo To restore, put the META disc in and run restore.bat from it.
>>"%LAUNCHER%" echo pause
>>"%LAUNCHER%" echo exit /b 1
>>"%LAUNCHER%" echo :carry_on
>>"%LAUNCHER%" echo (goto) 2^>nul ^& cd /d %%SystemDrive%%\ ^& call "%%TOOLS%%\recovery\scripts\restore.bat" --resume ^& call set "LCSAS_RC=%%%%ERRORLEVEL%%%%" ^& (if exist "%%TOOLS%%\.done" del /f /q "%%~f0") 2^>nul ^& (if exist "%%TOOLS%%\.done" rmdir /s /q "%%TOOLS%%") 2^>nul ^& cmd /c exit %%%%LCSAS_RC%%%%
if not exist "%LAUNCHER%" goto :resume_unsaved
set "RESUME_CMD=%LAUNCHER%"
REM The Desktop: where Explorer has it (OneDrive may have moved it),
REM else %USERPROFILE%\Desktop.
set "DESKTOP="
for /f "tokens=1,2,*" %%A in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders" /v Desktop 2^>nul') do if /i "%%A"=="Desktop" set "DESKTOP=%%C"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "!DESKTOP!\" goto :eof
copy /Y "%LAUNCHER%" "!DESKTOP!\Resume LCSAS restore.bat" >nul 2>nul
if exist "!DESKTOP!\Resume LCSAS restore.bat" set "DESK_LAUNCHER=!DESKTOP!\Resume LCSAS restore.bat"
goto :eof

:resume_unsaved
del /f /q "%CFG%" >nul 2>nul
set "RESUME_CMD="
goto :eof

REM Reached only via `goto` from the relocation, before anything is
REM copied.
:tools_taken
echo ERROR: LCSAS_TOOLS_DIR names !TOOLS!,
echo a folder that already holds other files.  The tools folder is
echo deleted once the restore has finished, so name a new or empty
echo folder instead, or leave LCSAS_TOOLS_DIR unset.
pause
exit /b 1

REM Reached only via `goto` from the tier-1 exit-77 branch above.  At
REM top level (outside every parenthesized block) so `exit /b 77`
REM reliably propagates to `cmd /c` (issue #384).  Normal flow stops at
REM the `exit /b 1` above and never falls into this label.
:wrong_password
exit /b 77

REM Reached only via `goto` from tier 1 when it stopped with exit code 75
REM (the reader typed q at the disc prompt), or with any exit code but 0
REM or 77 when the backup set has no data\ folder, so tier 2 cannot run
REM (#615).  At top level for the same reason as :wrong_password.  Tier 1
REM never removes a file it has restored, and a new run skips every file
REM that is already complete (#92).  The tools folder stays (#609).
:tier1_stopped
del "%PWFILE%" 2>nul
echo.
echo ============================================================
echo  The restore stopped before it finished ^(code %RC%^).
echo.
echo  The files restored so far are kept in:
echo    !TARGET!
echo.
if defined RESUME_CMD (
    if defined DESK_LAUNCHER echo  To carry on, double-click "Resume LCSAS restore.bat" on
    if defined DESK_LAUNCHER echo  your Desktop, or type this command:
    if not defined DESK_LAUNCHER echo  To carry on, type this command:
    echo    "!RESUME_CMD!"
    echo  The META disc is not needed for that.  It skips the files that
    echo  are already there and goes on from where it stopped.
) else (
    echo  To carry on, run restore.bat again from the META disc and
    echo  choose the same folder.  It skips the files that are already
    echo  there and goes on from where it stopped.
)
echo ============================================================
pause
exit /b %RC%
