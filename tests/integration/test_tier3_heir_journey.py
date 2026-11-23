"""#967 item 3: tier 3 as an heir meets it, through the real restore.sh.

A META disc built from this checkout restores a real rustic set staged
by the real pipeline onto several discs, with tier 1 taken off the META disc
and the real rustic as tier 2's program (it cannot read a set spread
over discs, so it is skipped, and tier 3 runs), no OpenSSL for AES (``LCSAS_TIER3_AES=
pure``, the slow built-in AES an heir gets on a computer without one)
and a bundled ``zstandard`` that does not load in this Python (built for
another one).  What the heir reads must hold together:

- restore.sh says the bundled zstandard does not load, instead of
  "PYTHONPATH includes bundled zstandard" followed by the reader's "the
  'zstandard' package is not available to this Python".
- The tier-2 skip says what happens next in plain words ("trying the
  next tool"), not "falling through to tier 3".
- With the slow AES the reader says up front how long the set takes at
  that rate (the reviewer measured 9 MB in 75 s with no line at all),
  and its progress lines say of how much.

The lines printed by time while a slow restore works ("about <time>
left") depend on the computer's speed, so they are tested with a clock
the test sets: tests/recovery_hardening/test_tier3_progress.py.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.integration.scratch_install import ScratchInstall, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

#: Random bytes, so zstd cannot shrink them: small files, each backed
#: up on its own (a pack each, so the set spans several TEST_TINY discs).
_FILES = 12
_SIZE = 100_000


def _heir_tier3(inst: ScratchInstall, name: str, out: Path) -> subprocess.CompletedProcess[str]:
    """``restore.sh`` from a META disc with no tier 1 and a broken
    bundled zstandard, slow AES, no questions."""
    from lcsas.meta.builder import MetaVolumeBuilder

    work = inst.root / "heir"
    mnt = work / "mnt"
    mnt.mkdir(parents=True)
    for label, tree in sorted(inst.disc_trees().items()):
        shutil.copytree(tree, mnt / label)
    meta = mnt / "LCSAS_META"
    MetaVolumeBuilder(meta, catalog_db_path=None,
                      allow_no_dvdisaster_source=True).build()
    gone = [p for p in (meta / "recovery" / "bin").rglob("lcsas-restore*")]
    assert gone, "the META disc has no tier-1 program to take away"
    for p in gone:
        # Tier 2's program in its place: the real rustic.  It cannot
        # read a set spread over discs, so restore.sh skips it.
        shutil.copy2(shutil.which("rustic") or "rustic",
                     p.with_name("rustic-static"))
        p.unlink()
    # A zstandard built for another Python: the folder is there, the
    # import fails, as on an heir's computer with another Python.
    shutil.rmtree(meta / "tools" / "lib", ignore_errors=True)
    broken = meta / "tools" / "lib" / "python3.99" / "zstandard"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text(
        'raise ImportError("built for another Python")\n')
    (work / "home").mkdir()
    (work / "tmp").mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "LCSAS_MOUNT_DIRS": str(mnt),
        "LCSAS_TOOLS_DIR": str(work / "tools"),
        "LCSAS_NEWEST_DISC_PROMPT": "0",
        "LCSAS_ANOTHER_SET": "0",
        "LCSAS_INTERACTIVE": "off",
        "LCSAS_BACKUP_SET": name,
        "LCSAS_TIER3_AES": "pure",
    }
    return subprocess.run(
        ["sh", str(meta / "restore.sh"),
         "--password-file", str(inst.key(name)),
         "--restore-folder", str(out)],
        capture_output=True, text=True, env=env,
        stdin=subprocess.DEVNULL, timeout=600,
    )


def test_tier3_without_openssl_says_how_long_and_shows_progress(tmp_path: Path) -> None:
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    for n in range(_FILES):
        inst.backup("alpha", f"f{n}.bin", size=_SIZE)
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    (session,) = inst.sessions()
    inst.mark_burned(session)
    assert len(inst.disc_trees()) >= 2, "the set should span two discs"

    out = tmp_path / "restored"
    res = _heir_tier3(inst, "alpha", out)
    err = res.stderr
    assert res.returncode == 0, err
    assert tree_digest(out / "alpha") == tree_digest(inst.data("alpha")), err

    # The bundled zstandard: one story, not two.
    assert "PYTHONPATH includes bundled zstandard" not in err, err
    assert re.search(r"\[tier 3\] the bundled zstandard at \S+ does not load "
                     r"in this Python", err), err

    # Tier 2 is skipped in the heir's words.
    assert not re.search(r"\[tier 2\] skipped:.*falling through", err), err
    assert re.search(r"\[tier 2\] skipped: .*trying the next tool", err), err

    # The slow AES: the time it will take, said before the work starts.
    assert "slow built-in pure-Python AES" in err, err
    up_front = re.search(
        rf"This snapshot holds [\d.]+ MB in {_FILES} file\(s\)\. "
        r"At about 0\.15 MB/s "
        r"\(the slow built-in AES\) that takes about \d+ (second|minute)", err)
    assert up_front, err
    # Progress says of how much (the last line, at 100%, has no time left).
    last = re.search(
        rf"Progress: {_FILES}/{_FILES} files restored, ([\d.]+) MB of "
        r"([\d.]+) MB\.\n", err)
    assert last and last.group(1) == last.group(2), err
    assert up_front.start() < last.start() < err.index("Restore complete"), err

