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

    # The relocation block must copy metadata into the RAM dir.
    assert "%RAMDIR%\\recovery\\metadata" in content, (
        "single-drive relocation does not carry metadata\\ into RAM"
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
    """
    block = _relocation_block(RESTORE_BAT.read_text())
    copies = [
        line.strip() for line in block.splitlines()
        if line.strip().startswith(("robocopy", "xcopy"))
    ]
    bin_copies = [c for c in copies if "recovery\\bin" in c]
    assert bin_copies, "relocation no longer copies the bin tree"
    for c in bin_copies:
        assert "%~dp0..\\bin" not in c, f"bin copied from %~dp0..\\bin again: {c}"
        assert "!SRC_ROOT!\\bin" in c, f"bin not copied from SRC_ROOT: {c}"

    # Both layouts are probed before the first copy.
    first_copy = block.index(bin_copies[0])
    assert 0 <= block.find('if exist "%~dp0..\\bin\\"') < first_copy
    assert 0 <= block.find('if exist "%~dp0recovery\\bin\\"') < first_copy

    # A copy with no .exe falls back to running from the disc.
    check = block.find('dir /s /b "%RAMDIR%\\recovery\\bin\\*.exe"')
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
        '& call "%RAMDIR%\\recovery\\scripts\\restore.bat" %* & '
    ), code[-1]
    assert not any(ln.startswith('call "%RAMDIR%') for ln in code), code


def test_restore_bat_relaunch_removes_the_copy_and_keeps_its_exit_code():
    """#545: every relocated run left its copy, tens of MB, in %TEMP%.
    The disc copy's relaunch line removes it once the relocated copy
    returns.  Any command after the `call` replaces the exit code (a
    trailing `set` turned 7 into 0 on windows-latest), so the code is
    kept first and handed back by `cmd /c exit` as the very last
    command.  windows-e2e's relocated wrong-password leg is the
    functional proof (exit 77 through the relocation, no copy left);
    this guards the order.
    """
    line = _code_lines(_relocation_block(RESTORE_BAT.read_text()))[-1]
    cmds = [c.strip() for c in line.split(" & ")]
    call = cmds.index('call "%RAMDIR%\\recovery\\scripts\\restore.bat" %*')
    assert cmds[call + 1:] == [
        'call set "LCSAS_RC=%%ERRORLEVEL%%"',
        'rmdir /s /q "%RAMDIR%" 2>nul',
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
    assert 'del "%PWFILE%" 2>nul' in body
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
    assert code[ask - 2:ask] == [":ask_target", 'set "TARGET="']
    after = code[ask:ask + 16]
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
