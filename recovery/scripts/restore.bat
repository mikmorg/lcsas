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
REM The order of the questions: the newest data disc (#605), the backup
REM set, by its name (#606), the folder, then the password or the share
REM cards (#607, #608), and after a good restore whether to restore
REM another backup set from the same discs.
REM
REM Split-password archives: when a backup set's LCSAS_REPO.txt says its
REM password is split into share cards, the script asks for the cards
REM and rebuilds the password with the bundled combiner
REM bin\<arch>\lcsas-keyshare.exe (#607).  When only PASSWORD_INFO.txt
REM says so, rebuild it first -- see recovery/docs/RECOVER_WINDOWS.txt,
REM "SHARE CARDS (SPLIT PASSWORDS)".
REM ====================================================================

REM setlocal, taking LCSAS_RELOCATED (set by the copy on the disc when it
REM relaunches this one, see the end of the guard below) into the local
REM scope and out of the caller's environment.  The relaunch runs after
REM the copy on the disc has ended, so it sets the variable in the caller's
REM own environment; without this a later run from the same prompt
REM would think it had already been relocated.
for %%R in ("%LCSAS_RELOCATED%") do (set "LCSAS_RELOCATED=" & setlocal enabledelayedexpansion & set "LCSAS_RELOCATED=%%~R")
REM The script's own state, never taken from the caller's environment.
for %%V in (CACHE_SET_HERE ANOTHER_PASS STAYING_ON_DISC METADATA_SET_HERE PW_NOTE_SAID NEWEST_ROOT) do set "%%V="

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
REM Storage Sense empty.  A run that does not relocate keeps its pack
REM cache, its catalog copy and the metadata it merges in TOOLS\in-place
REM (see "A run that does not relocate" below).
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
REM The tools folder is deleted once the restore has finished, so the
REM script uses only a folder that is new, empty, or one it made itself:
REM it writes .lcsas-tools there before anything else.  Any other folder
REM is refused before anything in it changes: one that LCSAS_TOOLS_DIR
REM names and that holds the reader's own files (a META disc copied to a
REM USB stick, say), or a whole drive (E:\ is "E:" by now, and
REM `rmdir /s /q "E:"` would empty the drive's current folder).  A run
REM that does not relocate is checked too.  The copy that runs from the
REM tools folder (relocated, or carrying on) is in such a folder already.
if defined LCSAS_RELOCATED goto :tools_owned
if defined RESUME goto :tools_owned
if /i "%~1"=="--check-disc" goto :tools_owned
if "%TOOLS:~-1%"==":" goto :tools_taken
if not exist "%TOOLS%\" goto :tools_owned
if exist "%TOOLS%\.lcsas-tools" goto :tools_owned
dir /b /a "%TOOLS%" 2>nul | findstr "." >nul
if not errorlevel 1 goto :tools_taken
:tools_owned
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
REM cmd itself reports a redirect it cannot open, on its OWN stderr, so
REM a 2>nul on the command, before or after the >, comes too late: every
REM start from a disc printed "Access is denied." (#611).  Only a 2>nul
REM on a block around it catches that (wine is silent either way, so
REM only windows-e2e shows it).  Every write that may fail uses this form.
set "PROBE=%~dp0lcsas-probe-%RANDOM%.tmp"
(> "%PROBE%" type nul) 2>nul
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

REM An earlier run left a whole copy of this very script (restore.bat is
REM copied last): run that, with the metadata, catalog copy and pack
REM cache it has.  fc /b compares the bytes.
if not exist "%TOOLS%\recovery\scripts\restore.bat" goto :tools_fresh
if not exist "%TOOLS%\recovery\bin\%ARCH%\lcsas-restore.exe" goto :tools_fresh
fc /b "%~f0" "%TOOLS%\recovery\scripts\restore.bat" >nul 2>nul <nul
if errorlevel 1 goto :tools_fresh
REM ...and only when it was copied from this very META disc (archive.txt,
REM see :archive_id).  A folder left by a stopped restore of another
REM archive, or from an older META disc, holds that disc's sets, catalog
REM copy and merged-from.txt, whose labels repeat from one archive to the
REM next: it is replaced.
call :archive_id
set "OLD_ARCHIVE="
if exist "%TOOLS%\archive.txt" for /f "usebackq delims=" %%A in ("%TOOLS%\archive.txt") do if not defined OLD_ARCHIVE set "OLD_ARCHIVE=%%A"
if not "!OLD_ARCHIVE!"=="!ARCHIVE_ID!" goto :tools_fresh
echo [lcsas-restore] using the recovery files already copied to !TOOLS!
REM A backup set on this META disc that the earlier copy lacks goes in
REM too (#605); the sets already there keep what the earlier run read.
if defined META_SRC for /f "delims=" %%T in ('dir /ad /b "%META_SRC%" 2^>nul') do (
    if not exist "!TOOLS!\metadata\%%T\" echo                   metadata\%%T
    if not exist "!TOOLS!\metadata\%%T\" xcopy /e /i /q /y "%META_SRC%\%%T" "!TOOLS!\metadata\%%T" >nul 2>nul <nul
)
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
    fc /b "!SRC_ROOT!\bin\%ARCH%\lcsas-restore.exe" "%TOOLS%\recovery\bin\%ARCH%\lcsas-restore.exe" >nul 2>nul <nul
    if not errorlevel 1 set "KEEP_CACHE=1"
)
if not defined KEEP_CACHE rmdir /s /q "%TOOLS%\pack-cache" 2>nul
:tools_cleared
if exist "%TOOLS%\recovery\" rmdir /s /q "%TOOLS%\recovery" 2>nul
if exist "%TOOLS%\metadata\" rmdir /s /q "%TOOLS%\metadata" 2>nul
if exist "%TOOLS%\metadata-new\" rmdir /s /q "%TOOLS%\metadata-new" 2>nul
del /f /q "%TOOLS%\catalog.db" "%TOOLS%\resume.cfg" "%TOOLS%\Resume LCSAS restore.bat" "%TOOLS%\merged-from.txt" "%TOOLS%\archive.txt" >nul 2>nul

REM .lcsas-tools first: the folder is the restore's own from now on.
call :claim_tools "%TOOLS%"
mkdir "%TOOLS%\recovery\scripts" 2>nul
mkdir "%TOOLS%\recovery\bin\%ARCH%" 2>nul
mkdir "%TOOLS%\recovery\docs" 2>nul
if not exist "%TOOLS%\recovery\bin\%ARCH%\" (
    echo [lcsas-restore] cannot create !TOOLS! -- staying on disc.
    echo                 Do not eject this disc until the restore has finished.
    set "STAYING_ON_DISC=1"
    goto :after_relocate
)

REM Only what a restore on this computer runs (#612): the three programs
REM in bin\<arch>\, and rustic-static.exe only when a backup set here
REM holds its own data\ (tier 2 cannot run otherwise); this computer's
REM guide; the backup sets' metadata.  Up to rc5 this was the whole bin
REM tree, 744 MB for six kinds of computer, with nothing on screen.
REM Every program that needs no input reads from <nul (xcopy, attrib,
REM fc, reg, lcsas-keyshare, --list-snapshots): on real Windows xcopy
REM and attrib read the script's own input, so with answers piped in
REM (windows-e2e, a test rig) they swallowed every answer left.  Wine's
REM do not, so only CI showed it.
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
    xcopy /e /i /q /y "%META_SRC%\%%T" "!TOOLS!\metadata\%%T" >nul 2>nul <nul
    if errorlevel 4 set "COPY_BAD=1"
)
if defined SRC_ROOT if exist "!SRC_ROOT!\catalog.db" copy /Y "!SRC_ROOT!\catalog.db" "!TOOLS!\catalog.db" >nul 2>nul
call :archive_id
if not defined ARCHIVE_ID goto :archive_saved
(> "%TOOLS%\archive.txt" echo !ARCHIVE_ID!) 2>nul
:archive_saved
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
    set "STAYING_ON_DISC=1"
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
REM cannot delete its own folder), and only if it is the restore's own
REM (.lcsas-tools, see "The tools folder" above).  A restore that
REM stopped keeps the folder, for "Resume LCSAS restore.bat" (#609).  A
REM command after the `call` replaces its exit code (a trailing `set`
REM turned 7 into 0), so keep the code in LCSAS_RC first and end with
REM `cmd /c exit` to hand it back.  `call` expands %%ERRORLEVEL%% after
REM the copy has returned; the child cmd expands %%LCSAS_RC%%.  Not
REM `exit`, which would close the prompt window of an heir who started
REM this from one.
REM
REM wine's cmd does not end a batch at (goto): it prints "Argument
REM missing", skips the rest of the line and goes on with the next one,
REM so under wine the run carries on from the disc, not relocated.  The
REM wine tests check the copy that way; windows-e2e runs this line.
:relaunch
(goto) 2>nul & cd /d %SystemDrive%\ & set "LCSAS_RELOCATED=%~d0\" & call "%TOOLS%\recovery\scripts\restore.bat" %* & call set "LCSAS_RC=%%ERRORLEVEL%%" & (if exist "%TOOLS%\.done" if exist "%TOOLS%\.lcsas-tools" rmdir /s /q "%TOOLS%") 2>nul & cmd /c exit %%LCSAS_RC%%

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

REM ----- A run that does not relocate (#609) ---------------------------
REM Run from a copy that can be written to (a META disc copied to a USB
REM stick or a folder, or LCSAS_NO_RELOCATE=1), or from the disc after a
REM copy that failed, the script keeps its pack cache, its catalog copy
REM and the metadata it merges in TOOLS\in-place, never in TOOLS itself:
REM TOOLS may hold a restore that stopped, which "Resume LCSAS
REM restore.bat" carries on, and that must stay as it is.  TOOLS_TOP is
REM the tools folder.  :use_tools makes both the first time they are
REM needed.
set "TOOLS_TOP=%TOOLS%"
if not defined LCSAS_RELOCATED set "TOOLS=%TOOLS%\in-place"

REM ----- The newest data disc (#605) ---------------------------------
REM Each backup set's index and snapshots on the META disc are as old as
REM the META disc: a backup made after it was burned is not in them, and
REM a backup set added since is not there at all.  Every data disc has
REM them too, as of its own burn, so the newest data disc has the newest.
REM Up to rc5 the META disc's copy won, and with one drive no data disc
REM is in at the start, so "latest" meant the latest when the META disc
REM was burned.
REM
REM So, run from a META disc (or its copy in the tools folder) with no
REM data disc in any drive, the script first asks for the newest data
REM disc.  "Newest" comes from the label in the disc's VOLUME_INFO.txt
REM (or volume_info.json), never from the drive's own name: Windows cuts
REM that to 16 characters, LCSAS_CD700_2026 for every CD of a year.
REM With data discs already in, it takes the newest of those.  Then the
REM tools folder's metadata\ gets, for each backup set on that disc, its
REM snapshots\ and index\ from that disc ONLY, keys\ from both, and its
REM LCSAS_REPO.txt; a set only on the META disc keeps the META copy (C2).
REM Never a union of index\ or snapshots\: the META copy can be newer
REM than any disc at hand, and an index entry for a pack pruned since
REM would make tier 1 pick between two at random.  The catalog copy is
REM taken from that disc too (see the catalog below).
REM
REM Not when carrying on a stopped restore: that must go on with the very
REM same snapshot.  Not when the script runs from the META disc itself
REM (the copy failed): that disc cannot come out.
for %%I in ("%RECOVERY%\..") do set "DISC_ROOT=%%~fI"
set "DISC_ROOT_S=%DISC_ROOT%"
if not "%DISC_ROOT_S:~-1%"=="\" set "DISC_ROOT_S=%DISC_ROOT_S%\"
if defined RESUME goto :newest_done
REM A META disc: relocated from one, or (C2) no label in VOLUME_INFO.txt
REM and recovery\scripts\restore.bat at the root.
set "META_HERE="
if defined LCSAS_RELOCATED set "META_HERE=1"
if defined META_HERE goto :meta_known
if not exist "%DISC_ROOT_S%recovery\scripts\restore.bat" goto :newest_done
call :read_label "%DISC_ROOT_S%"
if defined RL_LABEL goto :newest_done
:meta_known
call :find_newest
if defined DATA_SEEN goto :newest_found
if "%LCSAS_NEWEST_DISC_PROMPT%"=="0" goto :newest_done
if defined STAYING_ON_DISC goto :newest_done
echo.
echo  Take the META disc out and put in your NEWEST data disc: the one
echo  with the latest year, and then the highest number, e.g.
echo  LCSAS_CD700_2026_0002.  File Explorer shows only the first 16
echo  characters of the label; the full label is printed on the disc.
set "ND_TRIES=0"
:newest_ask
echo.
echo  Press ENTER when it is in; S then ENTER to skip ^(use the copies
echo  on the META disc^); Q then ENTER to quit.
set "ND_ANS="
set /p "ND_ANS=Newest data disc: "
if /i "!ND_ANS!"=="q" goto :newest_quit
if /i "!ND_ANS!"=="s" goto :newest_skip
call :find_newest
if defined NEWEST_ROOT goto :newest_found
set /a ND_TRIES+=1
if defined DATA_SEEN goto :newest_unlabelled
echo  No data disc is in a drive yet.  Wait until File Explorer shows
echo  it, then press ENTER again.
goto :newest_again
:newest_unlabelled
echo  The disc that is in has no LCSAS label in its VOLUME_INFO.txt, so
echo  it is not a data disc of this archive.  Put in the newest data
echo  disc, the one whose label is printed on it.
:newest_again
REM An empty answer and the end of the input look the same to cmd: after
REM five tries the restore goes on with the copies on the META disc, so
REM a run whose input has ended does not ask for ever.
if %ND_TRIES% lss 5 goto :newest_ask
echo  Going on with the copies on the META disc.
goto :newest_done
:newest_skip
echo  Using the copies on the META disc.
goto :newest_done
:newest_found
if not defined NEWEST_ROOT goto :newest_done
echo.
echo [lcsas-restore] newest data disc: !NEWEST_LABEL! in drive !NEWEST_ROOT:~0,2!
call :merge_newest
:newest_done

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
REM -- The copy merged with the newest data disc, when this run made it in
REM    the tools folder without relocating (#605); relocated, it is
REM    %DISC_ROOT%\metadata already.  First, so it wins over the copy on
REM    the disc this script runs from.
if defined METADATA_SET_HERE call :scan_metadata "%TOOLS%\metadata"
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
REM The menu shows each set's name from its LCSAS_REPO.txt (#606): the
REM folders are named by an id, a UUID since `repo add`.  Only the
REM marker's name_cmd, which holds nothing cmd acts on (C1), and printed
REM with delayed expansion, so even a marker someone edited is printed,
REM never run.  A set with no marker shows its folder name.
REM LCSAS_REPO picks a set by its id, its name_cmd or its name.  After a
REM good restore, "another backup set?" comes back here (ANOTHER_PASS).
REM See the :add_cand portability note re: %%NCAND%% / `call set`.
:choose_set
set "REPO="
set "SEL="
if not defined LCSAS_REPO goto :menu
for /l %%I in (1,1,%NCAND%) do call :match_set %%I
if defined SEL goto :repo_selected
for /l %%I in (1,1,%NCAND%) do call :match_name %%I
if defined SEL goto :repo_selected
echo ERROR: LCSAS_REPO=!LCSAS_REPO! is not among the backup sets on this archive:
for /l %%I in (1,1,%NCAND%) do call :menu_line %%I
pause
exit /b 1

:menu
if %NCAND% equ 1 (
    set "SEL=1"
    goto :repo_selected
)
echo.
echo This archive contains more than one backup set:
for /l %%I in (1,1,%NCAND%) do call :menu_line %%I
echo.
echo Type the number of the one to restore, or Q then ENTER to quit.
:pick_repo
set "REPO_PICK="
set /p "REPO_PICK=Which one do you want to restore? [1]: "
if "!REPO_PICK!"=="" set "REPO_PICK=1"
if /i "!REPO_PICK!"=="q" goto :menu_quit
set "SEL="
for /l %%I in (1,1,%NCAND%) do (
    if "!REPO_PICK!"=="%%I" set "SEL=%%I"
)
if not defined SEL (
    echo Please enter a number between 1 and %NCAND%, or Q to quit.
    goto :pick_repo
)

goto :repo_selected

:menu_quit
REM After a set was restored, Q here means "no more": the run has done
REM all the reader chose (exit 0, C5).  Before any, nothing was restored.
if defined ANOTHER_PASS goto :all_done
echo Stopped; nothing was restored.
pause
exit /b 65

:menu_line
REM %~1 = a candidate's number.  Delayed expansion prints the text as it
REM is: nothing in it is run.
echo    [%~1] !CAND_LABEL_%~1!
goto :eof

:match_set
REM %~1 = a candidate's number.  SEL = it when LCSAS_REPO is its folder
REM name (the id) or its name_cmd, case-blind.
if defined SEL goto :eof
if /i "!CAND_NAME_%~1!"=="!LCSAS_REPO!" set "SEL=%~1"
if defined CAND_NC_%~1 if /i "!CAND_NC_%~1!"=="!LCSAS_REPO!" set "SEL=%~1"
goto :eof

:match_name
REM %~1 = a candidate's number.  SEL = it when LCSAS_REPO is the name=
REM line of its marker; tried only when no id or name_cmd matched.  That
REM line may hold any character, "!" and "^" too, so it is compared with
REM delayed expansion off, straight from the file (case-blind).  cmd
REM reads the file in the console's code page, so a name with letters
REM outside A-Z may not match: use the id or the name in the menu then.
if defined SEL goto :eof
if not defined CAND_MK_%~1 goto :eof
set "MN_FILE=!CAND_%~1!\LCSAS_REPO.txt"
setlocal disabledelayedexpansion
set "MN_HIT="
REM (Brackets, not `if ... if /i ...` on one line: wine cannot parse a
REM second `if /i` after a compare.)
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%MN_FILE%") do (
    if "%%A"=="name" (
        if /i "%%B"=="%LCSAS_REPO%" set "MN_HIT=1"
    )
)
endlocal & set "MN_HIT=%MN_HIT%"
if defined MN_HIT set "SEL=%~1"
goto :eof

:add_cand
REM %~1 = candidate dir.  Validity: keys\ and index\ both present.
REM De-dup by repository name (last path component); first hit wins so the
REM copy merged with the newest data disc, scanned first, is preferred.
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
REM The set's marker (C1): CAND_MK_<n> = its id when it has a marker,
REM CAND_NC_<n> = its name_cmd (empty without a marker), CAND_K_<n>/CAND_N_<n> = its split, CAND_SID_<n> = the Split
REM ID on its cards, CAND_LABEL_<n> = its line in the menu.
call :read_marker "%~f1"
set "CAND_MK_%NCAND%=!MK_ID!"
set "CAND_NC_%NCAND%=!MK_NC!"
set "CAND_K_%NCAND%=!MK_K!"
set "CAND_N_%NCAND%=!MK_N!"
set "CAND_SID_%NCAND%=!MK_SID!"
set "_LB=!MK_NC!"
if not defined _LB set "_LB=!_CN!"
if defined MK_K set "_LB=!_LB! (password split: any !MK_K! of !MK_N! share cards)"
set "CAND_LABEL_%NCAND%=!_LB!"
goto :eof

:read_marker
REM %~1 = a backup set's folder.  Reads its LCSAS_REPO.txt (C1), and
REM only when its id= is the folder's name: MK_NC = name_cmd, when every
REM character of it is one C1 allows (else the folder name is shown),
REM MK_K and MK_N = the split's K and N when split=K-of-N, MK_SID = the
REM Split ID.  The marker is a hint: lcsas-keyshare checks the shares.
REM The name= line, which may hold any character, never reaches a for
REM body here: eol=n skips the lines that start with "n" (name= and
REM name_cmd=), and name_cmd= is fetched alone with findstr.  Real cmd
REM puts a for variable's text in after it has parsed the command, but
REM wine puts it in before, so a name with & or | in it would be run
REM there.  (The "#" lines have no "=": they set nothing.)
for %%K in (ID NC SPLIT SID K N) do set "MK_%%K="
if not exist "%~1\LCSAS_REPO.txt" goto :eof
for /f "usebackq eol=n tokens=1,* delims==" %%A in ("%~1\LCSAS_REPO.txt") do (
    if "%%A"=="id" set "MK_ID=%%B"
    if "%%A"=="split" set "MK_SPLIT=%%B"
    if "%%A"=="split_id" set "MK_SID=%%B"
)
for /f "tokens=1,* delims==" %%A in ('findstr /c:"name_cmd=" "%~1\LCSAS_REPO.txt" 2^>nul') do if "%%A"=="name_cmd" set "MK_NC=%%B"
if not "!MK_ID!"=="%~nx1" (
    for %%K in (ID NC SPLIT SID) do set "MK_%%K="
    goto :eof
)
REM Any other character leaves a token.  Spaces are taken out first, and
REM the value is read with %%...%% on a line of its own: wine's for /f
REM splits a "!VAR!" before it expands it, and stops at a space inside
REM delims=.  (The leading x: for /f skips a line that starts with ";".)
REM A quote is made "#" first, with delayed expansion (which never reads
REM the text again): put in a line with %%...%% as it is, a quote would
REM end the quoted text there, and what follows it would run.  The marker
REM is a file on a disc, so the same goes for split= and split_id=.
if not defined MK_NC goto :marker_nc_read
set "_T=!MK_NC: =!"
set "_T=!_T:"=#!"
for /f "delims=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+,-" %%x in ("x%_T%") do set "MK_NC="
:marker_nc_read
if defined MK_SID set "MK_SID=!MK_SID:"=#!"
if not defined MK_SPLIT goto :marker_nosplit
set "_T=!MK_SPLIT:-of-= !"
set "_T=!_T:"=#!"
for /f "tokens=1,2" %%a in ("%_T%") do (
    set "MK_K=%%a"
    set "MK_N=%%b"
)
call :to_number "%MK_K%" MK_K
call :to_number "%MK_N%" MK_N
if not defined MK_N set "MK_K="
if not defined MK_K goto :marker_nosplit
if %MK_K% lss 1 goto :marker_nosplit
if %MK_K% gtr %MK_N% goto :marker_nosplit
call :to_number "%MK_SID%" MK_SID_OK
if not defined MK_SID_OK set "MK_SID="
goto :eof
:marker_nosplit
set "MK_K="
set "MK_N="
set "MK_SID="
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
set "REPO=!CAND_%SEL%!"
set "SET_NAME=!CAND_NC_%SEL%!"
if not defined SET_NAME set "SET_NAME=!CAND_NAME_%SEL%!"
set "SPLIT_K=!CAND_K_%SEL%!"
set "SPLIT_N=!CAND_N_%SEL%!"
set "SPLIT_SID=!CAND_SID_%SEL%!"

REM ----- Ask for target directory -----------------------------------
REM Another backup set goes into a folder of its own by default, so its
REM files do not land among the last set's.
set "DEFAULT_TARGET=%USERPROFILE%\Documents\restored"
if defined ANOTHER_PASS set "DEFAULT_TARGET=%USERPROFILE%\Documents\restored-!SET_NAME!"
echo.
echo ============================================================
echo                 LCSAS Restore (Windows)
echo ============================================================
echo.
echo Recovery root: !RECOVERY!
echo Repo:          !REPO!
echo Backup set:    !SET_NAME!
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
echo Type a folder, or ENTER for the one in brackets; Q then ENTER quits.
set "TARGET="
set /p "TARGET=Restore to which folder? [%DEFAULT_TARGET%]: "
if /i "!TARGET!"=="q" goto :target_quit
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
REM A folder without its drive (restored, or \restored) would be made
REM wherever cmd happens to be: C:\ after the relaunch.  Ask for the
REM whole path.  \\server\share\folder is a whole path too.
if "!TARGET:~1,1!"==":" goto :target_chosen
if "!TARGET:~0,2!"=="\\" goto :target_chosen
echo.
echo  !TARGET! is not a whole path.  Type the folder with its drive,
echo  for example C:\restored
echo.
goto :ask_target
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
(> "%MARKER%" type nul) 2>nul

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
REM (CACHE_SET_HERE is cleared at the top, not here: another backup set,
REM restored in the same run, uses the cache this run made.)
call :use_tools
set "CACHE_OK="
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
REM be stale at burn time).  Tier 1 gets a COPY in TOOLS, never the file
REM on a disc: it keeps the catalog open until it ends, and an open file
REM holds the disc in the drive (#610).  `copy` keeps the file's date,
REM which tier 1 compares with any catalog.db it finds later: it takes a
REM newer one, never an older one.  The copy comes from, in this order:
REM  1. The newest data disc, when the step above found it (C2): it is
REM     still in its drive, as nothing has asked for another disc yet.
REM  2. The copy already in TOOLS, when a restore carries on, or when an
REM     earlier run here read the newest data disc (merged-from.txt): it
REM     is that disc's.  The disc in a drive now may be any disc, and an
REM     older disc's catalog would leave the disc prompts for the packs
REM     burned after it with no disc to name (#609).
REM  3. Else the LAST catalog.db found (highest drive letter), not the
REM     newest: plain cmd has no locale-proof way to compare file times
REM     (#482).  With an older catalog, the prompts for packs burned
REM     after it say "no record", and the restore still works.
REM Another backup set in the same run keeps the copy the first one
REM used: the disc in a drive by then is any disc.
if defined ANOTHER_PASS if defined CATALOG_ARG goto :catalog_done
set "CATALOG_ARG="
set "CATALOG_PICK="
set "CATALOG_COPIED="
set "CATALOG_COPY=%TOOLS%\catalog.db"
if not defined NEWEST_ROOT goto :catalog_not_newest
if not exist "!NEWEST_ROOT!catalog.db" goto :catalog_not_newest
set "CATALOG_PICK=!NEWEST_ROOT!catalog.db"
goto :catalog_picked
:catalog_not_newest
if not exist "%CATALOG_COPY%" goto :catalog_scan
set "CATALOG_PICK=%CATALOG_COPY%"
if defined RESUME goto :catalog_picked
if exist "%TOOLS%\merged-from.txt" goto :catalog_picked
:catalog_scan
if exist "%RECOVERY%\catalog.db" set "CATALOG_PICK=%RECOVERY%\catalog.db"
if exist "%REPO%\catalog.db"     set "CATALOG_PICK=%REPO%\catalog.db"
for %%L in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
    if exist "%%L:\catalog.db" (
        set "CATALOG_PICK=%%L:\catalog.db"
    )
)
:catalog_picked
if not defined CATALOG_PICK goto :catalog_done
for %%I in ("%CATALOG_PICK%") do set "CATALOG_PICK=%%~fI"
if /i "%CATALOG_PICK%"=="%CATALOG_COPY%" goto :catalog_copied
REM /f: a copy made from a disc keeps the disc's read-only mark, which
REM plain del cannot remove -- the old copy was then used while the line
REM below named the new one.  attrib -r takes the mark off the new copy.
del /f /q "%CATALOG_COPY%" >nul 2>nul
copy /Y "%CATALOG_PICK%" "%CATALOG_COPY%" >nul 2>nul
attrib -r "%CATALOG_COPY%" >nul 2>nul <nul
set "CATALOG_COPIED=1"
:catalog_copied
if not exist "%CATALOG_COPY%" (
    echo [lcsas-restore] could not copy !CATALOG_PICK! to !TOOLS!,
    echo                 so the disc prompts will not name the discs.
    goto :catalog_done
)
set "CATALOG_ARG=--catalog "!CATALOG_COPY!""
REM Not `if ... echo ... (a copy ...)`: wine reads those brackets as
REM a block.  A copy left by an earlier run is used as it is.
if not defined CATALOG_COPIED goto :catalog_kept
echo [lcsas-restore] using catalog !CATALOG_PICK! (a copy in !TOOLS!)
goto :catalog_done
:catalog_kept
echo [lcsas-restore] using the catalog copy in !TOOLS!
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

REM ----- The password (#607, #608) -----------------------------------
REM The password reaches the programs in a file in %TEMP%, PWFILE, which
REM is deleted on every way out.  It comes from the first of:
REM   LCSAS_PWFILE      a file; its exact bytes are copied (copy /b)
REM   LCSAS_PASSWORD    the password itself
REM   LCSAS_PW          the same; the older, restore.bat-only name
REM   LCSAS_SHARES_DIR  a folder of share card files, one card a file
REM   the share cards, typed here, when the set's LCSAS_REPO.txt says its
REM                     password is split (P there: type it yourself)
REM   the keyboard.
REM Before tier 1 starts, lcsas-restore.exe --list-snapshots tries the
REM password: it exits 77 when a key file rejects it.  The script then
REM says so and asks again (up to rc5 it stopped there).  An empty
REM answer asks again, and Q quits with 77 (C5).  cmd cannot tell an
REM empty answer from the end of its input, so the third empty answer
REM in a row quits too: a run whose input has ended stops, and does not
REM ask for ever.
REM CMD has no `read -s` equivalent, so a typed password is visible on
REM the screen; the script says so once.  RECOVER_WINDOWS.txt shows how
REM to type it hidden in PowerShell and pass it in LCSAS_PWFILE.
REM A window closed while a password or typed share cards sat in %TEMP%
REM left them there, and a new run picks new names, so nothing ever
REM removed them.  The first pass removes every lcsas-pw-*.txt file and
REM lcsas-shares-* folder in %TEMP% before it asks.  (So run one restore
REM at a time on a computer: a second one would remove the first one's.)
if defined ANOTHER_PASS goto :temp_cleared
del /f /q "%TEMP%\lcsas-pw-*.txt" >nul 2>nul
for /f "delims=" %%D in ('dir /ad /b "%TEMP%\lcsas-shares-*" 2^>nul') do rmdir /s /q "%TEMP%\%%D" 2>nul
:temp_cleared
set "PWFILE=%TEMP%\lcsas-pw-%RANDOM%-%TIME:~6,2%%TIME:~9,2%.txt"
set "KS_EXE=%RECOVERY%\bin\%ARCH%\lcsas-keyshare.exe"
set "PW_SOURCE="
set "PW_OWN="
set "PW_WRONG="
set "PW_FROM="
set "PW_EMPTY=0"
if defined LCSAS_PWFILE set "PW_SOURCE=LCSAS_PWFILE"
if not defined PW_SOURCE if defined LCSAS_PASSWORD set "PW_SOURCE=LCSAS_PASSWORD"
if not defined PW_SOURCE if defined LCSAS_PW set "PW_SOURCE=LCSAS_PW"
if not defined PW_SOURCE if defined LCSAS_SHARES_DIR set "PW_SOURCE=LCSAS_SHARES_DIR"

:pw_next
del /f /q "%PWFILE%" >nul 2>nul
if "!PW_SOURCE!"=="LCSAS_PWFILE" goto :pw_from_file
if "!PW_SOURCE!"=="LCSAS_PASSWORD" goto :pw_from_env
if "!PW_SOURCE!"=="LCSAS_PW" goto :pw_from_env
if "!PW_SOURCE!"=="LCSAS_SHARES_DIR" goto :pw_from_shares_dir
if defined PW_OWN goto :pw_ask
if defined SPLIT_K goto :shares_ask

:pw_ask
if defined PW_NOTE_SAID goto :pw_prompt
set "PW_NOTE_SAID=1"
echo.
echo What you type for the password is shown on the screen: make sure
echo nobody is looking.  RECOVER_WINDOWS.txt shows how to type it hidden.
if defined SPLIT_K goto :pw_prompt
echo If PASSWORD_INFO.txt says the password is SPLIT into share cards,
echo rebuild it first with bin\%ARCH%\lcsas-keyshare.exe -- see
echo recovery\docs\RECOVER_WINDOWS.txt, "SHARE CARDS (SPLIT PASSWORDS)".
:pw_prompt
echo.
REM Cleared first: `set /p` keeps the old value on an empty answer.
set "LCSAS_PW="
set /p "LCSAS_PW=Password: "
REM `if not defined`, not `if "%LCSAS_PW%"==""`: a '"' in the password
REM broke that comparison's syntax (#482).
if not defined LCSAS_PW goto :pw_empty
if /i "!LCSAS_PW!"=="q" goto :pw_gave_up
set "PW_EMPTY=0"
set "PW_VALUE=!LCSAS_PW!"
set "LCSAS_PW="
set "PW_FROM=typed"
call :write_pw
goto :pw_check

:pw_empty
set /a PW_EMPTY+=1
if %PW_EMPTY% geq 3 goto :pw_gave_up
echo Type the password and press ENTER, or Q then ENTER to quit.
goto :pw_prompt

:pw_from_file
set "PW_SOURCE="
copy /b /y "!LCSAS_PWFILE!" "%PWFILE%" >nul 2>nul
REM The copy keeps a read-only mark the reader's file has (a file on a CD
REM or a locked USB stick): take it off, or no later del could remove the
REM copy and no typed password could replace it.
attrib -r "%PWFILE%" >nul 2>nul <nul
if exist "%PWFILE%" goto :pw_file_copied
echo [lcsas-restore] cannot read the password file LCSAS_PWFILE names:
echo                 !LCSAS_PWFILE!
goto :pw_next
:pw_file_copied
echo [lcsas-restore] using the password in the file LCSAS_PWFILE names
set "PW_FROM=preset"
goto :pw_check

:pw_from_env
set "PW_VALUE=!%PW_SOURCE%!"
echo [lcsas-restore] using the password in %PW_SOURCE%
set "PW_SOURCE="
set "PW_FROM=preset"
call :write_pw
goto :pw_check

:pw_from_shares_dir
REM Every file in the folder is one card.  They are copied as card1.txt,
REM card2.txt ... into a new folder under %TEMP% and combined there, so
REM no name in the folder reaches the command line.  More than K cards
REM is fine (lcsas-keyshare uses K of them).
set "PW_SOURCE="
set "SH_DIR="
if not exist "%KS_EXE%" goto :shares_dir_bad
call :new_shares_dir
if not defined SH_DIR goto :shares_dir_bad
set "SH_N=0"
for /f "delims=" %%F in ('dir /b /a-d "%LCSAS_SHARES_DIR%" 2^>nul') do (
    set /a SH_N+=1
    copy /y "%LCSAS_SHARES_DIR%\%%F" "!SH_DIR!\card!SH_N!.txt" >nul 2>nul
)
if "%SH_N%"=="0" goto :shares_dir_bad
set "SH_ARGS="
for /l %%I in (1,1,%SH_N%) do set "SH_ARGS=!SH_ARGS! card%%I.txt"
pushd "%SH_DIR%"
"%KS_EXE%" %SH_ARGS% > "%PWFILE%" 2> "keyshare.txt" <nul
set "KRC=%ERRORLEVEL%"
popd
if "%KRC%"=="0" goto :shares_dir_ok
echo [lcsas-restore] the share cards in LCSAS_SHARES_DIR did not rebuild
echo                 the password:
type "%SH_DIR%\keyshare.txt"
rmdir /s /q "%SH_DIR%" 2>nul
goto :pw_next
:shares_dir_bad
if defined SH_DIR rmdir /s /q "%SH_DIR%" 2>nul
echo [lcsas-restore] cannot use the share cards in LCSAS_SHARES_DIR:
echo                 !LCSAS_SHARES_DIR!
goto :pw_next
:shares_dir_ok
rmdir /s /q "%SH_DIR%" 2>nul
echo [lcsas-restore] rebuilt the password from the share cards in LCSAS_SHARES_DIR
set "PW_FROM=shares"
goto :pw_check

:shares_ask
REM Share entry (C7).  Each card goes in a file of its own, in a new
REM folder under %TEMP% (never the tools folder, which outlives the run),
REM and is checked alone with lcsas-keyshare <file>: file mode names a
REM mistyped word (exit 3), so only that card is asked again.  Then all
REM K are combined into PWFILE, and the folder goes.
if not exist "%KS_EXE%" (
    echo [lcsas-restore] lcsas-keyshare.exe is missing, so the share cards
    echo                 cannot be used here: type the rebuilt password.
    set "PW_OWN=1"
    goto :pw_next
)
call :new_shares_dir
if not defined SH_DIR (
    echo [lcsas-restore] cannot make a folder for the share cards in %%TEMP%%:
    echo                 type the rebuilt password instead.
    set "PW_OWN=1"
    goto :pw_next
)
set "SID_TEXT="
if defined SPLIT_SID set "SID_TEXT= (Split ID !SPLIT_SID!)"
echo.
echo The password for !SET_NAME! is split into !SPLIT_N! share cards; you
echo need any !SPLIT_K! of them!SID_TEXT!.
echo If you have already rebuilt the password, type P then ENTER instead
echo of a card, and then the password.
set "SI=1"
:share_next
if %SI% gtr %SPLIT_K% goto :shares_combine
echo.
echo Share card %SI% of %SPLIT_K%: type all the words on one line, then ENTER
echo ^(or Q to quit^).
set "SHARE="
set /p "SHARE=Words of card %SI%: "
if not defined SHARE goto :share_empty
if /i "!SHARE!"=="q" goto :shares_gave_up
if /i "!SHARE!"=="p" goto :shares_own
set "PW_EMPTY=0"
REM One word cannot be a card (a card has 20 or 33), and `echo on`
REM would act on echo itself.
if "!SHARE!"=="!SHARE: =!" goto :share_one_word
> "%SH_DIR%\card%SI%.txt" echo !SHARE!
set "SHARE="
pushd "%SH_DIR%"
"%KS_EXE%" "card%SI%.txt" >nul 2> "keyshare.txt" <nul
set "KRC=%ERRORLEVEL%"
popd
REM 0: a card that holds the whole password; 1: a good card that needs
REM others (C1 amendment).  3: a word is wrong; 2: unreadable.
if "%KRC%"=="3" goto :share_bad
if "%KRC%"=="2" goto :share_bad
set /a SI+=1
goto :share_next

:share_one_word
echo That is not a whole card: type all of its words on one line.
goto :share_next

:share_empty
set /a PW_EMPTY+=1
if %PW_EMPTY% geq 3 goto :shares_gave_up
echo Type the words of share card %SI%, or Q then ENTER to quit.
goto :share_next

:share_bad
echo.
type "%SH_DIR%\keyshare.txt"
echo Share card %SI% was not typed right: the line above says where.
echo Check each word against the card and type the card again.
del "%SH_DIR%\card%SI%.txt" 2>nul
goto :share_next

:shares_combine
set "SH_ARGS="
for /l %%I in (1,1,%SPLIT_K%) do set "SH_ARGS=!SH_ARGS! card%%I.txt"
del /f /q "%PWFILE%" >nul 2>nul
pushd "%SH_DIR%"
"%KS_EXE%" %SH_ARGS% > "%PWFILE%" 2> "keyshare.txt" <nul
set "KRC=%ERRORLEVEL%"
popd
if "%KRC%"=="0" goto :shares_rebuilt
del /f /q "%PWFILE%" >nul 2>nul
rmdir /s /q "%SH_DIR%" 2>nul
echo.
echo Those share cards do not fit together, so they do not rebuild the
echo password.  Every card must have the same Split ID and belong to
echo !SET_NAME!, and no card may be typed twice.
set "SH_AGAIN="
set /p "SH_AGAIN=Press ENTER to type the cards again, or Q then ENTER to quit: "
if /i "!SH_AGAIN!"=="q" goto :pw_gave_up
goto :shares_ask

:shares_rebuilt
rmdir /s /q "%SH_DIR%" 2>nul
echo [lcsas-restore] the share cards rebuilt the password.
set "PW_FROM=shares"
goto :pw_check

:shares_own
rmdir /s /q "%SH_DIR%" 2>nul
set "PW_OWN=1"
goto :pw_next

:shares_gave_up
rmdir /s /q "%SH_DIR%" 2>nul
goto :pw_gave_up

:pw_check
REM Try the password before the restore (#608).  77 = a key file
REM rejected it; anything else is left to the restore itself to report.
set "BIN=%RECOVERY%\bin\%ARCH%\lcsas-restore.exe"
if not exist "%BIN%" goto :pw_ok
echo [lcsas-restore] checking the password ...
"%BIN%" --repo "%REPO%" --password-file "%PWFILE%" --list-snapshots >nul 2>nul <nul
set "PW_RC=%ERRORLEVEL%"
if not "%PW_RC%"=="77" goto :pw_ok
del /f /q "%PWFILE%" >nul 2>nul
set "PW_WRONG=1"
echo.
if "%PW_FROM%"=="shares" goto :pw_wrong_shares
echo That password is wrong: it does not open the backup set !SET_NAME!.
echo Check it and type it again, or type Q then ENTER to quit.
goto :pw_next
:pw_wrong_shares
echo The password rebuilt from those share cards does not open the backup
echo set !SET_NAME!.  Check that every card belongs to it.
goto :pw_next

:pw_gave_up
del /f /q "%PWFILE%" >nul 2>nul
echo.
echo ============================================================
if defined PW_WRONG echo  ERROR: could not decrypt the repository with that password.
if not defined PW_WRONG echo  No password was given, so nothing was restored.
if defined RESUME_CMD echo  To try again, run "!RESUME_CMD!"
if not defined RESUME_CMD echo  To try again, run restore.bat again.
echo ============================================================
pause
goto :wrong_password

:pw_ok

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
        del /f /q "%PWFILE%" >nul 2>nul
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
        del /f /q "%PWFILE%" >nul 2>nul
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
    del /f /q "%PWFILE%" >nul 2>nul
    if !RC! equ 0 goto :restore_done
    echo [tier 2] failed with exit code !RC!.
)

del /f /q "%PWFILE%" >nul 2>nul
echo.
echo ============================================================
echo  ERROR: no working recovery method on this system.
echo.
echo  Looked for:
echo    !RECOVERY!\bin\%ARCH%\lcsas-restore.exe
echo    !RECOVERY!\bin\%ARCH%\rustic-static.exe
echo.
echo  A manual restore with the tier-3 Python reader on the disc
echo  (requires a Python 3 install on this host) is described in
echo  !RECOVERY!\docs\RECOVER_WINDOWS.txt -- this .bat does not
echo  launch it for you.
echo ============================================================
pause
exit /b 1

REM ----- The restore has finished -------------------------------------
REM Reached only via `goto` from tier 1 or tier 2 when they exit 0.
:restore_done
del /f /q "%PWFILE%" >nul 2>nul
echo.
echo ============================================================
echo  Restore complete.  Files restored to: !TARGET!
echo ============================================================
REM Another backup set from the same discs?  The drill restores one set,
REM then another.  Asked only when the archive has more than one, and
REM before anything is cleaned up: yes goes back to the menu with the
REM tools folder, the pack cache (the discs read so far) and the
REM catalog copy as they are.  Only no (or LCSAS_ANOTHER_SET=0) ends the
REM run, and only then is the tools folder deleted (C6).  The end of the
REM input reads as no.
set "ANOTHER_ANS="
if %NCAND% gtr 1 if not "%LCSAS_ANOTHER_SET%"=="0" (
    echo.
    set /p "ANOTHER_ANS=Restore another backup set from these discs? [y/N]: "
)
if /i "!ANOTHER_ANS!"=="y" goto :another_set
if /i "!ANOTHER_ANS!"=="yes" goto :another_set
:all_done
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
REM returned (#609).  Not relocated, the run kept its pack cache, its
REM catalog copy and any metadata\ it merged (#605) in TOOLS\in-place,
REM which goes; then the tools folder goes too if nothing else is in it
REM (a stopped restore's files stay, and .lcsas-tools with them).
if not defined LCSAS_RELOCATED goto :all_done_in_place
(> "%TOOLS%\.done" type nul) 2>nul
goto :all_done_end
:all_done_in_place
if /i "!TOOLS!"=="!TOOLS_TOP!\in-place" rmdir /s /q "!TOOLS!" 2>nul
del /f /q "!TOOLS_TOP!\.lcsas-tools" >nul 2>nul
rmdir "!TOOLS_TOP!" >nul 2>nul
if exist "!TOOLS_TOP!\" call :claim_tools "%TOOLS_TOP%"
:all_done_end
pause
exit /b 0

REM Reached only via `goto` from :restore_done: the reader wants another
REM backup set.  Back to the menu, which shows every set again; the next
REM one asks for its folder and its password.
:another_set
set "ANOTHER_PASS=1"
set "LCSAS_REPO="
set "RESUME="
set "RESUME_TARGET="
goto :choose_set

REM ----- Subroutines --------------------------------------------------

:write_pw
REM Write PW_VALUE into PWFILE, then forget it.  `echo !PW_VALUE!` runs
REM a password of exactly "on", "off" or "/?" as an echo argument and
REM writes no password (#482).  Those three go through `set /p`, which
REM writes them as-is (without a newline).  Not `echo(`: Windows takes
REM it, but wine writes a literal "(" and the wine tests drive this
REM script.
del /f /q "%PWFILE%" >nul 2>nul
set "PW_PLAIN_ECHO=1"
if /i "!PW_VALUE!"=="on" set "PW_PLAIN_ECHO="
if /i "!PW_VALUE!"=="off" set "PW_PLAIN_ECHO="
if "!PW_VALUE!"=="/?" set "PW_PLAIN_ECHO="
if defined PW_PLAIN_ECHO (
    > "%PWFILE%" echo !PW_VALUE!
) else (
    > "%PWFILE%" <nul set /p "=!PW_VALUE!"
)
set "PW_PLAIN_ECHO="
set "PW_VALUE="
goto :eof

:claim_tools
REM %~1 = the tools folder.  Make it, and write .lcsas-tools in it: the
REM folder is the restore's own, so it may be deleted once the restore
REM has finished (see "The tools folder" above).
if not exist "%~1\" mkdir "%~1" 2>nul
if exist "%~1\.lcsas-tools" goto :eof
(> "%~1\.lcsas-tools" echo restore.bat made this folder; it deletes it once the restore is done.) 2>nul
goto :eof

:use_tools
REM Make the tools folder (TOOLS_TOP, with .lcsas-tools) and TOOLS, its
REM in-place\ for a run that does not relocate, before the first file
REM goes in.
if not exist "%TOOLS_TOP%\.lcsas-tools" call :claim_tools "%TOOLS_TOP%"
if not exist "%TOOLS%\" mkdir "%TOOLS%" 2>nul
goto :eof

:archive_id
REM ARCHIVE_ID = when the META disc this script runs from was made: the
REM "created_at" of its volume_info.json, nothing without one.  The tools
REM folder keeps it in archive.txt, and only a run from the very same
REM META disc uses that folder as it is.
set "ARCHIVE_ID="
if not defined SRC_ROOT goto :eof
for %%I in ("%SRC_ROOT%\..") do set "AI_JSON=%%~fI"
if not "%AI_JSON:~-1%"=="\" set "AI_JSON=%AI_JSON%\"
set "AI_JSON=%AI_JSON%volume_info.json"
if not exist "%AI_JSON%" goto :eof
for /f "tokens=1,* delims=:" %%A in ('findstr /c:"created_at" "%AI_JSON%" 2^>nul') do if not defined ARCHIVE_ID set "ARCHIVE_ID=%%B"
if not defined ARCHIVE_ID goto :eof
set "ARCHIVE_ID=!ARCHIVE_ID:"=!"
set "ARCHIVE_ID=!ARCHIVE_ID: =!"
set "ARCHIVE_ID=!ARCHIVE_ID:,=!"
goto :eof

:new_shares_dir
REM SH_DIR = a new, empty folder under %TEMP% for share card files, or
REM nothing when it cannot be made.
set "SH_DIR=%TEMP%\lcsas-shares-%RANDOM%%RANDOM%"
rmdir /s /q "%SH_DIR%" 2>nul
mkdir "%SH_DIR%" 2>nul
if not exist "%SH_DIR%\" set "SH_DIR="
goto :eof

:find_newest
REM DATA_SEEN = a drive D: to Z: holds a data disc.  NEWEST_ROOT ("E:\")
REM and NEWEST_LABEL = the one whose label is newest, by year, then by
REM number (C2); nothing when no data disc has a label.
set "DATA_SEEN="
set "NEWEST_ROOT="
set "NEWEST_LABEL="
set "NEWEST_Y=-1"
set "NEWEST_S=-1"
for %%L in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do call :consider_disc %%L
goto :eof

:consider_disc
REM %1 = a drive letter.  A data disc, as for tier 1's search list below:
REM a data\ folder, or a volume_info.json and no
REM recovery\scripts\restore.bat (that is a META disc).
set "CD_DATA="
if exist "%1:\data\" set "CD_DATA=1"
if exist "%1:\volume_info.json" if not exist "%1:\recovery\scripts\restore.bat" set "CD_DATA=1"
if not defined CD_DATA goto :eof
set "DATA_SEEN=1"
call :read_label "%1:\"
if not defined RL_LABEL goto :eof
call :parse_label
if not defined PL_YEAR goto :eof
if %PL_YEAR% lss %NEWEST_Y% goto :eof
if %PL_YEAR% equ %NEWEST_Y% if %PL_SEQ% leq %NEWEST_S% goto :eof
set "NEWEST_ROOT=%1:\"
set "NEWEST_LABEL=!RL_LABEL!"
set "NEWEST_Y=%PL_YEAR%"
set "NEWEST_S=%PL_SEQ%"
goto :eof

:read_label
REM %~1 = a disc root ending in "\".  RL_LABEL = the disc's label: the
REM rest of the first "  Label:" line of its VOLUME_INFO.txt, which no
REM rc6 change moved (the backup sets' rows come after it, indented),
REM else volume_info.json's "label"; nothing when neither has one.
set "RL_LABEL="
REM Only the lines with "Label:" (or "label") in them reach the for body:
REM the other lines list the backup sets' names (see :read_marker).
if exist "%~1VOLUME_INFO.txt" for /f "tokens=1,*" %%A in ('findstr /c:"Label:" "%~1VOLUME_INFO.txt" 2^>nul') do if not defined RL_LABEL if "%%A"=="Label:" set "RL_LABEL=%%B"
if defined RL_LABEL goto :read_label_ok
if not exist "%~1volume_info.json" goto :eof
REM (The `if` inside the brackets: wine loses the closing bracket of
REM `do if ... (`.)
for /f "tokens=1,* delims=:" %%A in ('findstr /c:"label" "%~1volume_info.json" 2^>nul') do (
    set "JL_K=%%A"
    set "JL_V=%%B"
    if not defined RL_LABEL call :json_label_line
)
:read_label_ok
call :label_ok
goto :eof

:label_ok
REM RL_LABEL = a label read from a disc, or from merged-from.txt.  It is
REM kept only when it holds nothing but A-Z, a-z, 0-9 and "_", as every
REM label LCSAS writes does.  It is text from a file on a disc, put in
REM lines below with %%RL_LABEL%%: a quote in it would end the quoted
REM text there, and what follows would run.  So a quote is made "#"
REM first, with delayed expansion (which never reads the text again),
REM and the check then sees it.
if not defined RL_LABEL goto :eof
set "_T=!RL_LABEL:"=#!"
for /f "delims=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_" %%x in ("x%_T%") do set "RL_LABEL="
goto :eof

:json_label_line
REM JL_K, JL_V = a line of volume_info.json split at its first ":".  Set
REM RL_LABEL when JL_K is "label": what JL_V holds up to a "," or "}".
set "JL_K=!JL_K:"=!"
set "JL_K=!JL_K: =!"
set "JL_K=!JL_K:{=!"
if /i not "!JL_K!"=="label" goto :eof
set "JL_V=!JL_V:"=!"
set "JL_V=!JL_V:,= !"
set "JL_V=!JL_V:}= !"
for /f "tokens=1" %%v in ("%JL_V%") do set "RL_LABEL=%%v"
goto :eof

:parse_label
REM RL_LABEL = PREFIX_MEDIA_YYYY_SEQ.  PL_YEAR and PL_SEQ = its last two
REM "_" fields, read from the right (a prefix may hold "_"), as numbers;
REM both nothing when they are not numbers.
set "PL_REST=!RL_LABEL!"
set "PL_PREV="
set "PL_LAST="
:parse_label_next
set "PL_CUR=%PL_REST%"
set "PL_REST="
for /f "tokens=1,* delims=_" %%a in ("%PL_CUR%") do (
    set "PL_PREV=!PL_LAST!"
    set "PL_LAST=%%a"
    set "PL_REST=%%b"
)
if defined PL_REST goto :parse_label_next
call :to_number "%PL_LAST%" PL_SEQ
call :to_number "%PL_PREV%" PL_YEAR
if not defined PL_SEQ set "PL_YEAR="
if not defined PL_YEAR set "PL_SEQ="
goto :eof

:to_number
REM %~1 = text, %2 = a variable's name.  Sets it to the text without its
REM leading zeros when the text is all digits (so `if` does not read it
REM as octal), else to nothing.
set "%2="
set "TN=%~1"
if not defined TN goto :eof
set "TN_BAD="
for /f "delims=0123456789" %%x in ("%TN%") do set "TN_BAD=1"
if defined TN_BAD goto :eof
for /f "tokens=* delims=0" %%x in ("%TN%") do set "TN=%%x"
set "%2=%TN%"
REM All zeros: the line above left it as it was.
if "%TN:~0,1%"=="0" set "%2=0"
goto :eof

:merge_newest
REM Merge the backup sets on the newest data disc, NEWEST_ROOT, into the
REM tools folder's metadata\ (C2; see "The newest data disc" above).
REM Relocated, that folder holds the META disc's copy already.  A run
REM that is not relocated makes it from the copy beside this script, so
REM that copy is never changed (it may be the reader's own).
REM Never from an older disc than an earlier run of this tools folder
REM read (merged-from.txt): that would go back to an older snapshot.
set "MF_LABEL="
if exist "%TOOLS%\merged-from.txt" for /f "usebackq delims=" %%A in ("%TOOLS%\merged-from.txt") do if not defined MF_LABEL set "MF_LABEL=%%A"
if not defined MF_LABEL goto :merge_fresh
if not defined LCSAS_RELOCATED goto :merge_fresh
set "RL_LABEL=!MF_LABEL!"
call :label_ok
call :parse_label
if not defined PL_YEAR goto :merge_fresh
if %PL_YEAR% lss %NEWEST_Y% goto :merge_fresh
if %PL_YEAR% equ %NEWEST_Y% if %PL_SEQ% leq %NEWEST_S% goto :merge_fresh
echo [lcsas-restore] keeping the backup sets read from !MF_LABEL!, a newer disc
set "NEWEST_ROOT="
goto :eof
:merge_fresh
if not exist "!NEWEST_ROOT!metadata\" goto :eof
if defined LCSAS_RELOCATED goto :merge_into
REM Not relocated: TOOLS is TOOLS\in-place, this run's own.
call :use_tools
if exist "%TOOLS%\metadata\" rmdir /s /q "%TOOLS%\metadata" 2>nul
del /f /q "%TOOLS%\merged-from.txt" >nul 2>nul
mkdir "%TOOLS%\metadata" 2>nul
if not exist "%TOOLS%\metadata\" goto :merge_cannot
set "METADATA_SET_HERE=1"
if exist "%DISC_ROOT_S%metadata\" for /f "delims=" %%T in ('dir /ad /b "%DISC_ROOT_S%metadata" 2^>nul') do xcopy /e /i /q /y "%DISC_ROOT_S%metadata\%%T" "!TOOLS!\metadata\%%T" >nul 2>nul <nul
:merge_into
if not exist "%TOOLS%\metadata\" mkdir "%TOOLS%\metadata" 2>nul
if not exist "%TOOLS%\metadata\" goto :merge_cannot
echo [lcsas-restore] reading the backup sets on !NEWEST_LABEL!:
for /f "delims=" %%T in ('dir /ad /b "!NEWEST_ROOT!metadata" 2^>nul') do call :merge_set "%%T"
if exist "%TOOLS%\metadata-new\" rmdir /s /q "%TOOLS%\metadata-new" 2>nul
(> "%TOOLS%\merged-from.txt" echo !NEWEST_LABEL!) 2>nul
goto :eof
:merge_cannot
echo [lcsas-restore] cannot write to !TOOLS!, so the backup sets on the
echo                 META disc are used as they are.
set "METADATA_SET_HERE="
goto :eof

:merge_set
REM %~1 = a backup set's folder name on the newest data disc.  Its copy
REM is made whole in metadata-new\ first (outside metadata\, so a run
REM cut short leaves no half copy for the menu), with keys\ from the copy
REM it replaces added: keys are named by their content, so a name in
REM both is the same key.  Then the old copy moves aside and the new one
REM into place, and back again if that fails: a disc that cannot be read
REM leaves the copy that was there as it was.
set "MS_SRC=!NEWEST_ROOT!metadata\%~1"
set "MS_DST=!TOOLS!\metadata\%~1"
set "MS_NEW=!TOOLS!\metadata-new\%~1"
set "MS_OLD=!TOOLS!\metadata-new\%~1.old"
if not exist "!MS_SRC!\keys\" goto :eof
if not exist "!MS_SRC!\index\" goto :eof
echo                   metadata\%~1
rmdir /s /q "!MS_NEW!" 2>nul
rmdir /s /q "!MS_OLD!" 2>nul
xcopy /e /i /q /y "!MS_SRC!" "!MS_NEW!" >nul 2>nul <nul
if errorlevel 2 goto :merge_set_bad
if not exist "!MS_NEW!\index\" goto :merge_set_bad
if not exist "!MS_NEW!\keys\" goto :merge_set_bad
if exist "!MS_DST!\keys\" xcopy /e /i /q /y "!MS_DST!\keys" "!MS_NEW!\keys" >nul 2>nul <nul
if exist "!MS_DST!\" move "!MS_DST!" "!MS_OLD!" >nul 2>nul
if exist "!MS_DST!\" goto :merge_set_bad
move "!MS_NEW!" "!MS_DST!" >nul 2>nul
if exist "!MS_DST!\index\" goto :eof
if exist "!MS_OLD!\" move "!MS_OLD!" "!MS_DST!" >nul 2>nul
:merge_set_bad
echo [lcsas-restore] could not read metadata\%~1 on that disc; using
echo                 the copy from the META disc.
rmdir /s /q "!MS_NEW!" 2>nul
goto :eof

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
>>"%LAUNCHER%" echo (goto) 2^>nul ^& cd /d %%SystemDrive%%\ ^& call "%%TOOLS%%\recovery\scripts\restore.bat" --resume ^& call set "LCSAS_RC=%%%%ERRORLEVEL%%%%" ^& (if exist "%%TOOLS%%\.done" del /f /q "%%~f0") 2^>nul ^& (if exist "%%TOOLS%%\.done" if exist "%%TOOLS%%\.lcsas-tools" rmdir /s /q "%%TOOLS%%") 2^>nul ^& cmd /c exit %%%%LCSAS_RC%%%%
if not exist "%LAUNCHER%" goto :resume_unsaved
set "RESUME_CMD=%LAUNCHER%"
REM The Desktop: where Explorer has it (OneDrive may have moved it),
REM else %USERPROFILE%\Desktop.
set "DESKTOP="
for /f "tokens=1,2,*" %%A in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders" /v Desktop 2^>nul ^<nul') do if /i "%%A"=="Desktop" set "DESKTOP=%%C"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "!DESKTOP!\" goto :eof
copy /Y "%LAUNCHER%" "!DESKTOP!\Resume LCSAS restore.bat" >nul 2>nul
if exist "!DESKTOP!\Resume LCSAS restore.bat" set "DESK_LAUNCHER=!DESKTOP!\Resume LCSAS restore.bat"
goto :eof

:resume_unsaved
del /f /q "%CFG%" >nul 2>nul
set "RESUME_CMD="
goto :eof

REM Reached only via `goto` from the check of the tools folder at the
REM start, before anything in it has changed.
:tools_taken
echo ERROR: the tools folder !TOOLS!
echo is a folder that already holds other files, or a whole drive.  The
echo restore copies its programs there and deletes the folder once it
echo has finished, so it uses only a new or empty folder, or one it made
echo itself.  Set LCSAS_TOOLS_DIR to a new or empty folder, or leave it
echo unset to use the usual one, %%LOCALAPPDATA%%\LCSAS-restore.
pause
exit /b 1

REM Reached only via `goto` from the newest-data-disc step: Q (C5: 65).
:newest_quit
echo Stopped; nothing was restored.
pause
exit /b 65

REM Reached only via `goto` from the folder question: Q (C5: 65).  After
REM a set was restored, Q there means "no more", as at the menu.
:target_quit
if defined ANOTHER_PASS goto :all_done
echo Stopped; nothing was restored.
pause
exit /b 65

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
del /f /q "%PWFILE%" >nul 2>nul
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
