"""Issue #781: tiers 1, 2 and 3 restore escaped file names alike.

rustic stores a node name that holds '"', '\\', a control character or a
byte that is not valid UTF-8 in Go-quoted form (``quo\\"te``, ``tab\\t``,
``latin\\xe9``).  Tiers 1 and 3 restored that stored text as the name;
tier 2 (rustic) restored the real one.  This builds a real rustic repo
holding one file of each name class (plus a directory and a symlink with
such names), restores it with all three tiers, and requires every tier
to give back exactly the source names, bytes for bytes, and contents.

Runs by default; skips when rustic or the lcsas-restore binary is absent.
A name class the host file system cannot create (macOS refuses names that
are not valid UTF-8) is left out of the source.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    find_restore_bin,
    find_restored_root,
    restore_with_tier2,
)

pytestmark = pytest.mark.integration

# One name per class.  Every one of these is stored escaped by rustic,
# except the plain, the valid-UTF-8 and the raw-control ones.
NAMES: list[bytes] = [
    b"plain.txt",
    b'quo"te.txt',
    b"back\\slash.txt",
    b"tab\there.txt",
    b"nl\nhere.txt",
    b"cr\rhere",
    b"bel\x07-bs\x08-vt\x0b-ff\x0c",
    b"esc\x1bx",                 # stored raw by rustic, \x1b by restic
    b"x01\x01",
    b"del\x7fx",
    b"latin\xe9.txt",            # a Latin-1 byte: not valid UTF-8
    b"lone-\xc3",                # a truncated UTF-8 sequence
    b"surr-\xed\xa0\x80x",       # an encoded surrogate: not valid UTF-8
    b"utf8-\xc3\xa9.txt",        # valid UTF-8, copied as is
    b"fffd-\xef\xbf\xbd.txt",    # U+FFFD itself
    b"zw-\xe2\x80\x8bx",         # U+200B, unprintable but valid
    b"sq'tick`x",
    b"lit-\\x41",                # a literal backslash-x-4-1
    b"mixed\\\"\t\xff",
]
LINK_TARGET = b'quo"te.txt'      # a target with a character rustic escapes
LINK_NAME = b"link\\to\tquote"
DIR_NAME = b"dir\\with\ttab\xe9"
INNER = b"inner\nfile\\x"


def _make_source(src: Path) -> list[bytes]:
    """Create the files; return the names the host could create."""
    made: list[bytes] = []
    root = os.fsencode(src)
    for name in NAMES:
        try:
            with open(os.path.join(root, name), "wb") as f:
                f.write(name)
        except OSError:
            continue  # e.g. macOS: not valid UTF-8
        made.append(name)
    try:
        os.mkdir(os.path.join(root, DIR_NAME))
        inner_dir = DIR_NAME
    except OSError:
        os.mkdir(os.path.join(root, b"dir\\with\ttab"))
        inner_dir = b"dir\\with\ttab"
    with open(os.path.join(root, inner_dir, INNER), "wb") as f:
        f.write(INNER)
    made.append(inner_dir)
    os.symlink(LINK_TARGET, os.path.join(root, LINK_NAME))
    made.append(LINK_NAME)
    return made


def _snapshot(root: Path) -> dict[bytes, tuple[str, bytes]]:
    """Map each relative byte path to (kind, content-or-link-target)."""
    out: dict[bytes, tuple[str, bytes]] = {}
    base = os.fsencode(root)
    for dirpath, dirnames, filenames in os.walk(base):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base)
            if os.path.islink(full):
                out[rel] = ("link", os.readlink(full))
            elif os.path.isdir(full):
                out[rel] = ("dir", b"")
            else:
                with open(full, "rb") as f:
                    out[rel] = ("file", f.read())
    return out


def _restore_tier1(repo: Path, target: Path, pwfile: Path,
                   bin_path: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [str(bin_path), "--repo", str(repo), "--target", str(target),
         "--password-file", str(pwfile)],
        capture_output=True, timeout=300,
    )
    err = res.stderr.decode("utf-8", "replace")
    assert res.returncode == 0, err
    assert "NOT RESTORED" not in err, err


def _restore_tier3(repo: Path, target: Path) -> None:
    from lcsas.restore.restic_fallback import PurePythonRestorer

    restorer = PurePythonRestorer(repo, password=b"test-password")
    restorer.restore(target=target)
    assert restorer.failures == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX byte names")
def test_escaped_names_restore_alike(tmp_path: Path) -> None:
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    env_bin = os.environ.get("LCSAS_RESTORE_BIN")
    bin_path = Path(env_bin) if env_bin else find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    src = tmp_path / "src"
    src.mkdir()
    made = _make_source(src)
    # The classes the test exists for must have been created.
    assert {b'quo"te.txt', b"back\\slash.txt", b"tab\there.txt"} <= set(made)
    want = _snapshot(src)

    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text("test-password\n")
    build_rustic_repo(src, repo, pwfile)

    outs = {}
    for tier, restore in (
        ("tier1", lambda t: _restore_tier1(repo, t, pwfile, bin_path)),
        ("tier2", lambda t: restore_with_tier2(repo, t, pwfile)),
        ("tier3", lambda t: _restore_tier3(repo, t)),
    ):
        target = tmp_path / f"{tier}_out"
        restore(target)
        outs[tier] = _snapshot(find_restored_root(target))

    for tier, got in outs.items():
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        assert not missing and not extra, (
            f"{tier}: names differ from the source\n"
            f"  missing: {missing}\n  extra:   {extra}"
        )
        wrong = sorted(k for k in want if got[k] != want[k])
        assert not wrong, f"{tier}: content or link target differs: {wrong}"
