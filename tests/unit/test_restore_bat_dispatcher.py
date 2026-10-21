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
    """#532: cmd reads a batch file one line at a time.  With the exit on
    the line after `call`, the outer instance read it from the meta disc
    after the relocated copy returned; the heir had ejected that disc, so
    a good restore ended in "The system cannot find the path specified."
    and exit 1.  The call and the exit must share one line, and the exit
    code must be read after the call runs (!ERRORLEVEL!, not %ERRORLEVEL%).
    """
    block = _relocation_block(RESTORE_BAT.read_text())
    calls = [
        line.strip() for line in block.splitlines()
        if line.strip().startswith('call "%RAMDIR%\\recovery\\scripts\\restore.bat"')
    ]
    assert len(calls) == 1, calls
    assert calls[0].endswith("%* & exit /b !ERRORLEVEL!"), calls[0]
