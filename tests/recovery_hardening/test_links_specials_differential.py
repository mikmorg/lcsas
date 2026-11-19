"""Issues #782-#785, #852: tiers 1, 2 and 3 restore links and FIFOs alike.

They also never write through a link left by an earlier restore.

Each test builds a real rustic repo and restores it with tier 1
(lcsas-restore), tier 2 (rustic) and tier 3 (the Python reader).

- Links: a relative link that points up and across inside the backup,
  one that points out of it, an absolute one, a dangling one and one
  whose target is not valid UTF-8 (rustic keeps those bytes in
  ``linktarget_raw``).  Every tier restores each one with its stored
  target, byte for byte.
- Special files: a FIFO and (on Linux) a socket.  rustic makes them
  without privilege, and so do tiers 1 and 3.
- Restore over an earlier restore (#784): snapshot A has links where
  snapshot B has a file and a folder, and a folder where B has a link.
  Restoring A, then B, into one folder must change nothing outside it,
  replace the links, keep the folder and report that item.
- As root only (#785, #783): a setuid and a setgid file owned by another
  user keep their mode and owner, and a character device is made.  This
  test skips unless run as root (run it by hand with sudo).

Runs by default; skips when rustic or the lcsas-restore binary is absent
(set LCSAS_RESTORE_BIN to test a private build).
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    find_restore_bin,
    find_restored_root,
    latest_snapshot_id,
    rustic_backup,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name == "nt", reason="POSIX links and FIFOs"),
]

PASSWORD = "test-password"
FAILURES = "RESTORE_FAILURES.txt"


def _need_tools() -> Path:
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    env_bin = os.environ.get("LCSAS_RESTORE_BIN")
    bin_path = Path(env_bin) if env_bin else find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")
    return bin_path


def _kind(full: bytes) -> tuple[str, bytes]:
    """(kind, link target or file content), never opening a FIFO."""
    st = os.lstat(full)
    if stat.S_ISLNK(st.st_mode):
        return ("link", os.readlink(full))
    if stat.S_ISDIR(st.st_mode):
        return ("dir", b"")
    if stat.S_ISFIFO(st.st_mode):
        return ("fifo", b"")
    if stat.S_ISSOCK(st.st_mode):
        return ("socket", b"")
    if stat.S_ISCHR(st.st_mode):
        return ("chardev", str(st.st_rdev).encode())
    if stat.S_ISREG(st.st_mode):
        with open(full, "rb") as f:
            return ("file", f.read())
    return ("other", b"")


def _snapshot(root: Path) -> dict[bytes, tuple[str, bytes]]:
    """Map each relative byte path to (kind, content-or-link-target)."""
    out: dict[bytes, tuple[str, bytes]] = {}
    base = os.fsencode(root)
    for dirpath, dirnames, filenames in os.walk(base):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, base)] = _kind(full)
    return out


def _tier1(bin_path: Path, repo: Path, target: Path, pwfile: Path,
           snapshot: str | None = None) -> subprocess.CompletedProcess[str]:
    target.mkdir(parents=True, exist_ok=True)
    cmd = [str(bin_path), "--repo", str(repo), "--target", str(target),
           "--password-file", str(pwfile)]
    if snapshot:
        cmd += ["--snapshot", snapshot]
    return subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace", timeout=300)


def _tier2(repo: Path, target: Path, pwfile: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["rustic", "-r", str(repo), "restore", "latest", str(target),
         "--password-file", str(pwfile)],
        capture_output=True, check=True, timeout=300,
    )


def _tier3(repo: Path, target: Path, snapshot: str | None = None):
    from lcsas.restore.restic_fallback import PurePythonRestorer

    restorer = PurePythonRestorer(repo, password=PASSWORD.encode())
    restorer.restore(target=target, snapshot_id=snapshot)
    return restorer


def _make_links_and_specials(src: Path) -> None:
    (src / "a").mkdir()
    (src / "b").mkdir()
    (src / "b" / "f").write_text("f\n")
    os.symlink("../b/f", src / "a" / "up")             # up and across
    os.symlink("../../../../outside", src / "a" / "out")  # up and out
    os.symlink("/etc/hostname", src / "abs")
    os.symlink("nowhere", src / "dangling")
    root = os.fsencode(src)
    os.symlink(b"tgt\xe9", os.path.join(root, b"nonutf"))  # #852
    os.mkfifo(src / "fifo")
    if sys.platform.startswith("linux"):
        # Linux only: macOS caps a socket path at 104 bytes (pytest's
        # tmp paths are longer) and lets only root mknod a socket.
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind(str(src / "sock"))
        finally:
            sock.close()


def test_links_and_specials_restore_alike(tmp_path: Path) -> None:
    bin_path = _need_tools()
    src = tmp_path / "src"
    src.mkdir()
    _make_links_and_specials(src)
    want = _snapshot(src)
    assert want[b"nonutf"] == ("link", b"tgt\xe9")
    assert want[b"fifo"][0] == "fifo"

    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text(PASSWORD + "\n")
    build_rustic_repo(src, repo, pwfile)

    outs = {}
    t1 = tmp_path / "tier1_out"
    res = _tier1(bin_path, repo, t1, pwfile)
    assert res.returncode == 0, res.stderr
    assert "NOT RESTORED" not in res.stderr, res.stderr
    outs["tier1"] = _snapshot(find_restored_root(t1))

    t2 = tmp_path / "tier2_out"
    _tier2(repo, t2, pwfile)
    outs["tier2"] = _snapshot(find_restored_root(t2))

    t3 = tmp_path / "tier3_out"
    restorer = _tier3(repo, t3)
    assert restorer.failures == 0 and restorer.skipped == 0
    outs["tier3"] = _snapshot(find_restored_root(t3))

    for tier, got in outs.items():
        assert got == want, (
            f"{tier} differs from the source:\n"
            f"  missing: {sorted(set(want) - set(got))}\n"
            f"  extra:   {sorted(set(got) - set(want))}\n"
            f"  wrong:   {sorted(k for k in want if k in got and got[k] != want[k])}"
        )
    for out in (t1, t3):
        assert not (out / FAILURES).exists()


def test_restore_over_earlier_restore_writes_nothing_outside(
    tmp_path: Path,
) -> None:
    """#784, shared fixture for tiers 1 and 3: restore snapshot A, then
    snapshot B, into one folder."""
    bin_path = _need_tools()
    outside = tmp_path / "outside"
    outside.mkdir()
    out_file = outside / "file"
    out_file.write_text("OUTSIDE\n")
    out_dir = outside / "dir"
    out_dir.mkdir()
    out_dir.chmod(0o751)

    src = tmp_path / "src"
    (src / "d").mkdir(parents=True)
    os.symlink(out_file, src / "d" / "x")       # A: link, B: file
    os.symlink(out_dir, src / "dd")              # A: link, B: folder
    (src / "l").mkdir()                          # A: folder, B: link
    (src / "l" / "keep").write_text("keep\n")
    os.mkfifo(src / "q")                         # A: FIFO, B: file

    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text(PASSWORD + "\n")
    build_rustic_repo(src, repo, pwfile)
    snap_a = latest_snapshot_id(repo, pwfile)

    (src / "d" / "x").unlink()
    (src / "d" / "x").write_text("B\n")
    (src / "dd").unlink()
    (src / "dd").mkdir()
    (src / "dd" / "inner").write_text("inner\n")
    shutil.rmtree(src / "l")
    os.symlink("d/x", src / "l")
    (src / "q").unlink()
    (src / "q").write_text("q\n")
    snap_b = rustic_backup(src, repo, pwfile)

    for tier in ("tier1", "tier3"):
        target = tmp_path / f"{tier}_out"
        if tier == "tier1":
            res = _tier1(bin_path, repo, target, pwfile, snap_a)
            assert res.returncode == 0, res.stderr
            res = _tier1(bin_path, repo, target, pwfile, snap_b)
            # The folder at "l" is kept and reported: an error, and
            # the walk finishes (#872: exit 4).
            assert res.returncode == 4, res.stderr
            assert "a folder with this name is already there" in res.stderr
        else:
            _tier3(repo, target, snap_a)
            restorer = _tier3(repo, target, snap_b)
            assert restorer.failures == 1

        assert out_file.read_text() == "OUTSIDE\n", tier
        assert sorted(os.listdir(out_dir)) == [], tier
        assert stat.S_IMODE(out_dir.stat().st_mode) == 0o751, tier

        # The tree sits under <target>/<absolute source path>; the list
        # of items not restored sits at <target>.
        root = target / src.relative_to(src.anchor)
        x = root / "d" / "x"
        assert not x.is_symlink() and x.read_text() == "B\n", tier
        dd = root / "dd"
        assert not dd.is_symlink() and (dd / "inner").read_text() == "inner\n"
        q = root / "q"
        assert stat.S_ISREG(os.lstat(q).st_mode) and q.read_text() == "q\n"
        assert (root / "l" / "keep").read_text() == "keep\n", tier
        listing = (target / FAILURES).read_text()
        assert "a folder with this name is already there" in listing, tier


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="needs root: run by hand with sudo",
)
def test_root_restore_keeps_owner_setuid_and_devices(tmp_path: Path) -> None:
    """#785: the owner is set before the mode, so setuid and setgid
    survive a root restore; #783: as root a device is made, as rustic
    does.  Tiers 1, 2 and 3 must agree."""
    bin_path = _need_tools()
    src = tmp_path / "src"
    src.mkdir()
    su = src / "su"
    su.write_text("su\n")
    os.chown(su, 1234, 1234)
    su.chmod(0o4755)
    sg = src / "sg"
    sg.write_text("sg\n")
    os.chown(sg, 1234, 1234)
    sg.chmod(0o2755)
    os.mknod(src / "null", 0o644 | stat.S_IFCHR, os.makedev(1, 3))

    def meta(root: Path) -> dict[str, tuple[int, int, int, int]]:
        out = {}
        for name in ("su", "sg", "null"):
            st = os.lstat(root / name)
            out[name] = (st.st_mode, st.st_uid, st.st_gid, st.st_rdev)
        return out

    want = meta(src)
    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text(PASSWORD + "\n")
    build_rustic_repo(src, repo, pwfile)

    t1 = tmp_path / "tier1_out"
    res = _tier1(bin_path, repo, t1, pwfile)
    assert res.returncode == 0, res.stderr
    t2 = tmp_path / "tier2_out"
    _tier2(repo, t2, pwfile)
    t3 = tmp_path / "tier3_out"
    restorer = _tier3(repo, t3)
    assert restorer.failures == 0 and restorer.skipped == 0
    for tier, out in (("tier1", t1), ("tier2", t2), ("tier3", t3)):
        got = meta(find_restored_root(out))
        assert got == want, f"{tier}: {got} != {want}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
