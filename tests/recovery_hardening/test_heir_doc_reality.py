"""Static guard: heir manuals give commands that work as typed (#605, #609, #618, #619).

FAILURE MODES CAUGHT
--------------------
The rc6 docs sweep was reviewed against the scripts and the tier-1
binary, and these claims were wrong although every doc test was green:

* ``--key repo.key`` and ``> repo.key``: restore.sh copies itself off the
  META disc, runs ``cd /`` and re-runs itself, so a relative --key file is
  looked up as ``/repo.key`` and the run stops with exit 2; and a META
  disc is read-only, so ``> repo.key`` run there fails.  Give full paths.
* ``metadata\\<name>`` and ``dir D:\\metadata`` in RECOVER_WINDOWS.txt:
  the folders are named by the set's id, a long code since rc6 (C1), so
  the listing shows no names.  RECOVER.txt already said ``<id>``.
* ``cd D:`` in cmd does not switch to drive D:, so the relative
  ``recovery\\bin\\...`` path on the next line was looked up on C:.
* ``lcsas-restore.exe --catalog ... --list-pending-packs`` alone exits 2:
  tier 1 checks --repo and --password-file before it looks at
  --list-pending-packs (restore.sh's own size check passes both, #535).
* restore-windows.md said lcsas-restore.exe embeds a UTF-8 manifest; the
  exe carries no resources at all, which hid the non-ASCII gap (#619).
* RECOVER.txt's first command restored into /tmp/restored, which is RAM on
  a live USB stick: restore.sh then asks "restore into RAM anyway?".

Static (Path.read_text and a PE header read) only: no binaries are run.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "recovery" / "docs"
RECOVER = DOCS / "RECOVER.txt"
RECOVER_WIN = DOCS / "RECOVER_WINDOWS.txt"
RESTORE_WIN_MD = REPO_ROOT / "docs" / "workflows" / "restore-windows.md"
MAIN_C = REPO_ROOT / "recovery" / "src" / "lcsas-restore" / "main.c"
WIN_EXE = REPO_ROOT / "recovery" / "bin" / "x86_64-windows" / "lcsas-restore.exe"

# Heir-facing text that quotes restore.sh / keyshare commands.
_HEIR_TEXTS = (
    RECOVER,
    RECOVER_WIN,
    REPO_ROOT / "src" / "lcsas" / "meta" / "keyshare_combine.py",
    REPO_ROOT / "src" / "lcsas" / "staging" / "metadata.py",
    REPO_ROOT / "docs" / "RECOVERY_GUIDE.md",
    REPO_ROOT / "docs" / "guides" / "recovery-runbook.md",
)

# A bare relative file name (no / ~ $ < or drive) after --key, or as the
# target of a > redirect: `--key repo.key`, `> repo.key`.
_REL_KEY = re.compile(r"--key\s+([A-Za-z0-9_.-]+\.(?:key|txt))\b")
_REL_REDIRECT = re.compile(r">\s*([A-Za-z0-9_.-]+\.key)\b")


def _joined_commands(text: str) -> list[str]:
    """Each line, with cmd ``^`` / sh ``\\`` continuation lines joined."""
    out: list[str] = []
    buf = ""
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.endswith(("^", "\\")):
            buf += stripped[:-1] + " "
            continue
        out.append(buf + stripped)
        buf = ""
    if buf:
        out.append(buf)
    return out


@pytest.mark.parametrize("path", _HEIR_TEXTS, ids=lambda p: p.name)
def test_key_files_are_given_by_full_path(path: Path) -> None:
    if not path.is_file():
        pytest.skip(f"{path.name} not in this tree")
    bad = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for rx in (_REL_KEY, _REL_REDIRECT):
            for m in rx.finditer(line):
                bad.append(f"{path.name}:{lineno}: {m.group(0)!r}")
    assert not bad, (
        "a password file given by a bare relative name: restore.sh runs "
        "`cd /` before it reads --key, and a META disc cannot be written "
        "to; use a full path such as ~/repo.key:\n" + "\n".join(bad)
    )


def test_recover_windows_names_metadata_folders_by_id() -> None:
    text = RECOVER_WIN.read_text(encoding="utf-8")
    assert "metadata\\<name>" not in text, (
        "RECOVER_WINDOWS.txt says metadata\\<name>: the folders are named "
        "by the set's id (C1); say metadata\\<id> and how to map ids to names"
    )
    assert "metadata\\<id>" in text
    assert 'findstr /s /b /c:"name="' in text, (
        "RECOVER_WINDOWS.txt must show how to see which metadata\\<id> "
        "folder is which set (LCSAS_REPO.txt name= line)"
    )


def test_cmd_cd_to_a_drive_is_not_used_to_switch_drives() -> None:
    """In cmd, `cd D:` prints D:'s current folder and stays on C:."""
    for path in (RECOVER_WIN, RESTORE_WIN_MD):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not re.fullmatch(r"\s*cd [A-Za-z]:\s*", line), (
                f"{path.name}:{lineno}: `cd X:` does not switch drives in "
                "cmd; type `X:` then `cd \\`, or `cd /d X:\\`"
            )


def test_list_pending_packs_commands_carry_repo_and_password_file() -> None:
    src = MAIN_C.read_text(encoding="utf-8")
    need = src.find("if (!repo_path || !pwfile)")
    lpp = src.find("if (list_pending_packs) {")
    if need == -1 or lpp == -1 or need > lpp:
        pytest.skip("tier 1 no longer checks --repo/--password-file first")
    checked = 0
    for path in (RECOVER, RECOVER_WIN, RESTORE_WIN_MD):
        for cmd in _joined_commands(path.read_text(encoding="utf-8")):
            if "lcsas-restore" not in cmd or "--list-pending-packs" not in cmd:
                continue
            checked += 1
            assert "--repo" in cmd and "--password-file" in cmd, (
                f"{path.name}: tier 1 exits 2 (usage) on this command, "
                f"which lacks --repo/--password-file: {cmd.strip()}"
            )
    assert checked, "no --list-pending-packs command found: scanner broken?"


def _pe_resource_dir_size(exe: Path) -> int:
    data = exe.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe:pe + 4] == b"PE\0\0", f"{exe} is not a PE file"
    magic = struct.unpack_from("<H", data, pe + 24)[0]
    dirs = pe + 24 + (112 if magic == 0x20B else 96)
    return struct.unpack_from("<II", data, dirs + 2 * 8)[1]


def test_windows_doc_does_not_claim_a_manifest_the_exe_lacks() -> None:
    if not WIN_EXE.is_file():
        pytest.skip("no Windows tier-1 binary in this tree")
    if _pe_resource_dir_size(WIN_EXE) != 0:
        pytest.skip("the exe has resources now; re-check the doc by hand")
    for path in (RESTORE_WIN_MD, RECOVER_WIN):
        text = path.read_text(encoding="utf-8")
        assert "embeds an application manifest" not in text, (
            f"{path.name} says lcsas-restore.exe embeds a UTF-8 manifest; "
            "it has no resources at all (#619)"
        )


def test_recover_txt_does_not_restore_into_tmp() -> None:
    """/tmp is RAM on a live USB stick and on many distros; restore.sh
    then asks "Continue restoring into RAM anyway?" after the password."""
    for lineno, line in enumerate(RECOVER.read_text(encoding="utf-8").splitlines(), 1):
        assert not re.search(r"restore\.sh\b.*\s/tmp/restored\b", line), (
            f"RECOVER.txt:{lineno}: restore into ~/restored, not /tmp: {line}"
        )
        assert "--target /tmp/restored" not in line, (
            f"RECOVER.txt:{lineno}: restore into ~/restored, not /tmp: {line}"
        )
