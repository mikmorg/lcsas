"""rc6 (#476 drill, F8/F9/F10/F12): Windows tier 1 with ONE optical drive.

The drill reader had one CD drive.  rc5's ``lcsas-restore.exe`` searched
only the paths it was given at launch and the POSIX mount parents, so once
the META disc came out of D: no data disc put into D: was ever searched:
the restore could not finish.  rc6 makes tier 1 find discs by itself on
Windows: every optical drive's root (``GetDriveTypeA == DRIVE_CDROM``) is
searched on each retry, a newly inserted data disc is copied into the pack
cache at once (so it never has to go back in), and ``q`` at the prompt
exits 75 ("paused by the reader").

These run the committed ``recovery/bin/x86_64-windows/lcsas-restore.exe``
(or ``LCSAS_RESTORE_BIN``) under wine in a private prefix.  Drive D: is a
``dosdevices`` symlink typed ``cdrom`` in the prefix's registry, so the
binary sees an optical drive; re-pointing the symlink is taking one disc
out of the drive and putting another in.  The binary gets NO
``--pack-search`` and NO ``--mount-parent``: only ``--meta-disc D:\\`` and
``LCSAS_PACK_CACHE_DIR``, as a reader's script would pass.

A symlink re-point is not a real CDFS eject (no "not ready" while the disc
spins up); windows-e2e's same-letter leg covers real Windows.  If wine
cannot be made to report D: as a CD drive the module skips, saying so.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.recovery_hardening._wine_helpers import stop_wineserver

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_EXE = Path(os.environ["LCSAS_RESTORE_BIN"]) if os.environ.get(
    "LCSAS_RESTORE_BIN"
) else REPO_ROOT / "recovery" / "bin" / "x86_64-windows" / "lcsas-restore.exe"

_BIN_OK = RESTORE_EXE.is_file() and RESTORE_EXE.suffix == ".exe"
_WINE_OK = shutil.which("wine") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_BIN_OK and _WINE_OK),
        reason=f"needs wine and a Windows {RESTORE_EXE.name} (present={_BIN_OK})",
    ),
]

sys.path.insert(0, str(REPO_ROOT / "recovery" / "tests"))

PASSWORD = "correct-horse-battery-staple"
LABEL_A = "LCSAS_TEST_2026_0001"
LABEL_B = "LCSAS_TEST_2026_0002"
BOX_END = re.compile(r"\+-{58}\+\r?\n> ")
PACK_RE = re.compile(r"\| Pack ([0-9a-f]{16})\.\.\.")


def _win(path: Path) -> str:
    """The Windows spelling of a host path (wine maps Z: to /)."""
    return "Z:" + str(path).replace("/", "\\")


def _catalog(path: Path, discs: list[tuple[str, list[Path]]]) -> None:
    """A minimal disc catalog naming each pack's disc."""
    db = sqlite3.connect(str(path))
    db.executescript(
        "CREATE TABLE schema_version(version INTEGER);"
        "INSERT INTO schema_version VALUES(10);"
        "CREATE TABLE packs(pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
        " size_bytes INTEGER, repo_id TEXT);"
        "CREATE TABLE volumes(volume_id INTEGER PRIMARY KEY, label TEXT,"
        " uuid TEXT, media_type TEXT, status TEXT);"
        "CREATE TABLE volume_packs(volume_id INTEGER, pack_id INTEGER);"
    )
    for vid, (label, packs) in enumerate(discs, start=1):
        db.execute("INSERT INTO volumes VALUES(?, ?, ?, 'CD700', 'VERIFIED')",
                   (vid, label, f"uuid-{vid}"))
        for p in packs:
            cur = db.execute("INSERT INTO packs(sha256, size_bytes, repo_id) "
                             "VALUES(?, ?, 'r')", (p.name, p.stat().st_size))
            db.execute("INSERT INTO volume_packs VALUES(?, ?)",
                       (vid, cur.lastrowid))
    db.commit()
    db.close()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A wine prefix with D: as a CD drive, a repository whose packs are
    split over two data discs, and a META disc."""
    import test_e2e  # recovery/tests: synthetic restic repository

    base = tmp_path_factory.mktemp("drive_swap")
    files = {
        "alpha.txt": b"alpha\n" + bytes(range(256)) * 16,
        "beta.txt": b"beta\n" + os.urandom(4096),
        "gamma.bin": os.urandom(16384),
        "delta.bin": os.urandom(16384),
        "epsilon.txt": b"epsilon " * 1024,
    }
    repo = base / "repo"
    test_e2e.build_repo(repo, PASSWORD, files, split_packs=4)
    packs = sorted(p for p in (repo / "data").iterdir() if p.is_file())
    assert len(packs) >= 2, packs
    half = len(packs) // 2
    disc_of: dict[str, str] = {}
    by_disc: dict[str, list[Path]] = {}
    for disc, label, chunk in (("disc_a", LABEL_A, packs[:half]),
                               ("disc_b", LABEL_B, packs[half:])):
        moved = []
        for p in chunk:
            d = base / disc / "data" / p.name[:2]
            d.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(d / p.name))
            moved.append(d / p.name)
            disc_of[p.name] = disc
        by_disc[disc] = moved
        (base / disc / "volume_info.json").write_text(
            json.dumps({"label": label, "type": "data"}))
    shutil.rmtree(repo / "data")
    # Each disc carries the catalog as of its burn: A knows only itself,
    # B (burned later, newer file) knows both.
    _catalog(base / "disc_a" / "catalog.db", [(LABEL_A, by_disc["disc_a"])])
    _catalog(base / "disc_b" / "catalog.db",
             [(LABEL_A, by_disc["disc_a"]), (LABEL_B, by_disc["disc_b"])])
    t = time.time()
    os.utime(base / "disc_a" / "catalog.db", (t - 3600, t - 3600))
    os.utime(base / "disc_b" / "catalog.db", (t, t))
    meta = base / "meta"
    (meta / "recovery" / "scripts").mkdir(parents=True)
    (meta / "recovery" / "scripts" / "restore.sh").write_text("#!/bin/sh\n")
    (meta / "volume_info.json").write_text(json.dumps({"type": "meta"}))
    (base / "pw").write_text(PASSWORD + "\n")
    shutil.copy2(RESTORE_EXE, base / "lcsas-restore.exe")

    prefix = base / "prefix"
    env = {**os.environ, "WINEPREFIX": str(prefix), "WINEDEBUG": "-all",
           "DISPLAY": ""}
    subprocess.run(["wine", "wineboot", "--init"], env=env,
                   capture_output=True, timeout=300, check=False)
    dos = prefix / "dosdevices"
    if not dos.is_dir():
        pytest.skip("wineboot did not create a prefix")
    _only_our_drives(dos)
    (dos / "d:").symlink_to(meta, target_is_directory=True)
    subprocess.run(
        ["wine", "reg", "add", r"HKEY_LOCAL_MACHINE\Software\Wine\Drives",
         "/v", "d:", "/t", "REG_SZ", "/d", "cdrom", "/f"],
        env=env, capture_output=True, timeout=120, check=False)
    # The mount manager reads drive types when it starts.
    stop_wineserver(prefix)
    # The runs themselves get no system D-Bus, so wine's mount manager
    # cannot map the host's own optical drives (another job may have a
    # disc in one) as E: and up.  D: keeps its registry type, which the
    # mount manager reads at start without D-Bus.
    env = {**env, "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/nonexistent"}
    w = {"base": base, "env": env, "dos": dos, "files": files,
         "disc_of": disc_of, "meta": meta}
    yield w
    stop_wineserver(prefix)


def _only_our_drives(dos: Path) -> None:
    """Keep C:, Z: (the host's /) and our D:; drop the drives wine makes
    for host devices.  A host CD drive would show up as E:, and another
    job's disc in it would be searched too."""
    for entry in dos.iterdir():
        if (re.fullmatch(r"[a-y]::?", entry.name)
                and entry.name not in ("c:", "d:")):
            entry.unlink()


def _point_d(world, target: Path) -> None:
    link = world["dos"] / "d:"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target, target_is_directory=True)


def _drive(world, name: str, extra_env: dict[str, str],
           on_prompt) -> tuple[int, str, int]:
    """Run tier 1 with the META disc in D:.  At each disc prompt call
    on_prompt(pack_prefix) -> the bytes to type.  Returns (rc, output,
    number of prompts)."""
    base = world["base"]
    target = base / f"restored-{name}"
    cmd = ["wine", str(base / "lcsas-restore.exe"),
           "--repo", _win(base / "repo"),
           "--password-file", _win(base / "pw"),
           "--target", _win(target),
           "--meta-disc", "D:\\",
           "--interactive", "on"]
    _only_our_drives(world["dos"])
    _point_d(world, world["meta"])
    proc = subprocess.Popen(cmd, env={**world["env"], **extra_env},
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    buf = bytearray()
    lock = threading.Lock()

    def reader() -> None:
        assert proc.stdout is not None
        while True:
            b = proc.stdout.read(1)
            if not b:
                break
            with lock:
                buf.extend(b)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    prompts = 0
    deadline = time.time() + 240
    try:
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.2)
            with lock:
                text = buf.decode("utf-8", "replace")
            ends = list(BOX_END.finditer(text))
            if len(ends) <= prompts:
                continue
            prompts = len(ends)
            asked = PACK_RE.findall(text[: ends[-1].end()])
            pack = asked[-1] if asked else None
            answer = on_prompt(pack) if prompts <= 4 else b"q\n"
            assert proc.stdin is not None
            proc.stdin.write(answer)
            proc.stdin.flush()
    finally:
        if proc.poll() is None:
            proc.kill()
        rc = proc.wait()
        th.join(timeout=5)
    return rc, buf.decode("utf-8", "replace"), prompts


BOX_RE = re.compile(r"(\+-{58}\+)\r?\n(.*?)(\+-{58}\+)", re.S)


def _box(text: str, n: int) -> list[str]:
    """The n-th prompt box (1-based), border to border, one str a line."""
    m = list(BOX_RE.finditer(text))[n - 1]
    inner = m.group(2).replace("\r", "").split("\n")[:-1]
    return [m.group(1), *inner, m.group(3)]


def test_one_drive_swap_finishes_with_each_disc_inserted_once(world) -> None:
    """META disc in D:; each prompt, put the disc holding the pack into
    D: and press ENTER.  The restore must finish, byte-exact, with one
    insertion per data disc."""
    base = world["base"]
    cache = base / "cache" / "nested" / "deeper"   # made with '\' separators
    inserted: list[str] = []

    def swap(pack: str | None) -> bytes:
        disc = next(d for h, d in world["disc_of"].items()
                    if pack and h.startswith(pack))
        inserted.append(disc)
        _point_d(world, base / disc)
        time.sleep(0.3)
        return b"\n"

    rc, out, prompts = _drive(world, "swap",
                              {"LCSAS_PACK_CACHE_DIR": _win(cache)}, swap)
    assert rc == 0, out[:3000] + "\n...\n" + out[-2000:]
    for name, data in world["files"].items():
        got = base / "restored-swap" / name
        assert got.is_file(), f"{name} missing:\n{out[-3000:]}"
        assert hashlib.sha256(got.read_bytes()).digest() == \
            hashlib.sha256(data).digest(), name
    assert prompts <= 2 and sorted(inserted) == ["disc_a", "disc_b"], (
        f"{prompts} prompts, inserted {inserted}:\n{out[-4000:]}")
    # Found by the drive-root search, named by its label, and copied
    # whole into the cache the moment it was seen.
    for label in (LABEL_A, LABEL_B):
        assert f"mounted the disc labelled {label} at D:\\" in out, out[-3000:]
        assert f"copying disc {label} to the cache" in out, out[-3000:]
    # The first box: the META disc is still in, so the one-drive hint
    # shows; there is no catalog yet; no POSIX folder is listed; every
    # line is 60 columns.
    first = _box(out, 1)
    assert all(len(line) == 60 for line in first), first
    joined = "\n".join(first)
    assert re.search(r"\|   [A-CE-Y]:\\ ", joined) is None, (
        f"a drive other than D: was searched (a host CD drive?):\n{joined}")
    assert "Single-drive restore" in joined
    assert "(no disc catalog yet: insert your newest data disc)" in joined
    for posix in ("/Volumes", "/media", "/mnt"):
        assert posix not in joined, joined
    # The second box: disc A's catalog (a copy in the cache) answers.
    second = _box(out, 2)
    assert all(len(line) == 60 for line in second), second
    assert "Single-drive restore" not in "\n".join(second)
    # Every pack is cached, the catalog copy is disc B's (the newer one
    # replaced the open copy of A's), and no ".part" is left behind.
    cached = [p for p in (cache / "data").rglob("*") if p.is_file()]
    assert len(cached) == len(world["disc_of"]), cached
    assert not list(cache.rglob("*.part"))
    assert (cache / ".locator-catalog.db").read_bytes() == \
        (base / "disc_b" / "catalog.db").read_bytes()


def test_q_at_the_disc_prompt_exits_75(world) -> None:
    rc, out, prompts = _drive(world, "quit", {}, lambda pack: b"q\n")
    assert prompts == 1, out[-3000:]
    assert rc == 75, out[-3000:]
    assert "To carry on, run the same command again" in out


def test_mount_dirs_split_on_semicolon(world) -> None:
    """LCSAS_MOUNT_DIRS splits on ';' on Windows (':' is in every drive
    letter), and a parent that does not exist is not listed."""
    base = world["base"]
    res = subprocess.run(
        ["wine", str(base / "lcsas-restore.exe"), "--verbose",
         "--repo", _win(base / "no-such-repo"),
         "--password-file", _win(base / "pw"),
         "--target", _win(base / "unused")],
        env={**world["env"], "LCSAS_MOUNT_DIRS": "D:\\;E:\\"},
        capture_output=True, text=True, timeout=120, check=False)
    assert "mount-parents=2" in res.stderr, res.stderr
