r"""restore.bat finishes a one-drive, two-disc restore under wine (#604, #615).

The release drill (#476) ran on a Windows 10 VM with ONE optical drive.
It never got past the first disc swap: restore.bat left the META disc's
drive letter out of tier 1's search list, so once the reader put a data
disc into that same drive, tier 1 kept answering "(still not found ...)"
(#604).  No gate had ever swapped discs within one drive letter (#617).

Wine maps a drive letter to a Linux folder through a symlink in
``$WINEPREFIX/dosdevices``.  Re-pointing ``d:`` from the META folder to
a data-disc folder is the eject-and-insert, in the SAME letter.

Where the run starts: the copy in the tools folder.  From a real disc,
restore.bat copies what it needs into the tools folder (#609) and
relaunches that copy with LCSAS_RELOCATED set to the META disc's letter.
wine runs the copy (test_restore_bat_tools_folder_wine.py checks it) but
not the relaunch line: its cmd does not end a batch at ``(goto) 2>nul``.
So the test lays out a tools folder as the relocation does
(recovery\scripts\restore.bat, recovery\bin\<triple>\, metadata\<id>\)
on C:, sets LCSAS_RELOCATED=D:\ as the relaunch line does, and runs that
copy.  Every line after the relocation then runs as shipped, with D:
holding the live META disc at launch.  The relaunch line and a
same-letter swap on a real drive are covered on windows-latest by
windows-e2e.yml (the single-drive leg).

Wine's ``set /p`` takes everything waiting on stdin at its first read,
so the harness writes each answer only once its prompt is on screen.
LCSAS_INTERACTIVE=on makes tier 1 ask at the disc prompt although its
stdin is a pipe (its default, auto, asks only on a console).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.recovery_hardening._wine_helpers import stop_wineserver
from tests.recovery_hardening.test_restore_bat_wine_smoke import (
    _init_prefix,
    _map_drive,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"
RESTORE_BAT = RECOVERY / "scripts" / "restore.bat"
RESTORE_EXE = RECOVERY / "bin" / "x86_64-windows" / "lcsas-restore.exe"

TRIPLE = "x86_64-pc-windows-gnu"
PASSWORD = "one-drive-only"
SET_ID = "documents"
LABELS = {"disc_a": "LCSAS_TEST_2026_0001", "disc_b": "LCSAS_TEST_2026_0002"}
# Tier 1 restores the files in this order (the tree keeps it).
FILES = {
    "a0.txt": b"file zero\n" * 400,
    "a1.txt": b"file one\n" * 400,
    "a2.bin": bytes(range(256)) * 16,
    "a3.txt": b"file three\n" * 400,
}
# Stand-in for the tools folder (%LOCALAPPDATA%\LCSAS-restore) that
# holds the relocated copy.
RELOC = "lcsas-restore-reloc"
TARGET_WIN = "C:\\restored"
DEADLINE_S = 240
MAX_BOXES = 6

# The last line of a disc prompt, then the "> " it waits behind.
BOX_END = re.compile(r"\+-{58}\+\r?\n> ")
PACK_RE = re.compile(r"\| Pack ([0-9a-f]{16})\.\.\.")
POSIX_PARENTS = ("/Volumes", "/media", "/mnt", "/run/media")

_WINE_OK = shutil.which("wine") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_WINE_OK and RESTORE_BAT.is_file() and RESTORE_EXE.is_file()),
        reason=(
            f"restore.bat single-drive wine test requires wine "
            f"(present={_WINE_OK}), {RESTORE_BAT} and {RESTORE_EXE}"
        ),
    ),
]


@pytest.fixture(scope="module", autouse=True)
def _require_working_wineboot() -> None:
    """Skip if wineboot cannot initialise a fresh prefix within a bounded
    time (#390), as in test_restore_bat_wine_smoke.py."""
    if not _WINE_OK:
        return
    import tempfile
    with tempfile.TemporaryDirectory() as _td:
        env = {**os.environ, "WINEDEBUG": "-all",
               "WINEPREFIX": _td, "DISPLAY": ""}
        try:
            subprocess.run(["wine", "wineboot", "--init"],
                           env=env, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            pytest.skip(
                "wineboot did not initialise a fresh prefix within 90s on "
                "this host (issue #390) — skipping restore.bat wine tests"
            )
        except OSError:
            pytest.skip("wine is not runnable on this host")
        finally:
            stop_wineserver(_td)


@pytest.fixture(autouse=True)
def _stop_test_wineserver(tmp_path: Path):
    """Stop this test's wineserver before pytest removes its prefix (#453)."""
    yield
    stop_wineserver(tmp_path / "wineprefix")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class _Fixture:
    prefix: Path
    env: dict[str, str]
    meta: Path
    discs: dict[str, Path]
    reloc: Path

    @property
    def target(self) -> Path:
        return self.prefix / "drive_c" / "restored"

    def disc_holding(self, pack_prefix: str) -> Path | None:
        for disc in self.discs.values():
            if any(p.name.startswith(pack_prefix)
                   for p in (disc / "data").rglob("*") if p.is_file()):
                return disc
        return None


def _build(tmp_path: Path) -> _Fixture:
    """A META folder and two data-disc folders, plus the relocated copy.

    The tree and a0.txt's data go on disc A, a1.txt's on disc B.  Tier 1
    reads the tree first (prompt 1: disc A), restores a0.txt from disc A,
    then needs disc B for a1.txt (prompt 2).
    """
    sys.path.insert(0, str(RECOVERY / "tests"))
    import test_e2e  # sibling fixture builders
    import test_multidisc

    from lcsas.restore.restic_fallback import PurePythonRestorer

    repo = tmp_path / "repo"
    test_e2e.build_repo(repo, PASSWORD, FILES, split_packs=3)
    reader = PurePythonRestorer(repo, password=PASSWORD.encode(),
                                interactive=False)
    reader._ensure_loaded()
    index = reader._blob_index
    tree_pack = next(loc.pack_id for loc in index.values()
                     if loc.blob_type == "tree")
    on_a = {tree_pack, index[_sha(FILES["a0.txt"])].pack_id}
    packs = sorted(p for p in (repo / "data").iterdir() if p.is_file())
    assert len(packs) == 3 and len(on_a) == 2, (packs, on_a)

    discs = {name: tmp_path / name for name in LABELS}
    for pack in packs:
        disc = discs["disc_a"] if pack.name in on_a else discs["disc_b"]
        dst = disc / "data" / pack.name[:2]
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pack, dst / pack.name)

    def copy_set(root: Path) -> None:
        shutil.copytree(repo, root / "metadata" / SET_ID,
                        ignore=shutil.ignore_patterns("data"))

    def packs_on(disc: Path) -> list[tuple[str, int]]:
        return [(p.name, p.stat().st_size)
                for p in (disc / "data").rglob("*") if p.is_file()]

    catalog = tmp_path / "catalog.db"
    test_multidisc._build_catalog(
        catalog, packs_on(discs["disc_a"]), packs_on(discs["disc_b"]),
        LABELS["disc_a"], LABELS["disc_b"],
    )
    for name, disc in discs.items():
        (disc / "volume_info.json").write_text(
            json.dumps({"label": LABELS[name], "type": "data"}))
        copy_set(disc)
        shutil.copy2(catalog, disc / "catalog.db")

    # The META disc: both copies of restore.bat, the tier-1 program, the
    # backup set's metadata, no catalog.db, and recovery\scripts\
    # restore.sh, which tier 1 checks to see that the META disc is still
    # in the drive.
    meta = tmp_path / "meta"
    scripts = meta / "recovery" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RESTORE_BAT, scripts / "restore.bat")
    shutil.copy2(RESTORE_BAT, meta / "restore.bat")
    (scripts / "restore.sh").write_text("#!/bin/sh\n")
    bin_dir = meta / "recovery" / "bin" / TRIPLE
    bin_dir.mkdir(parents=True)
    shutil.copy2(RESTORE_EXE, bin_dir / "lcsas-restore.exe")
    copy_set(meta)
    (meta / "volume_info.json").write_text(json.dumps({"type": "meta"}))

    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)

    # The relocated copy, laid out as the relocation block writes the
    # tools folder (#609).
    reloc = prefix / "drive_c" / RELOC
    (reloc / "recovery" / "scripts").mkdir(parents=True)
    shutil.copy2(RESTORE_BAT, reloc / "recovery" / "scripts" / "restore.bat")
    shutil.copytree(meta / "recovery" / "bin", reloc / "recovery" / "bin")
    shutil.copytree(meta / "metadata", reloc / "metadata")

    _map_drive(prefix, "d", meta)  # the META disc is in the one drive
    run_env = {k: v for k, v in env.items()
               if not k.startswith("LCSAS_") and k.upper() != "TARGET"}
    run_env.update({
        "LCSAS_RELOCATED": "D:\\",   # what the relaunch line sets
        "LCSAS_TARGET": TRIPLE,
        "LCSAS_INTERACTIVE": "on",
    })
    return _Fixture(prefix=prefix, env=run_env, meta=meta, discs=discs,
                    reloc=reloc)


class _Run:
    """restore.bat under wine with its stdin held open."""

    def __init__(self, fx: _Fixture) -> None:
        bat = f"C:\\{RELOC}\\recovery\\scripts\\restore.bat"
        self.proc = subprocess.Popen(
            ["wine", "cmd", "/c", bat],
            env=fx.env, cwd=str(fx.prefix / "drive_c"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._pump = threading.Thread(target=self._read, daemon=True)
        self._pump.start()
        self.deadline = time.monotonic() + DEADLINE_S

    def _read(self) -> None:
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                return
            with self._lock:
                self._buf.extend(chunk)

    @property
    def text(self) -> str:
        with self._lock:
            return self._buf.decode("utf-8", "replace")

    def wait_for(self, cond) -> bool:
        while time.monotonic() < self.deadline:
            if cond(self.text):
                return True
            if self.proc.poll() is not None:
                self._pump.join(timeout=5)
                return cond(self.text)
            time.sleep(0.2)
        return False

    def send(self, line: str) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(line.encode() + b"\n")
            self.proc.stdin.flush()
        except OSError:
            pass  # it has already exited; the assertions say why

    def finish(self) -> int:
        """Close stdin (ends the final `pause`) and wait for the exit."""
        assert self.proc.stdin is not None
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        try:
            rc = self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = self.proc.wait(timeout=60)
        self._pump.join(timeout=5)
        return rc


def _drive(fx: _Fixture, run: _Run, *, quit_at: int | None = None,
           target_win: str = TARGET_WIN) -> list[str]:
    """Answer restore.bat's prompts and tier 1's disc prompts.

    At each disc prompt, put the disc that holds the named pack into D:
    (re-point the symlink) and press ENTER; at prompt ``quit_at`` type q
    instead.  Returns the text of every disc prompt, in order.
    """
    assert run.wait_for(lambda t: "Restore to which folder?" in t), run.text
    run.send(target_win)
    assert run.wait_for(lambda t: "Password:" in t), run.text
    run.send(PASSWORD)

    boxes: list[str] = []
    while True:
        run.wait_for(lambda t: len(BOX_END.findall(t)) > len(boxes)
                     or "Press any key" in t)
        text = run.text
        ends = [m.end() for m in BOX_END.finditer(text)]
        if len(ends) <= len(boxes):
            return boxes  # the end banner, an exit, or the deadline
        end = ends[len(boxes)]
        box = text[text.rfind("| Pack ", 0, end):end]
        boxes.append(box)
        if len(boxes) == quit_at or len(boxes) > MAX_BOXES:
            run.send("q")
            continue
        m = PACK_RE.search(box)
        disc = fx.disc_holding(m.group(1)) if m else None
        if disc is not None:
            _map_drive(fx.prefix, "d", disc)  # eject, insert, same letter
        run.send("")


def test_one_drive_restore_swaps_discs_in_the_same_letter(tmp_path: Path) -> None:
    """#604: the META disc's letter is searched once the META disc is out,
    and the pack cache keeps the swaps to about one per disc."""
    fx = _build(tmp_path)
    run = _Run(fx)
    try:
        boxes = _drive(fx, run)
    finally:
        rc = run.finish()
    out = run.text
    # Recorded for the drill log: with the rc5 tier 1 a disc is drained
    # into the cache only after a pack is found on it.
    print(f"disc prompts: {len(boxes)} for {len(fx.discs)} data discs")

    assert rc == 0, f"rc={rc}\n--- output ---\n{out}"
    # Case-sensitive: restore.bat's banner, not tier 1's own line.
    assert "Restore complete" in out, out
    for name, data in FILES.items():
        got = fx.target / name
        assert got.is_file() and _sha(got.read_bytes()) == _sha(data), (
            f"{name} not restored byte-exact\n{out}")

    assert 1 <= len(boxes) <= len(fx.discs) + 1, (
        f"{len(boxes)} disc prompts for {len(fx.discs)} data discs\n{out}")
    assert "drives searched for discs: D:\\" in out, out
    for box in boxes:
        searching = box.split("Currently searching:", 1)[-1]
        assert "|   D:\\ " in searching, f"D:\\ not searched:\n{box}"
        for posix in POSIX_PARENTS:
            assert posix not in box, f"{posix} listed on Windows:\n{box}"
    # The pack cache was on: each disc was copied in once.  (Was
    # "draining disc D:\\", tier 1's wording before rc6's #610 rebuild.)
    for label in LABELS.values():
        assert f"copying disc {label} to the cache" in out, out
    assert "cannot create the pack cache" not in out, out
    # After the first swap tier 1 read disc A's catalog.db (through
    # --mount-parent), so the second prompt names disc B.
    assert len(boxes) >= 2 and LABELS["disc_b"] in boxes[1], boxes
    assert "[tier 2]" not in out, out
    # restore.bat removes the pack cache it made once the restore is done,
    # and marks the tools folder for the relaunch line to delete (#609);
    # the launcher it had put on the Desktop is gone.
    assert not (fx.reloc / "pack-cache").exists()
    assert (fx.reloc / ".done").is_file()
    desktops = list((fx.prefix / "drive_c" / "users").glob(
        "*/Desktop/Resume LCSAS restore.bat"))
    assert desktops == [], desktops


def test_q_at_the_disc_prompt_stops_without_tier2(tmp_path: Path) -> None:
    """#615: 'q' at the disc prompt ends tier 1 with a non-zero code.  For
    a backup set read from metadata\\<id>\\ (no data\\), tier 2 cannot
    work, so restore.bat stops there, keeps the restored files and says
    how to carry on.

    The folder has an "&" in its name, as Windows allows: the banner must
    print it whole (`echo %TARGET%` cut it there and ran the rest)."""
    fx = _build(tmp_path)
    # A decoy tier 2: a fall-through would print its banner.
    decoy = fx.reloc / "recovery" / "bin" / TRIPLE / "rustic-static.exe"
    decoy.write_bytes(b"MZ decoy")
    folder = "Photos & Videos"
    target = fx.prefix / "drive_c" / folder
    run = _Run(fx)
    try:
        boxes = _drive(fx, run, quit_at=2, target_win=f"C:\\{folder}")
    finally:
        rc = run.finish()
    out = run.text

    assert len(boxes) == 2, out
    # 75: tier 1 paused by the reader (C5).  Was "not 0 or 77" before
    # tier 1 had its own code for it.
    assert rc == 75, f"rc={rc}\n{out}"
    assert "The restore stopped before it finished" in out, out
    assert f"(code {rc})" in out, out
    # The tools folder stays, and the banner says how to carry on (#609).
    assert f'"C:\\{RELOC}\\Resume LCSAS restore.bat"' in out, out
    assert (fx.reloc / "resume.cfg").is_file()
    assert not (fx.reloc / ".done").exists()
    assert "trying tier 2" not in out and "[tier 2]" not in out, out
    assert "no working recovery method" not in out, out
    assert re.search(r"kept in:\r?\n +C:\\Photos & Videos\r?\n", out), out
    assert "recognize" not in out, out  # "Videos" was not run
    # a0.txt was restored from disc A before the second prompt: kept.
    got = target / "a0.txt"
    assert got.is_file() and _sha(got.read_bytes()) == _sha(FILES["a0.txt"]), out


def test_catalog_seen_at_launch_is_passed_as_a_local_copy(tmp_path: Path) -> None:
    """#610: restore.bat gives tier 1 a COPY of the catalog.db it finds at
    launch, in its own folder, never the file on a disc: tier 1 keeps the
    catalog open to the end, and an open file holds that disc in its
    drive.  `copy` keeps the file's date, which tier 1 compares with any
    catalog.db it meets later.

    Disc A is in a second drive, E:, at launch, so restore.bat sees its
    catalog.db and searches E: as well.  Tier 1 reads the tree and a0.txt
    there with no prompt.  The one prompt is for disc B, by name: the
    catalog is known from the start."""
    fx = _build(tmp_path)
    _map_drive(fx.prefix, "e", fx.discs["disc_a"])
    run = _Run(fx)
    try:
        boxes = _drive(fx, run)
    finally:
        rc = run.finish()
    out = run.text
    print(f"disc prompts: {len(boxes)}")

    assert rc == 0, f"rc={rc}\n--- output ---\n{out}"
    assert "Restore complete" in out, out
    for name, data in FILES.items():
        got = fx.target / name
        assert got.is_file() and _sha(got.read_bytes()) == _sha(data), (
            f"{name} not restored byte-exact\n{out}")

    # The copy is in the tools folder itself (C6), not in its recovery\.
    tools = f"C:\\{RELOC}"
    assert f"using catalog E:\\catalog.db (a copy in {tools})" in out, out
    assert "could not copy" not in out, out
    copy = fx.reloc / "catalog.db"
    original = fx.discs["disc_a"] / "catalog.db"
    assert copy.read_bytes() == original.read_bytes()
    assert abs(copy.stat().st_mtime - original.stat().st_mtime) < 2.0, (
        "the copy did not keep the catalog's date")
    assert "drives searched for discs: D:\\ E:\\" in out, out
    assert len(boxes) == 1 and LABELS["disc_b"] in boxes[0], boxes
    assert "[tier 2]" not in out, out
