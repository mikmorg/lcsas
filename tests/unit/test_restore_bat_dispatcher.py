"""Smoke tests for recovery/scripts/restore.bat target dispatch.

Windows `.bat` scripts can't be executed on Linux, so we settle for
static-content assertions: the file must contain the post-Phase-21.1
target string, must NOT carry the pre-Phase-21.1 ``x86_64-windows``
string, and must explicitly reject Windows ARM64 with a documented
workaround.
"""

from __future__ import annotations

from pathlib import Path

RESTORE_BAT = Path(__file__).resolve().parents[2] / "recovery" / "scripts" / "restore.bat"


def test_restore_bat_uses_new_target_name():
    """restore.bat must build paths under bin\\x86_64-pc-windows-gnu\\.

    This is the canonical target name from
    docs/CROSS_PLATFORM_META_RFC.md §3.  The meta-builder writes its
    bundle to bin\\x86_64-pc-windows-gnu\\, so the .bat must match.
    """
    content = RESTORE_BAT.read_text()
    assert "x86_64-pc-windows-gnu" in content


def test_restore_bat_does_not_use_legacy_arch_name():
    """The pre-Phase-21.1 ``x86_64-windows`` name no longer matches
    the bundled-binary directory layout and must not appear in the
    dispatcher.  Regression guard against an accidental revert.

    Note: docstrings or comment lines that reference the legacy name
    for context are still allowed; we just bar it from the active
    `set "ARCH=..."` assignments.
    """
    content = RESTORE_BAT.read_text()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("REM") or stripped.startswith("::"):
            continue
        assert "x86_64-windows" not in stripped, (
            f"legacy arch name still in restore.bat: {line!r}"
        )
        # Also catch the ARM64-as-supported regression.
        assert "set \"ARCH=aarch64-windows\"" not in stripped


def test_restore_bat_rejects_windows_arm64_with_explanation():
    """Windows ARM64 is a Phase 21.1 §6 Q1 deferred target — restore.bat
    must explicitly fail (not silently fall through to a non-existent
    binary) when run on it, and the message must mention winget or
    'install rustic' so the user knows what to do."""
    content = RESTORE_BAT.read_text()
    assert "ARM64" in content
    assert (
        "Windows ARM64 is not yet supported" in content
        or "ARM64 is not supported" in content
    )
    assert ("winget" in content) or ("install rustic" in content.lower())


def test_restore_bat_honors_lcsas_target_override():
    """The .bat must respect $LCSAS_TARGET so operators can override
    the auto-detected target (e.g. when running under emulation)."""
    content = RESTORE_BAT.read_text()
    assert "LCSAS_TARGET" in content


def test_restore_bat_probes_holographic_metadata_layout():
    """UX-01: restore.bat must discover repos under the holographic
    ``metadata\\<tenant>\\`` layout the meta-builder actually writes, and
    the single-drive RAM relocation must carry ``metadata`` along so
    discovery still works after the meta disc is ejected.

    The pre-UX-01 dispatcher probed only ``%RECOVERY%\\repo`` and
    ``%RECOVERY%`` — neither of which ever exists on a real meta-volume —
    and the relocation copied only ``bin`` + ``catalog.db``.  This guard
    bites against that regression.
    """
    content = RESTORE_BAT.read_text()

    # The holographic per-tenant metadata\ tree must be enumerated.  The
    # implementation walks metadata\<tenant>\ subdirs via the :scan_metadata
    # helper (which uses `dir /ad /b` — `for /d` globbing is unreliable
    # across CMD interpreters, notably wine).
    assert ":scan_metadata" in content, "no :scan_metadata repo probe helper"
    assert "%RECOVERY%\\metadata" in content, (
        "this-volume metadata\\ probe missing (relocated-RAM case)"
    )
    assert "%DISC_ROOT%\\metadata" in content, (
        "disc-root metadata\\ probe missing"
    )

    # The drive-letter scan (D..Z) for other mounted discs must be present,
    # so a data disc carrying metadata\<tenant>\ is discoverable when the
    # meta disc itself ships no repo.
    assert "%%L:\\metadata" in content, (
        "no per-drive-letter metadata probe (other mounted discs)"
    )

    # LCSAS_REPO must select a tenant (parity with restore.sh).
    assert "LCSAS_REPO" in content

    # The relocation block must copy metadata into the tools folder
    # (#609: metadata\<id>\ at its top, where the copy's DISC_ROOT is).
    assert "!TOOLS!\\metadata\\%%T" in content, (
        "single-drive relocation does not carry metadata\\ into the tools folder"
    )

    # The dead-end "no restic repo" message must be gone in favour of the
    # actionable multi-path error.
    assert "could not find an LCSAS backup set" in content
    assert "no restic repo" not in content, (
        "the dead-end 'no restic repo' message must be replaced"
    )


def test_restore_bat_warns_before_overwriting_nonempty_target():
    """UX-07: restore.bat must guard a non-empty, non-LCSAS target.

    Functional .bat coverage lives in INFRA-01's Windows e2e
    (``windows-e2e.yml``) and the local wine suite; this is the cheap
    static check that the load-bearing strings are present: the hidden
    marker filename, the YES confirmation prompt, the override env var,
    and the distinct abort exit code (65).  The guard must sit BEFORE the
    password prompt so the heir is stopped before typing a secret.
    """
    content = RESTORE_BAT.read_text()

    assert ".lcsas-restore-marker" in content, "marker filename missing"
    assert "Type YES" in content, "YES confirmation prompt missing"
    assert "LCSAS_FORCE_NONEMPTY_TARGET" in content, "override env var missing"
    assert "exit /b 65" in content, "distinct abort exit code (65) missing"

    # The guard must precede the password prompt.
    guard_at = content.find(".lcsas-restore-marker")
    pw_at = content.find('set /p "LCSAS_PW=Password:')
    assert guard_at != -1 and pw_at != -1
    assert guard_at < pw_at, "non-empty-target guard must come before the password prompt"


def _code_lines(text: str, comment_prefixes: tuple[str, ...] = ("REM",)) -> list[str]:
    """Non-blank, non-comment lines of *text*, stripped."""
    lines = (ln.strip() for ln in text.splitlines())
    return [ln for ln in lines if ln and not ln.startswith(comment_prefixes)]


def _relocation_block(content: str) -> str:
    start = content.index("if defined LCSAS_RELOCATED goto :after_relocate")
    end = content.index("\n:after_relocate\n")
    return content[start:end]


def test_restore_bat_relocation_copies_bin_for_both_script_locations():
    """#531: the meta disc carries restore.bat twice, at the disc root
    (E:\\restore.bat, what the heir double-clicks) and at
    recovery\\scripts\\.  The relocation used to mirror "%~dp0..\\bin",
    which is E:\\bin from the root copy, so the RAM copy had no binaries
    and the restore died with "no working recovery method".  The copy
    must come from a root resolved for both layouts, and an empty copy
    must not be launched.  windows-e2e's root relocation leg is the
    functional proof; this is the cheap guard against a revert.

    Updated for #612: the programs are copied one by one through
    :copy_one (from "%SRC_ROOT%\\<path>"), not as a robocopy/xcopy of
    the whole bin tree, and the check after the copy looks at this
    computer's bin\\<arch>\\ only."""
    content = RESTORE_BAT.read_text()
    block = _relocation_block(content)
    copy_loop = (
        'for %%F in (lcsas-restore.exe lcsas-keyshare.exe lcsas-ecc.exe %T2_EXE%) '
        'do call :copy_one "bin\\%ARCH%\\%%F"'
    )
    code = _code_lines(block)
    assert copy_loop in code, "relocation no longer copies the programs"
    first_copy = block.index(copy_loop)

    # :copy_one copies from the resolved root, never from %~dp0..\bin.
    sub = _label_body(content, ":copy_one")
    assert 'copy /Y "%SRC_ROOT%\\%~1" "%TOOLS%\\recovery\\%~1" >nul 2>nul' in sub, sub
    assert not any("%~dp0..\\bin" in ln for ln in sub)

    # Both layouts are probed before the first copy.
    assert 0 <= block.find('if exist "%~dp0..\\bin\\"') < first_copy
    assert 0 <= block.find('if exist "%~dp0recovery\\bin\\"') < first_copy

    # A copy with no .exe falls back to running from the disc.
    check = block.find('dir /b "%TOOLS%\\recovery\\bin\\%ARCH%\\*.exe"')
    assert check > first_copy, "no empty-copy check after the bin copy"
    assert "goto :after_relocate" in block[check:block.index("copied recovery files to")]


def test_restore_bat_relaunch_reads_nothing_more_from_the_disc():
    """#532: while a batch file runs, cmd goes back to it when a `call`
    returns, even with `exit /b` on the same line or in a ( ) block, so
    after the heir ejected the meta disc a good restore ended in "The
    system cannot find the path specified." and exit 1.  Measured on
    windows-latest: only ending the disc copy with `(goto) 2>nul` BEFORE
    the call avoids it and keeps the relocated copy's exit code.  Ending
    the batch undoes setlocal (the cwd goes back to the disc), so the cd
    and the LCSAS_RELOCATED sentinel belong on the same line.
    """
    block = _relocation_block(RESTORE_BAT.read_text())
    code = _code_lines(block)
    assert code[-1].startswith(
        '(goto) 2>nul & cd /d %SystemDrive%\\ & set "LCSAS_RELOCATED=%~d0\\" '
        '& call "%TOOLS%\\recovery\\scripts\\restore.bat" %* & '
    ), code[-1]
    assert not any(ln.startswith('call "%TOOLS%') for ln in code), code
    # The reuse branch jumps straight to the same line (#609).
    assert code[-2:-1] == [":relaunch"], code[-3:]


def test_restore_bat_relaunch_removes_the_copy_and_keeps_its_exit_code():
    """#545: every relocated run left its copy, tens of MB, in %TEMP%.
    The disc copy's relaunch line removes it once the relocated copy
    returns.  Any command after the `call` replaces the exit code (a
    trailing `set` turned 7 into 0 on windows-latest), so the code is
    kept first and handed back by `cmd /c exit` as the very last
    command.  windows-e2e's relocated wrong-password leg is the
    functional proof (exit 77 through the relocation); this guards the
    order.

    Updated for #609: the copy now lives in the fixed tools folder and
    goes only when the restore finished, which the copy marks with
    TOOLS\\.done.  A stopped restore keeps it for "Resume LCSAS
    restore.bat".  The `if` is in parentheses: bare, it would take the
    `& cmd /c exit` after it as part of its own command.

    Updated (B2 review): and only when the folder is the restore's own,
    with .lcsas-tools in it (the check at the start refuses any other)."""
    line = _code_lines(_relocation_block(RESTORE_BAT.read_text()))[-1]
    cmds = [c.strip() for c in line.split(" & ")]
    call = cmds.index('call "%TOOLS%\\recovery\\scripts\\restore.bat" %*')
    assert cmds[call + 1:] == [
        'call set "LCSAS_RC=%%ERRORLEVEL%%"',
        '(if exist "%TOOLS%\\.done" if exist "%TOOLS%\\.lcsas-tools" '
        'rmdir /s /q "%TOOLS%") 2>nul',
        "cmd /c exit %%LCSAS_RC%%",
    ], cmds


def test_restore_bat_takes_the_relocation_sentinel_out_of_the_callers_env():
    """#532: the relaunch sets LCSAS_RELOCATED in the CALLER's environment
    (the disc copy has already ended).  The relocated copy must take it
    into its own setlocal scope and clear it outside, or a second run from
    the same prompt would skip relocation."""
    content = RESTORE_BAT.read_text()
    code = _code_lines(content, ("REM", "@"))
    assert code[0] == (
        'for %%R in ("%LCSAS_RELOCATED%") do (set "LCSAS_RELOCATED=" & '
        'setlocal enabledelayedexpansion & set "LCSAS_RELOCATED=%%~R")'
    ), code[0]
    assert "setlocal enabledelayedexpansion" not in code[1:], (
        "a second setlocal would bring the caller's LCSAS_RELOCATED back"
    )


def _code(content: str) -> list[str]:
    return _code_lines(content, ("REM", "@"))


def test_restore_bat_searches_the_meta_disc_letter_after_a_swap():
    """#604: on a machine with ONE optical drive every data disc goes into
    the META disc's own letter.  Up to rc5 restore.bat skipped that letter
    (SKIP_L), so tier 1 never looked there again.  It must now be passed
    as --pack-search, and as --mount-parent once the pack cache exists,
    as the very same string as --meta-disc (tier 1 compares them byte for
    byte; --meta-disc makes it skip the letter only while the META disc
    is in).  Functional proof: test_restore_bat_single_drive_wine.py and
    windows-e2e's single-drive leg."""
    content = RESTORE_BAT.read_text()
    code = _code(content)
    assert "SKIP_L" not in content, "the meta-letter skip is back (#604)"
    assert 'if defined LCSAS_RELOCATED set "DISC_LETTERS= %LCSAS_RELOCATED%"' in code
    assert 'if defined LCSAS_RELOCATED set "META_DISC_ARG=--meta-disc %LCSAS_RELOCATED%"' in code
    loop = code.index("for %%D in (%DISC_LETTERS%) do (")
    assert code[loop + 1] == 'set "PACK_SEARCH_ARGS=!PACK_SEARCH_ARGS! --pack-search %%D"'
    assert code[loop + 2] == (
        'if defined CACHE_OK set "PACK_SEARCH_ARGS=!PACK_SEARCH_ARGS! --mount-parent %%D"'
    )
    # --mount-parent nowhere else: without a cache tier 1 would open a
    # disc's catalog.db in place and hold the disc (#610).
    mounts = [ln for ln in code if "--mount-parent" in ln]
    assert mounts == [code[loop + 2]], mounts
    # The letters stay unquoted: "X:\" would end in \" (#615).
    assert not any('"--pack-search' in ln or '--pack-search "' in ln for ln in code)


def test_restore_bat_sets_up_the_pack_cache_before_tier1():
    """#610: Windows tier 1 ran without LCSAS_PACK_CACHE_DIR, so discs went
    back and forth.  restore.bat must set it (unless the caller did) and
    make the folder itself before tier 1 starts."""
    content = RESTORE_BAT.read_text()
    tier1 = content.index("----- Tier 1:")
    head = content[:tier1]
    assert 'set "LCSAS_PACK_CACHE_DIR=%TOOLS%\\pack-cache"' in head
    assert 'mkdir "%LCSAS_PACK_CACHE_DIR%"' in head
    assert 'if exist "%LCSAS_PACK_CACHE_DIR%\\" set "CACHE_OK=1"' in head


def test_restore_bat_passes_a_local_copy_of_the_catalog():
    """#610: a catalog.db passed in place on a data disc stays open for the
    whole restore and holds that disc in the drive.  Tier 1 gets a copy
    in TOOLS, never a disc path."""
    code = _code(RESTORE_BAT.read_text())
    args = [ln for ln in code if "CATALOG_ARG=--catalog" in ln]
    assert args == ['set "CATALOG_ARG=--catalog "!CATALOG_COPY!""'], args
    assert 'set "CATALOG_COPY=%TOOLS%\\catalog.db"' in code
    assert 'copy /Y "%CATALOG_PICK%" "%CATALOG_COPY%" >nul 2>nul' in code


def test_restore_bat_passes_lcsas_interactive_through():
    """LCSAS_INTERACTIVE reaches tier 1 as --interactive, so a harness that
    feeds the disc prompt through a pipe can make tier 1 ask."""
    content = RESTORE_BAT.read_text()
    for value in ("on", "off", "auto"):
        assert f'set "INTERACTIVE_ARG=--interactive {value}"' in content
    tier1 = content[content.index("----- Tier 1:"):content.index("----- Tier 2:")]
    assert "%INTERACTIVE_ARG%" in tier1


def test_restore_bat_stops_without_tier2_for_a_metadata_only_set():
    """#615: after 'q' at the disc prompt (or any other tier-1 failure but
    77) restore.bat fell into tier 2, which reads packs only from the
    set's own data\\ folder.  A set under metadata\\<id>\\ has none, so
    tier 2 could only fail and then say "no working recovery method".
    The script must stop there instead, from a top-level label (an
    `exit /b` inside the tier-1 block does not reach `cmd /c`, #384)."""
    content = RESTORE_BAT.read_text()
    tier1 = content[content.index("----- Tier 1:"):content.index("----- Tier 2:")]
    stop = tier1.index('if not exist "%REPO%\\data\\" goto :tier1_stopped')
    assert stop < tier1.index("trying tier 2"), "the guard must come first"
    code = _code(content)
    label = code.index(":tier1_stopped")
    assert label > code.index(":wrong_password"), "must sit at top level, after the exits"
    body = code[label:]
    # Updated (B2 review): /f, as for every delete of the password file.
    assert 'del /f /q "%PWFILE%" >nul 2>nul' in body
    assert body[-1] == "exit /b %RC%", body


def test_restore_bat_target_cannot_end_in_a_backslash():
    """#615: --target "E:\\" ends in \\", which the program reads as a
    quote, so the folder swallowed every later argument; and a TARGET
    left in the environment won over the default on an empty answer.

    A whole drive is refused and the question asked again: tier 1 makes
    the folder with mkdir and accepts only "already exists", and Windows
    answers "access denied" for the top of a drive.  Functional proof:
    test_restore_bat_wine_smoke.py::test_whole_drive_target_is_asked_again."""
    code = _code(RESTORE_BAT.read_text())
    ask = code.index('set /p "TARGET=Restore to which folder? [%DEFAULT_TARGET%]: "')
    # Updated (B2 review): the line that says Q quits comes first.
    assert code[ask - 3:ask] == [
        ":ask_target",
        "echo Type a folder, or ENTER for the one in brackets; Q then ENTER quits.",
        'set "TARGET="']
    after = code[ask:ask + 20]
    assert 'if defined TARGET set "TARGET=!TARGET:"=!"' in after
    assert 'set "TARGET=!TARGET:~0,-1!"' in after
    drive = after.index('if "!TARGET:~-1!"==":" (')
    assert after.index('if not defined TARGET set "TARGET=%DEFAULT_TARGET%"') < drive
    refusal = after[drive:after.index(")", drive)]
    assert "goto :ask_target" in refusal, refusal
    assert not any(ln.startswith("set ") for ln in refusal), refusal
    targets = [ln for ln in code if ln.startswith('"%BIN%"')
               and ("--target " in ln or "restore latest " in ln)]
    assert len(targets) == 2 and all('"%TARGET%"' in ln for ln in targets), targets


def test_restore_bat_stop_banner_prints_the_folder_verbatim():
    """The banner after a tier-1 stop names the folder that holds the
    files restored so far.  `echo %TARGET%` is expanded before cmd splits
    the line, so a folder such as "Photos & Videos" was cut at the "&"
    and cmd tried to run "Videos".  `!TARGET!` is expanded after."""
    code = _code(RESTORE_BAT.read_text())
    body = code[code.index(":tier1_stopped"):]
    kept = body.index("echo  The files restored so far are kept in:")
    assert body[kept + 1] == "echo    !TARGET!", body[kept:kept + 2]


# ---------------------------------------------------------------------
# rc6 PR B2 (drill #476): the Windows journey from a disc.
# ---------------------------------------------------------------------


def _label_body(content: str, label: str) -> list[str]:
    """Code lines from ``label`` up to the next top-level ``goto :eof``
    or ``exit /b`` (the end of a subroutine or of a terminal label)."""
    code = _code(content)
    start = code.index(label)
    body = []
    for ln in code[start:]:
        body.append(ln)
        if ln in ("goto :eof",) or ln.startswith("exit /b"):
            break
    return body


def test_restore_bat_write_probes_send_errors_away_first():
    """#611: `> "%PROBE%" echo. 2>nul` printed "Access is denied." on every
    start from a disc: cmd reports a redirect it cannot open itself, and a
    2>nul after the `>` comes too late.  Under wine only the form with
    2>nul first was silent.  The marker in the restore folder had the
    same pattern.  Functional proof: the wine relocation test (a
    read-only META folder) and every windows-e2e relocation leg."""
    code = _code(RESTORE_BAT.read_text())
    assert '2>nul > "%PROBE%" echo.' in code
    assert '2>nul > "%MARKER%" echo.' in code
    for ln in code:
        for name in ("%PROBE%", "%MARKER%", '%TOOLS%\\.done'):
            if f'> "{name}' in ln:
                assert ln.startswith("2>nul >"), f"redirect error escapes: {ln}"


def test_restore_bat_warns_about_quickedit_before_the_copy():
    """#613: a click in the console window starts QuickEdit's "Select"
    mode, which pauses the restore, and nothing told the reader.  The
    note comes first, before the long copy, once (the relocated copy does
    not repeat it), and says how to get out of it.  QuickEdit is not
    switched off: that would change the reader's console settings."""
    content = RESTORE_BAT.read_text()
    note = content.index("PAUSES the restore")
    assert note < content.index('2>nul > "%PROBE%" echo.')
    assert note < content.index("call :copy_one")
    head = content[:note]
    assert head.rstrip().endswith("echo  A click selects text and"), head[-200:]
    guard = head.rindex("if not defined LCSAS_RELOCATED (")
    assert "Esc" in content[note:note + 300]
    assert "right-click" in content[note:note + 300]
    assert guard > content.index("setlocal enabledelayedexpansion")
    assert not any("reg add" in ln for ln in _code(content)), "no registry change"


def test_restore_bat_relocation_copies_only_this_platform():
    """#612: the relocation copied the whole recovery\\bin (744 MB, six
    kinds of computer) without a word.  It now copies bin\\<arch>\\'s three
    programs (rustic-static.exe only when tier 2 could run), this
    computer's guide and the metadata, and names each on screen.  ARCH is
    known before the relocation for that.  Functional proof:
    test_restore_bat_tools_folder_wine.py."""
    content = RESTORE_BAT.read_text()
    block = _relocation_block(content)
    code = _code_lines(block)
    assert content.index('set "ARCH=x86_64-pc-windows-gnu"') < content.index(
        "if defined LCSAS_RELOCATED goto :after_relocate")
    assert not any(ln.startswith("robocopy") or "where robocopy" in ln for ln in code)
    for ln in code:
        if ln.startswith(("xcopy", "copy")):
            assert "\\bin" not in ln, f"copies a whole bin folder: {ln}"
    assert 'call :copy_one "docs\\RECOVER_WINDOWS.txt"' in code
    # rustic-static.exe only for a set that holds its own data\.
    t2 = [ln for ln in code if 'set "T2_EXE=rustic-static.exe"' in ln]
    assert len(t2) == 1 and '\\data\\"' in t2[0], t2
    # The progress lines name what is copied.
    sub = _label_body(content, ":copy_one")
    assert "echo                   recovery\\%~1" in sub, sub
    assert "echo                   metadata\\%%T" in code
    # restore.bat is copied last: a copy cut short is never reused.
    last_copy = max(i for i, ln in enumerate(code)
                    if ln.startswith(("copy ", "xcopy ", "call :copy_one"))
                    or " call :copy_one " in ln)
    assert code[last_copy] == 'copy /Y "%~f0" "%TOOLS%\\recovery\\scripts\\restore.bat" >nul 2>nul'


def test_restore_bat_reuses_or_replaces_the_tools_folder():
    """#609 (C6): a tools folder with a byte-identical restore.bat is used
    as it is (`fc /b`), with its metadata, catalog copy and pack cache;
    otherwise what it holds is removed BY NAME (LCSAS_TOOLS_DIR may name a
    folder with other files) and copied afresh, keeping pack-cache\\ only
    when the tier-1 program is byte-identical.  wine's fc is a stub that
    answers "same" for any two files, so the replace branch is proved only
    on windows-e2e (Invoke-RelocationLeg -SeedStale)."""
    block = _relocation_block(RESTORE_BAT.read_text())
    code = _code_lines(block)
    reuse = code.index('fc /b "%~f0" "%TOOLS%\\recovery\\scripts\\restore.bat" >nul 2>nul <nul')
    assert code[reuse + 1] == "if errorlevel 1 goto :tools_fresh"
    # Updated (B2 review): and only a folder copied from this very META
    # disc -- its volume_info.json "created_at", kept in archive.txt.
    # Functional proof (wine, whose fc says "same" for any two files):
    # test_restore_bat_journey_wine.py::
    # test_tools_folder_from_another_meta_disc_is_replaced.
    assert code[reuse + 2:reuse + 6] == [
        "call :archive_id",
        'set "OLD_ARCHIVE="',
        'if exist "%TOOLS%\\archive.txt" for /f "usebackq delims=" %%A in '
        '("%TOOLS%\\archive.txt") do if not defined OLD_ARCHIVE '
        'set "OLD_ARCHIVE=%%A"',
        'if not "!OLD_ARCHIVE!"=="!ARCHIVE_ID!" goto :tools_fresh',
    ], code[reuse + 2:reuse + 6]
    # Reused: a set on this META disc that the copy lacks goes in (#605),
    # then the relaunch.  Nothing else.
    relaunch = code.index("goto :relaunch", reuse)
    assert code[reuse + 6].startswith(
        "echo [lcsas-restore] using the recovery files already copied")
    added = code[reuse + 7:relaunch]
    assert added[0].startswith("if defined META_SRC for /f ") and added[0].endswith(
        " do ("), added
    assert [ln for ln in added if "xcopy" in ln] == [
        'if not exist "!TOOLS!\\metadata\\%%T\\" xcopy /e /i /q /y "%META_SRC%\\%%T" '
        '"!TOOLS!\\metadata\\%%T" >nul 2>nul <nul'], added
    fresh = code.index(":tools_fresh")
    assert fresh > reuse
    same_exe = 'fc /b "!SRC_ROOT!\\bin\\%ARCH%\\lcsas-restore.exe"'
    keep = [ln for ln in code[fresh:] if ln.startswith(same_exe)]
    assert len(keep) == 1, code[fresh:]
    # Never a recursive delete of the folder itself before the restore.
    # Updated (B2 review): in the whole script, the only recursive deletes
    # of a tools folder are the relaunch line's (after .done, and only of
    # a folder with .lcsas-tools in it) and TOOLS\\in-place's at the end
    # of a run that did not relocate.
    whole = _code(RESTORE_BAT.read_text())
    for ln in whole:
        for name in ('"%TOOLS%"', '"!TOOLS!"', '"%TOOLS_TOP%"', '"!TOOLS_TOP!"'):
            if f"rmdir /s /q {name}" in ln:
                assert ln.startswith("(goto) 2>nul") or ln == (
                    'if /i "!TOOLS!"=="!TOOLS_TOP!\\in-place" '
                    'rmdir /s /q "!TOOLS!" 2>nul'), ln
    # A folder that is not the restore's own is refused (see
    # test_restore_bat_tools_folder_must_be_its_own).
    assert any("goto :tools_taken" in ln for ln in whole)


def test_restore_bat_tools_folder_default_and_override():
    """C4/C6: the tools folder is %LOCALAPPDATA%\\LCSAS-restore unless
    LCSAS_TOOLS_DIR names another; the copy in it finds it two folders up
    from itself.  Not %TEMP%: Disk Cleanup and Storage Sense empty it."""
    code = _code(RESTORE_BAT.read_text())
    assert 'if defined LOCALAPPDATA set "TOOLS=%LOCALAPPDATA%\\LCSAS-restore"' in code
    assert 'if defined LCSAS_TOOLS_DIR set "TOOLS=%LCSAS_TOOLS_DIR%"' in code
    assert 'set "TOOLS=%~dp0..\\.."' in code
    assert not any("%TEMP%\\lcsas-restore" in ln for ln in code)
    # The cache and the catalog copy are in it (#610).
    assert 'set "LCSAS_PACK_CACHE_DIR=%TOOLS%\\pack-cache"' in code


def test_restore_bat_tools_folder_goes_only_after_a_finished_restore():
    """#609 (C6): the tools folder is deleted only after rc 0, through
    TOOLS\\.done and the (goto) relaunch line: the copy runs from that
    folder and cannot delete it itself (cmd reads a running batch file
    again after a `call` returns).  A stale .done is removed first, so it
    cannot delete the folder after a stop."""
    content = RESTORE_BAT.read_text()
    code = _code(content)
    writes = [ln for ln in code if '"%TOOLS%\\.done" echo.' in ln]
    assert writes == ['2>nul > "%TOOLS%\\.done" echo.'], writes
    done = _label_body(content, ":restore_done")
    assert writes[0] in done, done
    assert code.index('del /f /q "%TOOLS%\\.done" >nul 2>nul') < code.index(
        "if defined LCSAS_RELOCATED goto :after_relocate")
    # The Desktop launcher goes at the same point.
    assert 'if defined DESK_LAUNCHER del /f /q "!DESK_LAUNCHER!" >nul 2>nul' in done


def test_restore_bat_q_at_the_disc_prompt_is_terminal():
    """C5 / #615: tier 1 exits 75 when the reader types q at the disc
    prompt ("paused by the reader").  That ends the run whatever the set's
    layout: keep the tools folder, print how to carry on, exit 75.  Tier 2
    must not run."""
    content = RESTORE_BAT.read_text()
    tier1 = content[content.index("----- Tier 1:"):content.index("----- Tier 2:")]
    stop = tier1.index("if !RC! equ 75 goto :tier1_stopped")
    assert stop < tier1.index('if not exist "%REPO%\\data\\" goto :tier1_stopped')
    assert stop < tier1.index("trying tier 2")
    body = _label_body(content, ":tier1_stopped")
    assert 'echo    "!RESUME_CMD!"' in body, body
    assert body[-1] == "exit /b %RC%"


def test_restore_bat_resume_cfg_never_holds_the_password():
    """#609 (C6): resume.cfg keeps the set, the folder, the META disc's
    letter and the letters searched -- NEVER the password.  It is written
    before the password prompt, so every stop after it can be carried on.
    Functional proof: test_restore_bat_tools_folder_wine.py reads the file
    for the password."""
    content = RESTORE_BAT.read_text()
    body = _label_body(content, ":save_resume")
    for ln in body:
        for word in ("LCSAS_PW", "PWFILE", "LCSAS_PASSWORD"):
            assert word not in ln, f"resume.cfg/launcher writer touches {word}: {ln}"
    cfg = [ln[ln.index("echo ") + 5:] for ln in body if '"%CFG%" echo ' in ln]
    keys = [c.split("=", 1)[0] for c in cfg if not c.startswith("#")]
    assert keys == ["set", "target", "meta", "letters"], cfg
    code = _code(content)
    assert code.index("if defined LCSAS_RELOCATED call :save_resume") < code.index(
        'set /p "LCSAS_PW=Password: "')


def _launcher_lines(content: str) -> list[str]:
    """The lines restore.bat writes into "Resume LCSAS restore.bat", as
    the file will hold them (carets and doubled percents undone)."""
    out = []
    for ln in _code(content):
        if not ln.startswith(('> "%LAUNCHER%" echo ', '>>"%LAUNCHER%" echo ')):
            continue
        text = ln.split(" echo ", 1)[1]
        if "!" not in text:
            text = text.replace("^&", "&").replace("^>", ">")
        # A batch line's %% is one % in the file it writes.
        out.append(text.replace("%%", "%"))
    return out


def test_restore_bat_resume_launcher_ends_itself_before_the_restore():
    """#609 (C6): "Resume LCSAS restore.bat" runs the tools copy with
    --resume.  Like the relaunch line on the disc, it ends its own batch
    with (goto) first, so the copy can delete the Desktop launcher, and it
    removes the tools folder (and itself) only once the copy has left
    .done there.  wine cannot run the (goto); the wine test runs the rest
    of this line as it stands."""
    lines = _launcher_lines(RESTORE_BAT.read_text())
    assert lines[0] == "@echo off"
    assert 'set "TOOLS=!TOOLS!"' in lines
    goto = [ln for ln in lines if ln.startswith("(goto) 2>nul & ")]
    assert len(goto) == 1, lines
    # Last: nothing after it is ever read (and wine, which runs on past a
    # (goto), then ends there too).
    assert lines[-1] == goto[0], lines
    cmds = [c.strip() for c in goto[0].split(" & ")]
    assert cmds == [
        "(goto) 2>nul",
        "cd /d %SystemDrive%\\",
        'call "%TOOLS%\\recovery\\scripts\\restore.bat" --resume',
        'call set "LCSAS_RC=%%ERRORLEVEL%%"',
        '(if exist "%TOOLS%\\.done" del /f /q "%~f0") 2>nul',
        # Updated (B2 review): only a folder with .lcsas-tools in it.
        '(if exist "%TOOLS%\\.done" if exist "%TOOLS%\\.lcsas-tools" '
        'rmdir /s /q "%TOOLS%") 2>nul',
        "cmd /c exit %%LCSAS_RC%%",
    ], cmds
    # The script deletes the Desktop copy itself on rc 0 (it may be the
    # one that ran), so the order above is safe from either copy.


def test_restore_bat_newest_disc_step_comes_before_the_menu():
    """#605 (C2, C7): before the set menu, a run from a META disc with no
    data disc in asks for the NEWEST data disc, in C7's words; Q quits
    with 65, S and LCSAS_NEWEST_DISC_PROMPT=0 skip it.  The label comes
    from VOLUME_INFO.txt's "  Label:" line (first one only), never from
    the drive.  Functional proof: test_restore_bat_journey_wine.py."""
    content = RESTORE_BAT.read_text()
    code = _code(content)
    step = code.index(":meta_known")
    ask = code.index('set /p "TARGET=Restore to which folder? [%DEFAULT_TARGET%]: "')
    assert step < code.index(":choose_set") < ask
    assert code.index('if /i "%~1"=="--check-disc" (') < step
    for line in (
        "echo  Take the META disc out and put in your NEWEST data disc: the one",
        "echo  with the latest year, and then the highest number, e.g.",
        "echo  LCSAS_CD700_2026_0002.  File Explorer shows only the first 16",
        "echo  characters of the label; the full label is printed on the disc.",
        "echo  Press ENTER when it is in; S then ENTER to skip ^(use the copies",
        "echo  on the META disc^); Q then ENTER to quit.",
    ):
        assert line in code, line
    assert 'if "%LCSAS_NEWEST_DISC_PROMPT%"=="0" goto :newest_done' in code
    assert "if defined RESUME goto :newest_done" in code
    quit_body = _label_body(content, ":newest_quit")
    assert quit_body[-1] == "exit /b 65", quit_body
    read = _label_body(content, ":read_label")
    assert any('"%%A"=="Label:"' in ln and "if not defined RL_LABEL" in ln
               for ln in read), read


def test_restore_bat_merges_only_index_and_snapshots_from_the_newest_disc():
    """#605 (C2): no robocopy union of index\\ or snapshots\\.  Each set on
    the newest disc is copied whole into metadata-new\\ (outside
    metadata\\), keys\\ of the copy it replaces are added, and only then
    is it moved into place."""
    content = RESTORE_BAT.read_text()
    assert "robocopy" not in "\n".join(_code(content)).lower()
    body = _label_body(content, ":merge_set")
    joined = "\n".join(body)
    assert 'xcopy /e /i /q /y "!MS_SRC!" "!MS_NEW!"' in joined
    assert 'xcopy /e /i /q /y "!MS_DST!\\keys" "!MS_NEW!\\keys"' in joined
    assert joined.index('"!MS_NEW!"') < joined.index('move "!MS_NEW!" "!MS_DST!"')
    assert 'set "MS_NEW=!TOOLS!\\metadata-new\\%~1"' in body


def test_restore_bat_menu_shows_name_cmd_and_never_reparses_it():
    """#606 (C1): the menu shows the marker's name_cmd and split, printed
    with delayed expansion (`call echo` would re-parse a name), and the
    marker's name= line is never put in a variable."""
    content = RESTORE_BAT.read_text()
    code = _code(content)
    assert not [ln for ln in code if ln.startswith("call echo") or " call echo " in ln]
    assert "echo    [%~1] !CAND_LABEL_%~1!" in code
    assert ('if defined MK_K set "_LB=!_LB! (password split: any !MK_K! '
            'of !MK_N! share cards)"') in code
    marker = "\n".join(_label_body(content, ":read_marker"))
    for key in ("id", "name_cmd", "split", "split_id"):
        assert f'if "%%A"=="{key}" set ' in marker, key
    assert 'if "%%A"=="name" set' not in marker


def test_restore_bat_tries_the_password_before_tier1():
    """#608 (C7): the password is tried with --list-snapshots (exit 77 on
    a wrong one) before tier 1 runs, and a wrong one asks again; an empty
    answer asks again, Q quits with 77."""
    content = RESTORE_BAT.read_text()
    code = _code(content)
    check = [ln for ln in code if "--list-snapshots" in ln]
    head = '"%BIN%" --repo "%REPO%" --password-file "%PWFILE%" '
    assert check == [head + "--list-snapshots >nul 2>nul <nul"], check
    tier1 = [i for i, ln in enumerate(code)
             if ln.startswith(head + '--target "%TARGET%" --snapshot latest ')]
    assert len(tier1) == 1, tier1
    assert code.index(check[0]) < tier1[0]
    assert code.index('set "LCSAS_PW="') < code.index('set /p "LCSAS_PW=Password: "')
    assert 'if /i "!LCSAS_PW!"=="q" goto :pw_gave_up' in code
    start = code.index(":pw_gave_up")
    gave_up = code[start:code.index("goto :wrong_password", start) + 1]
    assert "pause" in gave_up, gave_up
    assert not [ln for ln in gave_up if ln.startswith("goto :pw")], gave_up


def test_restore_bat_share_cards_live_in_temp_never_in_the_tools_folder():
    """#607 (C7): each typed card is a file of its own in a new folder
    under %TEMP%, checked alone with lcsas-keyshare, then all K are
    combined into PWFILE; P lets the reader type the password."""
    content = RESTORE_BAT.read_text()
    code = _code(content)
    new_dir = _label_body(content, ":new_shares_dir")
    assert 'set "SH_DIR=%TEMP%\\lcsas-shares-%RANDOM%%RANDOM%"' in new_dir
    assert not [ln for ln in code if "SH_DIR=" in ln and "TOOLS" in ln]
    assert '"%KS_EXE%" "card%SI%.txt" >nul 2> "keyshare.txt" <nul' in code
    assert '"%KS_EXE%" %SH_ARGS% > "%PWFILE%" 2> "keyshare.txt" <nul' in code
    assert 'if "%KRC%"=="3" goto :share_bad' in code
    assert 'if /i "!SHARE!"=="p" goto :shares_own' in code


def test_restore_bat_asks_for_another_set_before_cleaning_up():
    """After a good restore, "Restore another backup set from these
    discs?" comes before the pack cache goes and before .done is
    written (C6): yes goes back to the menu, keeping both."""
    content = RESTORE_BAT.read_text()
    done = _label_body(content, ":restore_done")
    ask = [i for i, ln in enumerate(done)
           if "Restore another backup set from these discs? [y/N]: " in ln]
    assert len(ask) == 1, done
    rm_cache = 'if defined CACHE_SET_HERE rmdir /s /q "!LCSAS_PACK_CACHE_DIR!" 2>nul'
    assert ask[0] < done.index(rm_cache)
    assert ask[0] < done.index('2>nul > "%TOOLS%\\.done" echo.')
    assert 'if /i "!ANOTHER_ANS!"=="y" goto :another_set' in done
    assert 'if not "%LCSAS_ANOTHER_SET%"=="0"' in "\n".join(done)


def test_restore_bat_c4_variables_are_documented():
    """C4: every variable restore.bat honours has a row in ENV_VARS.txt."""
    env_doc = (RESTORE_BAT.parents[1] / "docs" / "ENV_VARS.txt").read_text()
    content = RESTORE_BAT.read_text()
    for var in ("LCSAS_REPO", "LCSAS_TARGET", "LCSAS_PASSWORD", "LCSAS_PWFILE",
                "LCSAS_PW", "LCSAS_SHARES_DIR", "LCSAS_NEWEST_DISC_PROMPT",
                "LCSAS_TOOLS_DIR", "LCSAS_PACK_CACHE_DIR", "LCSAS_INTERACTIVE",
                "LCSAS_ANOTHER_SET"):
        assert var in content, var
        assert f"\n{var}\n" in env_doc, f"{var} has no entry in ENV_VARS.txt"


def test_restore_bat_tools_folder_must_be_its_own():
    """C4/C6 (B2 review): the tools folder is deleted once a restore has
    finished, so restore.bat refuses, before anything in it changes, a
    whole drive and any folder that holds files but no .lcsas-tools --
    for runs that relocate AND runs that do not.  The check used to run
    only for LCSAS_TOOLS_DIR, only on the relocation path, and only when
    the folder held no recovery\\ or pack-cache\\.  Functional proof:
    test_restore_bat_journey_wine.py::test_tools_folder_must_be_the_restores_own."""
    content = RESTORE_BAT.read_text()
    code = _code(content)
    start = code.index("if defined LCSAS_RELOCATED goto :tools_owned")
    assert code[start:start + 9] == [
        "if defined LCSAS_RELOCATED goto :tools_owned",
        "if defined RESUME goto :tools_owned",
        'if /i "%~1"=="--check-disc" goto :tools_owned',
        'if "%TOOLS:~-1%"==":" goto :tools_taken',
        'if not exist "%TOOLS%\\" goto :tools_owned',
        'if exist "%TOOLS%\\.lcsas-tools" goto :tools_owned',
        'dir /b /a "%TOOLS%" 2>nul | findstr "." >nul',
        "if not errorlevel 1 goto :tools_taken",
        ":tools_owned",
    ], code[start:start + 9]
    # Before anything is deleted in TOOLS, and before the relocation.
    assert start > code.index(':tools_known')
    assert start < code.index('del /f /q "%TOOLS%\\.done" >nul 2>nul')
    assert start < code.index("if defined LCSAS_RELOCATED goto :after_relocate")
    assert not any("LCSAS_TOOLS_DIR" in ln and "tools_taken" in ln for ln in code)
    # The copy marks the folder as the restore's own before it fills it.
    fresh = code.index(":tools_cleared")
    claim = code.index('call :claim_tools "%TOOLS%"', fresh)
    assert claim < code.index('mkdir "%TOOLS%\\recovery\\scripts" 2>nul', fresh)
    sub = _label_body(content, ":claim_tools")
    assert any(ln.startswith('2>nul > "%~1\\.lcsas-tools" echo ') for ln in sub), sub
    taken = _label_body(content, ":tools_taken")
    assert any("already holds other files, or a whole drive" in ln for ln in taken)
    assert taken[-1] == "exit /b 1"


def test_restore_bat_runs_that_do_not_relocate_use_in_place():
    """#609 (B2 review): a run that does not relocate (a META copy that
    can be written to, LCSAS_NO_RELOCATE=1) keeps its pack cache, catalog
    copy and merged metadata in TOOLS\\in-place.  In TOOLS itself it
    deleted a stopped restore's metadata\\, pack cache and catalog copy,
    so "Resume LCSAS restore.bat" then found no backup set."""
    content = RESTORE_BAT.read_text()
    code = _code(content)
    top = code.index('set "TOOLS_TOP=%TOOLS%"')
    assert code[top + 1] == 'if not defined LCSAS_RELOCATED set "TOOLS=%TOOLS%\\in-place"'
    assert code.index('if /i "%~1"=="--check-disc" (') < top < code.index(":meta_known")
    # Made before the first file goes in.
    assert code.index("call :use_tools") < code.index('set "CACHE_OK="')
    merge = _label_body(content, ":merge_fresh")
    assert merge.index("call :use_tools") < merge.index(
        'if exist "%TOOLS%\\metadata\\" rmdir /s /q "%TOOLS%\\metadata" 2>nul')
    assert not any('"%DISC_ROOT_S%"=="%TOOLS%\\"' in ln for ln in merge)
    done = _label_body(content, ":restore_done")
    i = done.index(":all_done_in_place")
    assert done[i + 1:i + 5] == [
        'if /i "!TOOLS!"=="!TOOLS_TOP!\\in-place" rmdir /s /q "!TOOLS!" 2>nul',
        'del /f /q "!TOOLS_TOP!\\.lcsas-tools" >nul 2>nul',
        'rmdir "!TOOLS_TOP!" >nul 2>nul',
        'if exist "!TOOLS_TOP!\\" call :claim_tools "%TOOLS_TOP%"',
    ], done[i:]


def test_restore_bat_carrying_on_keeps_the_newest_catalog_copy():
    """#609/#605 (B2 review): a restore that carries on (or a tools folder
    that read the newest data disc before, merged-from.txt) keeps the
    catalog copy in TOOLS -- the newest data disc's -- instead of copying
    whatever disc is in a drive over it: an older disc's catalog has no
    record of the packs burned after it.  The copy is deleted with /f: a
    copy from a disc is read-only.  Functional proof:
    test_restore_bat_journey_wine.py::
    test_three_discs_one_drive_carry_on_keeps_the_newest_catalog."""
    code = _code(RESTORE_BAT.read_text())
    newest = code.index('set "CATALOG_PICK=!NEWEST_ROOT!catalog.db"')
    keep = code.index(":catalog_not_newest")
    scan = code.index(":catalog_scan")
    assert newest < keep < scan < code.index(":catalog_picked")
    assert code[keep + 1:scan] == [
        'if not exist "%CATALOG_COPY%" goto :catalog_scan',
        'set "CATALOG_PICK=%CATALOG_COPY%"',
        "if defined RESUME goto :catalog_picked",
        'if exist "%TOOLS%\\merged-from.txt" goto :catalog_picked',
    ], code[keep:scan]
    assert 'del /f /q "%CATALOG_COPY%" >nul 2>nul' in code
    assert not [ln for ln in code if ln.startswith('del "%CATALOG_COPY%"')]
    copy = code.index('copy /Y "%CATALOG_PICK%" "%CATALOG_COPY%" >nul 2>nul')
    assert code[copy + 1] == 'attrib -r "%CATALOG_COPY%" >nul 2>nul <nul'


def test_restore_bat_password_file_goes_even_when_read_only():
    """#608 (B2 review): `copy` keeps a read-only mark, so a copy of a
    read-only LCSAS_PWFILE could not be deleted by plain `del` nor
    replaced by `> "%PWFILE%" echo`: the wrong password was tried again
    and again, and a right one stayed in %TEMP%.  Every delete of the
    password file is /f, and the mark comes off the copy.  A window
    closed mid-restore left the file there for good: the first pass
    removes every lcsas-pw-*.txt and lcsas-shares-* in %TEMP%."""
    code = _code(RESTORE_BAT.read_text())
    assert not [ln for ln in code if 'del "%PWFILE%"' in ln]
    assert len([ln for ln in code if 'del /f /q "%PWFILE%" >nul 2>nul' in ln]) >= 10
    copy = code.index('copy /b /y "!LCSAS_PWFILE!" "%PWFILE%" >nul 2>nul')
    assert code[copy + 1] == 'attrib -r "%PWFILE%" >nul 2>nul <nul'
    clean = code.index("if defined ANOTHER_PASS goto :temp_cleared")
    assert code[clean + 1:clean + 4] == [
        'del /f /q "%TEMP%\\lcsas-pw-*.txt" >nul 2>nul',
        "for /f \"delims=\" %%D in ('dir /ad /b \"%TEMP%\\lcsas-shares-*\" 2^>nul') "
        'do rmdir /s /q "%TEMP%\\%%D" 2>nul',
        ":temp_cleared",
    ]
    assert code[clean + 4].startswith('set "PWFILE=%TEMP%\\lcsas-pw-')


def test_restore_bat_disc_text_never_breaks_out_of_quotes():
    """#605/#606 (B2 review): a disc's label and a marker's name_cmd=,
    split= and split_id= are text from a disc, and were put in lines with
    %...% inside quotes: a '"' in them ended the quotes and the rest of
    the line ran.  Every such value has its quotes made "#" with delayed
    expansion (which never reads the text again) before any %...% use,
    and a label is kept only when it is [A-Za-z0-9_].  Functional proof:
    test_restore_bat_journey_wine.py::test_disc_text_with_a_quote_is_never_run."""
    content = RESTORE_BAT.read_text()
    read = _label_body(content, ":read_label")
    assert read[-2:] == ["call :label_ok", "goto :eof"], read
    ok = _label_body(content, ":label_ok")
    q = ok.index('set "_T=!RL_LABEL:"=#!"')
    check = [i for i, ln in enumerate(ok) if '%%x in ("x%_T%") do set "RL_LABEL="' in ln]
    assert check and check[0] == q + 1, ok
    assert "delims=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_" in ok[q + 1]
    code = _code(content)
    assert 'set "PL_REST=!RL_LABEL!"' in code
    assert 'set "NEWEST_LABEL=!RL_LABEL!"' in code
    assert not [ln for ln in code if "%RL_LABEL%" in ln or "%MF_LABEL%" in ln]
    mf = code.index('set "RL_LABEL=!MF_LABEL!"')
    assert code[mf + 1:mf + 3] == ["call :label_ok", "call :parse_label"]
    marker = code[code.index(":read_marker"):code.index(":marker_nosplit")]
    nc = marker.index('set "_T=!MK_NC: =!"')
    assert marker[nc + 1] == 'set "_T=!_T:"=#!"' and '("x%_T%")' in marker[nc + 2]
    sp = marker.index('set "_T=!MK_SPLIT:-of-= !"')
    assert marker[sp + 1] == 'set "_T=!_T:"=#!"' and '("%_T%")' in marker[sp + 2]
    sid = marker.index('if defined MK_SID set "MK_SID=!MK_SID:"=#!"')
    assert sid < marker.index('call :to_number "%MK_SID%" MK_SID_OK')
    # The banner: the set's folder (a name on a disc) never with %...%.
    assert "echo Repo:          !REPO!" in code
    assert not [ln for ln in code if ln.startswith("echo") and "%REPO%" in ln]


def test_restore_bat_folder_question_takes_q_and_a_whole_path():
    """#615 (B2 review): Q at the folder question quits (65, or "no
    more" after a set was restored), as at every other question: "q" used
    to become the folder C:\\q.  A folder with no drive is asked again.
    Functional proof: test_restore_bat_journey_wine.py::
    test_folder_question_takes_q_and_wants_a_whole_path."""
    content = RESTORE_BAT.read_text()
    code = _code(content)
    ask = code.index('set /p "TARGET=Restore to which folder? [%DEFAULT_TARGET%]: "')
    assert code[ask + 1] == 'if /i "!TARGET!"=="q" goto :target_quit'
    chosen = code.index(":target_chosen", ask)
    assert code[chosen - 7:chosen] == [
        'if "!TARGET:~1,1!"==":" goto :target_chosen',
        'if "!TARGET:~0,2!"=="\\\\" goto :target_chosen',
        "echo.",
        "echo  !TARGET! is not a whole path.  Type the folder with its drive,",
        "echo  for example C:\\restored",
        "echo.",
        "goto :ask_target",
    ], code[chosen - 7:chosen]
    body = _label_body(content, ":target_quit")
    assert body == [":target_quit", "if defined ANOTHER_PASS goto :all_done",
                    "echo Stopped; nothing was restored.", "pause", "exit /b 65"], body


def test_restore_bat_helpers_never_read_the_scripts_input():
    """Every program that needs no input reads <nul: xcopy, attrib, fc,
    reg, lcsas-keyshare and the --list-snapshots password check.

    On real Windows xcopy and attrib read the script's own standard
    input.  With the answers piped in (windows-e2e), the catalog copy's
    `attrib -r` swallowed every answer after the folder question, and
    the run ended "No password was given" (77); a probe run on CI
    showed it.  Wine's xcopy and attrib leave stdin alone, so the wine
    tests passed; only a static pin can hold this here.
    """
    import re

    calls = re.compile(
        r'(?i)(?:^|[\s(&\'])(?:xcopy|attrib|fc|reg)\s|^"%KS_EXE%"\s|--list-snapshots\b')
    lines = [
        ln.strip() for ln in RESTORE_BAT.read_text().splitlines()
        if ln.strip() and not ln.strip().upper().startswith(("REM", "::"))
    ]
    hits = [ln for ln in lines if calls.search(ln) and not ln.startswith("echo")]
    assert len(hits) >= 13, f"self-check: only {len(hits)} calls found:\n" + "\n".join(hits)
    missing = [ln for ln in hits
               if "<nul" not in ln.replace(" ", "") and "^<nul" not in ln]
    assert not missing, "these read the script's input:\n" + "\n".join(missing)
