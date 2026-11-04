r"""restore.bat's questions under wine: disc, set, password, cards (#605-#608).

Release drill #476 ran restore.bat from a META disc on a Windows 10 VM
with ONE optical drive.  Past the copy to the tools folder (#609) and the
disc swaps (#604), the questions themselves let the reader down:

* #605: the META disc's copy of each backup set won, so "latest" was the
  latest when the META disc was burned, and a set added since was not
  listed at all.  Now the script asks for the NEWEST data disc first,
  and merges that disc's index and snapshots into the tools folder (C2).
* #606: the menu showed folder names, UUIDs since `repo add`.  It now
  shows each set's name_cmd from its LCSAS_REPO.txt (C1), and the split.
* #608: an empty or a wrong password ended the run.  Now it asks again,
  and a wrong one is caught before the restore starts.
* #607: share cards had to be combined by hand.  Now the script asks
  for K cards, checks each one alone, and rebuilds the password.
* After a good restore it asks whether to restore another backup set
  from the same discs, and deletes the tools folder only after "no".

The fixtures use UUID set ids with names that differ from them, and
every set has its marker: a fixture whose id is its name hides #606.

What wine can and cannot run (see test_restore_bat_tools_folder_wine.py):
it runs the copy into the tools folder but not the relaunch line, so the
relocated tests lay the tools folder out as the copy does and start the
copy in it with LCSAS_RELOCATED=D:\, as the relaunch line does.  Wine's
``set /p`` takes everything waiting on stdin at its first read, so the
harness writes each answer only once its prompt is on screen.  One wine
prefix serves the whole module (a fresh one is about 1.3 GB); each test
resets its drive letters.
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
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.recovery_hardening._wine_helpers import stop_wineserver
from tests.recovery_hardening.test_restore_bat_wine_smoke import (
    _harden_prefix_drives,
    _init_prefix,
    _map_drive,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"
RESTORE_BAT = RECOVERY / "scripts" / "restore.bat"
WIN_BIN = RECOVERY / "bin" / "x86_64-windows"
PROGRAMS = ("lcsas-ecc.exe", "lcsas-keyshare.exe", "lcsas-restore.exe")
TRIPLE = "x86_64-pc-windows-gnu"

LABEL_A = "LCSAS_TEST_2026_0001"
LABEL_B = "LCSAS_TEST_2026_0002"  # the newest data disc
# How long one wine run may take.  The teeth check (the same tests run
# against an older restore.bat, where the prompts never come) sets it
# lower with LCSAS_WINE_TEST_DEADLINE_S.
DEADLINE_S = int(os.environ.get("LCSAS_WINE_TEST_DEADLINE_S", "240"))

ACCESS_DENIED = re.compile(r"Access (is )?denied", re.IGNORECASE)
NOT_RUN = ("recognize", "is not recognized", "Syntax error")

_WINE_OK = shutil.which("wine") is not None
_BINS_OK = all((WIN_BIN / p).is_file() for p in PROGRAMS)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_WINE_OK and RESTORE_BAT.is_file() and _BINS_OK),
        reason=(
            f"restore.bat journey wine test requires wine "
            f"(present={_WINE_OK}), {RESTORE_BAT} and the Windows programs "
            f"in {WIN_BIN}"
        ),
    ),
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix()
                  for p in root.rglob("*") if p.is_file())


def _make_writable(root: Path) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(dirpath, name), 0o644)


def _make_read_only(root: Path) -> None:
    """A disc: nothing in it can be written."""
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o444)
        os.chmod(dirpath, 0o555)


# --------------------------------------------------------------------
# The wine prefix: one for the module
# --------------------------------------------------------------------

@dataclass
class _Wine:
    prefix: Path
    env: dict[str, str]
    user: str
    temp: Path  # %TEMP%, on the Linux side

    @property
    def drive_c(self) -> Path:
        return self.prefix / "drive_c"

    @property
    def user_dir(self) -> Path:
        return self.drive_c / "users" / self.user

    @property
    def desktop(self) -> Path:
        return self.user_dir / "Desktop"


def _win_to_host(wine: _Wine, path: str) -> Path:
    """C:\\a\\b -> <prefix>/drive_c/a/b (only C: is needed here)."""
    assert path[:3].upper() == "C:\\", path
    return wine.drive_c.joinpath(*path[3:].split("\\"))


@pytest.fixture(scope="module")
def wine(tmp_path_factory: pytest.TempPathFactory):
    """One wine prefix for the module, as test_restore_bat_wine_smoke
    makes them (every drive letter D: to Z: claimed by an empty folder,
    the user's Desktop and Documents folders of its own)."""
    prefix = tmp_path_factory.mktemp("journey") / "wineprefix"
    try:
        env = _init_prefix(prefix)
    except subprocess.TimeoutExpired:
        pytest.skip("wine could not start a fresh prefix on this host (#390)")
    users = [p.name for p in (prefix / "drive_c" / "users").iterdir()
             if p.is_dir() and p.name.lower() != "public"]
    if len(users) != 1:
        stop_wineserver(prefix)
        pytest.skip(f"wine prefix has no single user folder: {users}")
    res = subprocess.run(["wine", "cmd", "/c", "echo %TEMP%"], env=env,
                         capture_output=True, timeout=120)
    temp_win = res.stdout.decode("utf-8", "replace").strip()
    run_env = {k: v for k, v in env.items()
               if not k.startswith("LCSAS_") and k.upper() != "TARGET"}
    run_env.update({"LCSAS_TARGET": TRIPLE, "LCSAS_INTERACTIVE": "on"})
    w = _Wine(prefix=prefix, env=run_env, user=users[0], temp=Path())
    w.temp = _win_to_host(w, temp_win)
    w.temp.mkdir(parents=True, exist_ok=True)
    try:
        yield w
    finally:
        stop_wineserver(prefix)


@pytest.fixture
def fresh(wine: _Wine):
    """Each test starts with every drive letter empty, no tools folder,
    no launcher on the Desktop and no lcsas files in %TEMP%."""
    _harden_prefix_drives(wine.prefix)
    for leftover in (wine.user_dir / "AppData" / "Local" / "LCSAS-restore",):
        shutil.rmtree(leftover, ignore_errors=True)
    for item in list(wine.desktop.glob("*")) + list(wine.temp.glob("lcsas-*")):
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink()
    return wine


# --------------------------------------------------------------------
# The archive: backup sets, data discs, the META disc
# --------------------------------------------------------------------

@dataclass
class _Set:
    """One backup set.  Invented: the UUID ids, names and passwords."""

    repo_id: str
    name: str
    password: str
    files: dict[str, bytes]
    split: tuple[int, int] | None = None
    shares: list[str] = field(default_factory=list)
    split_id: int | None = None
    repo: Path = Path()

    @property
    def name_cmd(self) -> str:
        from lcsas.staging.repo_marker import cmd_name
        return cmd_name(self.name, self.repo_id)


def _docs() -> _Set:
    return _Set("9c41d2e8-5f3a-4b7e-a0d6-71e2c8b45f09", "Family documents",
                "docs-pass-4471",
                {"letter-a.txt": b"dear all\n" * 300,
                 "tax-2025.bin": bytes(range(256)) * 12})


def _photos(split: tuple[int, int] | None = None) -> _Set:
    return _Set("2f7a9b13-c6e4-4d58-9a21-e0b3f6c7d812", "Holiday photos",
                "photos-pass-9302",
                {"beach.jpg": b"\xff\xd8 not really a jpeg " * 200,
                 "notes.txt": b"the sea\n" * 150},
                split=split)


@dataclass
class _Archive:
    meta: Path
    discs: dict[str, Path]  # label -> disc folder
    sets: list[_Set]


def _build_set(tmp: Path, s: _Set) -> None:
    sys.path.insert(0, str(RECOVERY / "tests"))
    import test_e2e  # sibling fixture builder

    s.repo = tmp / f"repo-{s.repo_id}"
    test_e2e.build_repo(s.repo, s.password, s.files, split_packs=2)
    if s.split:
        from lcsas.keyshare import (
            encode_master_secret,
            share_identifier,
            split_secret,
        )
        s.shares = split_secret(encode_master_secret(s.password.encode()),
                                *s.split)
        s.split_id = share_identifier(s.shares[0])


def _put_set(root: Path, s: _Set, *, marker: bool = True) -> Path:
    from lcsas.staging.repo_marker import RepoMarker, write_repo_marker

    dst = root / "metadata" / s.repo_id
    shutil.copytree(s.repo, dst, ignore=shutil.ignore_patterns("data"))
    if marker:
        write_repo_marker(dst, RepoMarker(
            repo_id=s.repo_id, name=s.name, split=s.split,
            split_id=s.split_id))
    return dst


def _packs(s: _Set) -> list[Path]:
    return sorted(p for p in (s.repo / "data").iterdir() if p.is_file())


def _write_disc(disc: Path, label: str, sets: list[_Set],
                packs_of: list[_Set], catalog: Path,
                only: set[str] | None = None) -> None:
    """A data disc as the burn lays it out: data\\<xx>\\<pack>, every set's
    metadata with its marker, catalog.db, volume_info.json and
    VOLUME_INFO.txt (written by the real renderer).  ``only``: the pack
    names to put on it, of the sets in ``packs_of`` (default: all)."""
    from lcsas.db.models import Pack, Volume
    from lcsas.staging.metadata import render_volume_info_txt

    disc.mkdir(parents=True)
    packs = []
    for s in packs_of:
        for pack in _packs(s):
            if only is not None and pack.name not in only:
                continue
            dst = disc / "data" / pack.name[:2]
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pack, dst / pack.name)
            packs.append(Pack(pack_id=len(packs) + 1, sha256=pack.name,
                              size_bytes=pack.stat().st_size,
                              repo_id=s.repo_id, is_pruned=False,
                              created_at=""))
    for s in sets:
        _put_set(disc, s)
    shutil.copy2(catalog, disc / "catalog.db")
    volume = Volume(volume_id=1, label=label, uuid=f"uuid-{label}",
                    media_type="CD700", capacity_bytes=700 * 2**20,
                    used_bytes=0, location="", status="STAGING",
                    created_at="2026-10-01 10:00:00", closed_at=None,
                    verified_at=None)
    (disc / "VOLUME_INFO.txt").write_text(render_volume_info_txt(
        volume, packs, repo_names={s.repo_id: s.name for s in sets}))
    (disc / "volume_info.json").write_text(
        json.dumps({"uuid": volume.uuid, "label": label}, indent=2))


def _write_meta(meta: Path, sets: list[_Set]) -> None:
    """The META disc: both copies of restore.bat, restore.sh (tier 1
    checks it to see the META disc is in), this computer's programs, the
    guide, and the metadata of the sets that existed when it was burned.
    No catalog, no label."""
    from datetime import UTC, datetime

    from lcsas.meta.builder import render_meta_volume_info_txt

    scripts = meta / "recovery" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RESTORE_BAT, scripts / "restore.bat")
    shutil.copy2(RESTORE_BAT, meta / "restore.bat")
    (scripts / "restore.sh").write_text("#!/bin/sh\n")
    win = meta / "recovery" / "bin" / TRIPLE
    win.mkdir(parents=True)
    for prog in PROGRAMS:
        shutil.copy2(WIN_BIN / prog, win / prog)
    (meta / "recovery" / "docs").mkdir()
    shutil.copy2(RECOVERY / "docs" / "RECOVER_WINDOWS.txt",
                 meta / "recovery" / "docs" / "RECOVER_WINDOWS.txt")
    for s in sets:
        _put_set(meta, s)
    (meta / "volume_info.json").write_text(json.dumps({"type": "meta"}))
    (meta / "VOLUME_INFO.txt").write_text(
        render_meta_volume_info_txt(datetime(2026, 9, 1, tzinfo=UTC)))


def _catalog(path: Path, disc_a: list[_Set], disc_b: list[_Set] | None,
             only: set[str] | None = None) -> Path:
    """A catalog as burned on a disc.  Disc A's (``disc_b`` None) was
    burned before disc B existed.  ``only``: disc A's pack names, when
    it holds only some packs of its sets (the rest are disc B's)."""
    sys.path.insert(0, str(RECOVERY / "tests"))
    import test_multidisc

    def sizes(sets: list[_Set], keep) -> list[tuple[str, int]]:
        return [(p.name, p.stat().st_size) for s in sets for p in _packs(s)
                if keep(p.name)]

    on_a = (lambda n: True) if only is None else (lambda n: n in only)
    on_b = (lambda n: True) if only is None else (lambda n: n not in only)
    test_multidisc._build_catalog(path, sizes(disc_a, on_a),
                                  sizes(disc_b or [], on_b), LABEL_A, LABEL_B)
    return path


def _tools_folder(wine: _Wine, archive: _Archive, name: str) -> Path:
    """The tools folder as the copy from the META disc lays it out:
    restore.bat, this computer's programs, the guide, the META disc's
    metadata.  It runs as the relaunch line starts it (LCSAS_RELOCATED)."""
    tools = wine.drive_c / name
    (tools / "recovery" / "scripts").mkdir(parents=True)
    shutil.copy2(RESTORE_BAT, tools / "recovery" / "scripts" / "restore.bat")
    shutil.copytree(archive.meta / "recovery" / "bin",
                    tools / "recovery" / "bin")
    shutil.copytree(archive.meta / "recovery" / "docs",
                    tools / "recovery" / "docs")
    shutil.copytree(archive.meta / "metadata", tools / "metadata")
    return tools


# --------------------------------------------------------------------
# Driving a run
# --------------------------------------------------------------------

class _Run:
    """One wine command, stdin held open, output pumped; answers are
    written one at a time, each once its prompt is on screen."""

    def __init__(self, wine: _Wine, cmd: str,
                 extra_env: dict[str, str] | None = None) -> None:
        self.proc = subprocess.Popen(
            ["wine", "cmd", "/c", cmd],
            env={**wine.env, **(extra_env or {})}, cwd=str(wine.drive_c),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._pump = threading.Thread(target=self._read, daemon=True)
        self._pump.start()
        self.deadline = time.monotonic() + DEADLINE_S
        self.pos = 0  # what the test has read so far

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

    def expect(self, needle: str) -> str:
        """Wait for ``needle`` after what was read so far; return the
        text up to and including it."""
        while True:
            text = self.text
            at = text.find(needle, self.pos)
            if at != -1:
                seen = text[self.pos:at + len(needle)]
                self.pos = at + len(needle)
                return seen
            if self.proc.poll() is not None:
                self._pump.join(timeout=5)
                if needle in self.text[self.pos:]:
                    continue
                raise AssertionError(
                    f"exited (rc={self.proc.returncode}) before {needle!r}:\n"
                    f"{self.text}")
            if time.monotonic() > self.deadline:
                raise AssertionError(f"no {needle!r} in time:\n{self.text}")
            time.sleep(0.2)

    def answer(self, prompt: str, line: str) -> str:
        seen = self.expect(prompt)
        self.send(line)
        return seen

    def send(self, line: str) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(line.encode() + b"\n")
            self.proc.stdin.flush()
        except OSError:
            pass  # it has already exited; the assertions say why

    def finish(self) -> int:
        """Close stdin (ends the last `pause`) and wait for the exit."""
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


def _assert_clean(out: str) -> None:
    """Nothing in a name, a card or a password was run as a command, and
    no write probe said "Access denied"."""
    assert not ACCESS_DENIED.search(out), out
    for bad in NOT_RUN:
        assert bad not in out, f"{bad!r} in the output:\n{out}"


def _assert_restored(target: Path, s: _Set, out: str) -> None:
    for rel, data in s.files.items():
        got = target / rel
        assert got.is_file() and _sha(got.read_bytes()) == _sha(data), (
            f"{rel} of {s.name} not restored byte-exact\n{out}")


def _menu_lines(out: str) -> list[str]:
    return re.findall(r"^ {3}\[\d+\] (.*?)\r?$", out, re.MULTILINE)


# --------------------------------------------------------------------
# The tests
# --------------------------------------------------------------------

def test_newest_data_disc_first_on_one_drive(fresh: _Wine, tmp_path: Path) -> None:
    """#605 (C2, C7) on one drive.  The META disc knows only "Family
    documents"; "Holiday photos" was added after it was burned and is on
    the newest data disc only.  The META disc's copy of the documents set
    also holds an index file and a key file that the newest disc's copy
    does not, and the disc's copy a key file the META's does not.

    1. From the META disc: Q at the newest-disc step ends the run with 65
       (nothing restored), after the copy to the tools folder.
    2. The copy in the tools folder (relocated): the step asks for the
       newest data disc with the C7 wording; the disc goes into D: (the
       one drive) and ENTER prints its full label, read from
       VOLUME_INFO.txt.  The menu then lists the photos set too, and it
       restores from that disc.  In the tools folder each set has
       snapshots\\ and index\\ from the newest disc only, keys\\ from both,
       and the catalog copy is that disc's."""
    wine = fresh
    docs, photos = _docs(), _photos()
    for s in (docs, photos):
        _build_set(tmp_path, s)
    meta = tmp_path / "meta"
    _write_meta(meta, [docs])
    # The META disc's copy of the documents set: an index file and a key
    # file that the newest disc's copy does not have.
    meta_docs = meta / "metadata" / docs.repo_id
    index_file = next((meta_docs / "index").iterdir())
    shutil.copy2(index_file, meta_docs / "index" / ("0" * 64))
    key_file = next((meta_docs / "keys").iterdir())
    shutil.copy2(key_file, meta_docs / "keys" / ("a" * 64))
    cat_a = _catalog(tmp_path / "cat-a.db", [docs], None)
    cat_b = _catalog(tmp_path / "cat-b.db", [docs], [photos])
    disc_a = tmp_path / "disc_a"
    disc_b = tmp_path / "disc_b"
    _write_disc(disc_a, LABEL_A, [docs], [docs], cat_a)
    _write_disc(disc_b, LABEL_B, [docs, photos], [docs, photos], cat_b)
    shutil.copy2(key_file, disc_b / "metadata" / docs.repo_id / "keys" / ("b" * 64))
    _make_read_only(meta)
    try:
        _map_drive(wine.prefix, "d", meta)

        # --- 1. from the META disc: Q at the step ------------------------
        run = _Run(wine, "D:\\restore.bat")
        try:
            step = run.answer("Newest data disc: ", "q")
        finally:
            rc = run.finish()
        out = run.text
        _assert_clean(out)
        assert rc == 65, f"rc={rc}\n{out}"
        assert "Stopped; nothing was restored." in out, out
        tools = wine.user_dir / "AppData" / "Local" / "LCSAS-restore"
        assert (tools / "recovery" / "scripts" / "restore.bat").is_file(), out
        assert "copied recovery files to" in step, step
        c7 = ("Take the META disc out and put in your NEWEST data disc: the one\r\n"
              " with the latest year, and then the highest number, e.g.\r\n"
              " LCSAS_CD700_2026_0002.  File Explorer shows only the first 16\r\n"
              " characters of the label; the full label is printed on the disc.")
        assert c7 in step, step
        assert ("Press ENTER when it is in; S then ENTER to skip (use the copies\r\n"
                " on the META disc); Q then ENTER to quit.") in step, step
        assert "Restore to which folder?" not in out, out

        # --- 2. the copy in the tools folder ------------------------------
        copy = tools / "recovery" / "scripts" / "restore.bat"
        assert copy.read_bytes() == RESTORE_BAT.read_bytes()
        tools_win = f"C:\\users\\{wine.user}\\AppData\\Local\\LCSAS-restore"
        run = _Run(wine, f"{tools_win}\\recovery\\scripts\\restore.bat",
                   {"LCSAS_RELOCATED": "D:\\", "LCSAS_ANOTHER_SET": "0"})
        try:
            run.expect("Newest data disc: ")
            # Eject the META disc, put the newest data disc in: same letter.
            _map_drive(wine.prefix, "d", disc_b)
            run.send("")
            found = run.expect("Which one do you want to restore? [1]: ")
            menu = _menu_lines(found)
            pick = next(i for i, ln in enumerate(menu, 1) if ln == photos.name)
            run.send(str(pick))
            run.answer("Restore to which folder?", "C:\\photos")
            run.answer("Password: ", photos.password)
            run.expect("Restore complete")
        finally:
            rc = run.finish()
        out = run.text
        _assert_clean(out)
        assert rc == 0, f"rc={rc}\n{out}"
        assert f"newest data disc: {LABEL_B} in drive D:" in out, out
        assert sorted(menu) == sorted([docs.name, photos.name]), menu
        assert "Backup set:    Holiday photos" in out, out
        _assert_restored(wine.drive_c / "photos", photos, out)
        assert f"using catalog D:\\catalog.db (a copy in {tools_win})" in out, out
        assert (tools / "catalog.db").read_bytes() == cat_b.read_bytes()
        assert (tools / "merged-from.txt").read_text().strip() == LABEL_B

        # C2: snapshots\ and index\ from the newest disc only, keys\ from
        # both, sets only on that disc copied whole.
        merged = tools / "metadata" / docs.repo_id
        disc_docs = disc_b / "metadata" / docs.repo_id
        for part in ("index", "snapshots"):
            assert _files(merged / part) == _files(disc_docs / part), part
        assert _files(merged / "keys") == sorted(
            set(_files(disc_docs / "keys")) | set(_files(meta_docs / "keys")))
        assert ("0" * 64) not in _files(merged / "index")
        assert _files(tools / "metadata" / photos.repo_id) == _files(
            disc_b / "metadata" / photos.repo_id)
        assert not (tools / "metadata-new").exists()
    finally:
        _make_writable(meta)


def test_newest_of_the_discs_already_in_the_drives(fresh: _Wine, tmp_path: Path) -> None:
    """#605 with two drives: the data discs are in at the start, so there
    is no question, and the newest of them is used, by the label in its
    VOLUME_INFO.txt, not by its drive letter.  Disc A, older, is in E:
    (the last letter, which the catalog rule up to rc5 took); disc B in
    D:.  The documents set has a pack on each.  Disc A also lists a set
    whose name is "Label: LCSAS_TEST_2099_9999", on a line of its own:
    only the first "  Label:" line of VOLUME_INFO.txt is the disc's.

    Not relocated (LCSAS_NO_RELOCATE=1), so the merged copy is made in the
    tools folder and goes with it after the restore.  LCSAS_REPO picks the
    set by its name (case-blind), which here is not its name_cmd, and
    LCSAS_PASSWORD gives the password."""
    wine = fresh
    docs = _docs()
    docs.name = "Family's documents, 2020+"
    assert docs.name_cmd == "Family_s documents, 2020+"
    decoy = _Set("e1d2c3b4-a5f6-4789-8abc-def012345678",
                 "Label: LCSAS_TEST_2099_9999", "decoy-pass-1",
                 {"x.txt": b"decoy\n"})
    for s in (docs, decoy):
        _build_set(tmp_path, s)
    meta = tmp_path / "meta"
    _write_meta(meta, [docs])
    disc_a = tmp_path / "disc_a"
    disc_b = tmp_path / "disc_b"
    docs_packs = [p.name for p in _packs(docs)]
    on_a = {docs_packs[0], *(p.name for p in _packs(decoy))}
    cat_a = _catalog(tmp_path / "cat-a.db", [docs, decoy], None, on_a)
    cat_b = _catalog(tmp_path / "cat-b.db", [docs, decoy], [docs], on_a)
    _write_disc(disc_a, LABEL_A, [docs, decoy], [docs, decoy], cat_a, on_a)
    _write_disc(disc_b, LABEL_B, [docs], [docs], cat_b,
                set(docs_packs) - on_a)
    assert len(docs_packs) == 2 and docs.repo_id < decoy.repo_id
    info_a = (disc_a / "VOLUME_INFO.txt").read_text()
    assert re.search(r"^ +Label: LCSAS_TEST_2099_9999$", info_a, re.M), info_a
    assert info_a.index(f"Label:         {LABEL_A}") < info_a.index("2099")
    _make_read_only(meta)
    try:
        _map_drive(wine.prefix, "g", meta)
        _map_drive(wine.prefix, "d", disc_b)
        _map_drive(wine.prefix, "e", disc_a)
        tools = wine.drive_c / "lcsas tools"
        run = _Run(wine, "G:\\restore.bat", {
            "LCSAS_NO_RELOCATE": "1", "LCSAS_TOOLS_DIR": "C:\\lcsas tools",
            "LCSAS_REPO": "FAMILY'S documents, 2020+",
            "LCSAS_PASSWORD": docs.password,
        })
        try:
            run.answer("Restore to which folder?", "C:\\docs")
            run.expect("Restore complete")
        finally:
            rc = run.finish()
        out = run.text
        _assert_clean(out)
        assert rc == 0, f"rc={rc}\n{out}"
        assert "Newest data disc: " not in out, "asked although discs were in"
        assert f"newest data disc: {LABEL_B} in drive D:" in out, out
        assert "using catalog D:\\catalog.db" in out, out
        assert "This archive contains more than one backup set" not in out, out
        assert f"Backup set:    {docs.name_cmd}" in out, out
        assert "using the password in LCSAS_PASSWORD" in out, out
        assert "Password: " not in out, out
        _assert_restored(wine.drive_c / "docs", docs, out)
        # Not relocated: the merged copy and the catalog copy go, and with
        # them the folder.
        assert not tools.exists(), _files(tools)
    finally:
        _make_writable(meta)


def test_menu_shows_names_never_runs_them(fresh: _Wine, tmp_path: Path) -> None:
    """#606: the menu shows each set's name_cmd from its marker, with its
    split, never the UUID folder name; a set with no marker shows its
    folder name.  A name full of what cmd acts on (& | ^ % ! ( ) < > ")
    is shown as its name_cmd, and nothing in it is run.  A name in
    another script, with no letter A-Z, shows the id.  LCSAS_REPO
    matches the name_cmd too.  Q at the menu ends the run with 65."""
    wine = fresh
    hostile = _Set("71b0c9d8-3e2f-4a1b-9c8d-7e6f5a4b3c2d",
                   'Tom\'s "photos" & <videos> | 100% ^done! (old)',
                   "hostile-pass-1", {"h.txt": b"h\n"}, split=(2, 3))
    accents = _Set("4c3b2a19-0f8e-4d7c-8b6a-594837261504",
                   "Fotos de mamá", "accents-pass-1", {"a.txt": b"a\n"})
    cyrillic = _Set("0a1b2c3d-4e5f-4061-8728-394a5b6c7d8e",
                    "Фото семьи", "cyrillic-pass-1", {"c.txt": b"c\n"})
    bare = _Set("5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9", "unused",
                "bare-pass-1", {"b.txt": b"b\n"})
    for s in (hostile, accents, cyrillic, bare):
        _build_set(tmp_path, s)
    meta = tmp_path / "meta"
    _write_meta(meta, [hostile, accents, cyrillic])
    _put_set(meta, bare, marker=False)
    assert hostile.name_cmd == "Tom_s _photos_ _ _videos_ _ 100_ _done_ _old_"
    assert accents.name_cmd == "Fotos de mam_"
    assert cyrillic.name_cmd == cyrillic.repo_id
    _make_read_only(meta)
    try:
        _map_drive(wine.prefix, "d", meta)
        run = _Run(wine, "D:\\recovery\\scripts\\restore.bat",
                   {"LCSAS_NO_RELOCATE": "1", "LCSAS_NEWEST_DISC_PROMPT": "0"})
        try:
            seen = run.answer("Which one do you want to restore? [1]: ", "q")
        finally:
            rc = run.finish()
        out = run.text
        _assert_clean(out)
        assert rc == 65, f"rc={rc}\n{out}"
        assert sorted(_menu_lines(seen)) == sorted([
            f"{hostile.name_cmd} (password split: any 2 of 3 share cards)",
            accents.name_cmd,
            cyrillic.repo_id,
            bare.repo_id,
        ]), seen
        for s in (hostile, accents):
            assert s.repo_id not in seen, f"{s.name} shown by its id:\n{seen}"
        assert "or Q then ENTER to quit" in seen, seen

        # LCSAS_REPO by name_cmd: no menu.
        run = _Run(wine, "D:\\recovery\\scripts\\restore.bat", {
            "LCSAS_NO_RELOCATE": "1", "LCSAS_NEWEST_DISC_PROMPT": "0",
            "LCSAS_REPO": accents.name_cmd})
        try:
            run.answer("Restore to which folder?", "C:\\accents")
            run.answer("Password: ", "q")
        finally:
            rc = run.finish()
        out = run.text
        assert rc == 77, f"rc={rc}\n{out}"
        assert f"Backup set:    {accents.name_cmd}" in out, out
        assert "No password was given, so nothing was restored." in out, out
    finally:
        _make_writable(meta)


def test_password_asked_again_until_it_is_right(fresh: _Wine, tmp_path: Path) -> None:
    """#608 (C7): an empty answer asks again; a wrong password is caught
    by lcsas-restore --list-snapshots BEFORE the restore starts, said
    plainly, and asked again; the right one restores.  That the password
    is visible is said once."""
    wine = fresh
    docs = _docs()
    _build_set(tmp_path, docs)
    meta = tmp_path / "meta"
    _write_meta(meta, [docs])
    disc_b = tmp_path / "disc_b"
    _write_disc(disc_b, LABEL_B, [docs], [docs],
                _catalog(tmp_path / "cat.db", [docs], None))
    archive = _Archive(meta, {LABEL_B: disc_b}, [docs])
    tools = _tools_folder(wine, archive, "lcsas-pw")
    _map_drive(wine.prefix, "d", disc_b)  # the newest data disc is in
    run = _Run(wine, "C:\\lcsas-pw\\recovery\\scripts\\restore.bat",
               {"LCSAS_RELOCATED": "D:\\"})
    try:
        run.answer("Restore to which folder?", "C:\\docs")
        run.answer("Password: ", "")
        empty = run.answer("Password: ", "not-the-password")
        wrong = run.answer("Password: ", docs.password)
        run.expect("Restore complete")
    finally:
        rc = run.finish()
    out = run.text
    _assert_clean(out)
    assert rc == 0, f"rc={rc}\n{out}"
    assert "Type the password and press ENTER, or Q then ENTER to quit." in empty
    assert ("That password is wrong: it does not open the backup set "
            "Family documents.") in wrong, wrong
    assert "[tier 1] running" not in wrong, "the restore ran on a wrong password"
    assert out.count("[tier 1] running") == 1, out
    assert out.count("is shown on the screen") == 1, out
    _assert_restored(wine.drive_c / "docs", docs, out)
    assert not list(wine.temp.glob("lcsas-pw-*")), "password file left"
    assert (tools / ".done").is_file()


def test_split_password_from_two_of_three_cards(fresh: _Wine, tmp_path: Path) -> None:
    """#607 (C7): the marker says split=2-of-3, so the script asks for
    exactly 2 cards.  The first is typed with a wrong word: lcsas-keyshare
    names it, and only that card is asked again.  Each card is a file of
    its own under %TEMP%, never the tools folder, and none is left."""
    wine = fresh
    photos = _photos(split=(2, 3))
    _build_set(tmp_path, photos)
    meta = tmp_path / "meta"
    _write_meta(meta, [photos])
    disc_b = tmp_path / "disc_b"
    _write_disc(disc_b, LABEL_B, [photos], [photos],
                _catalog(tmp_path / "cat.db", [photos], None))
    tools = _tools_folder(wine, _Archive(meta, {LABEL_B: disc_b}, [photos]),
                          "lcsas-split")
    _map_drive(wine.prefix, "d", disc_b)
    words = photos.shares[0].split()
    typo = " ".join([*words[:4], "zqzq", *words[5:]])
    run = _Run(wine, "C:\\lcsas-split\\recovery\\scripts\\restore.bat",
               {"LCSAS_RELOCATED": "D:\\"})
    try:
        run.answer("Restore to which folder?", "C:\\photos")
        intro = run.answer("Words of card 1: ", typo)
        bad = run.answer("Words of card 1: ", photos.shares[0])
        run.answer("Words of card 2: ", photos.shares[2])
        run.expect("Restore complete")
    finally:
        rc = run.finish()
    out = run.text
    _assert_clean(out)
    assert rc == 0, f"rc={rc}\n{out}"
    assert (f"The password for {photos.name} is split into 3 share cards; you\r\n"
            f"need any 2 of them (Split ID {photos.split_id:05d}).") in intro, intro
    assert "type P then ENTER" in intro, intro
    assert "'zqzq' is not a share word" in bad, bad
    assert "Share card 1 was not typed right" in bad, bad
    assert "Words of card 3" not in out, out
    assert ("Share card 2 of 2: type all the words on one line, then ENTER\r\n"
            "(or Q to quit).") in out, out
    assert "the share cards rebuilt the password" in out, out
    assert "Password: " not in out, out
    _assert_restored(wine.drive_c / "photos", photos, out)
    assert not list(wine.temp.glob("lcsas-shares-*")), "share cards left in %TEMP%"
    assert not list(wine.temp.glob("lcsas-pw-*")), "password file left"
    for word in words[:3]:
        assert not any(word in p.read_text(errors="replace")
                       for p in tools.rglob("*.txt")), "a card in the tools folder"


def test_shares_dir_rebuilds_the_password_without_questions(
    fresh: _Wine, tmp_path: Path,
) -> None:
    """#607 (C4): LCSAS_SHARES_DIR names a folder with one card per file,
    here printed cards, prose and all, and all three of them (more than K
    is fine).  No question is asked for the password or the cards."""
    wine = fresh
    photos = _photos(split=(2, 3))
    _build_set(tmp_path, photos)
    meta = tmp_path / "meta"
    _write_meta(meta, [photos])
    disc_b = tmp_path / "disc_b"
    _write_disc(disc_b, LABEL_B, [photos], [photos],
                _catalog(tmp_path / "cat.db", [photos], None))
    _tools_folder(wine, _Archive(meta, {LABEL_B: disc_b}, [photos]),
                  "lcsas-cards")
    cards = wine.drive_c / "my cards"
    cards.mkdir()
    for n, share in enumerate(photos.shares, 1):
        (cards / f"card {n}.txt").write_text(
            f"LCSAS KEY SHARE {n} of 3\r\nSplit ID : {photos.split_id:05d}\r\n"
            f"\r\n{share}\r\n\r\nKeep this card safe.\r\n")
    _map_drive(wine.prefix, "d", disc_b)
    run = _Run(wine, "C:\\lcsas-cards\\recovery\\scripts\\restore.bat",
               {"LCSAS_RELOCATED": "D:\\",
                "LCSAS_SHARES_DIR": "C:\\my cards"})
    try:
        run.answer("Restore to which folder?", "C:\\photos")
        run.expect("Restore complete")
    finally:
        rc = run.finish()
    out = run.text
    _assert_clean(out)
    assert rc == 0, f"rc={rc}\n{out}"
    assert ("rebuilt the password from the share cards in LCSAS_SHARES_DIR"
            in out), out
    assert "Words of card" not in out and "Password: " not in out, out
    _assert_restored(wine.drive_c / "photos", photos, out)
    assert not list(wine.temp.glob("lcsas-shares-*")), "card copies left"
    assert len(list(cards.iterdir())) == 3, "the reader's cards were touched"


def test_another_set_then_the_tools_folder_goes(fresh: _Wine, tmp_path: Path) -> None:
    """After a good restore the script asks "Restore another backup set
    from these discs?".  Yes goes back to the menu with the tools folder
    as it is (no .done yet, the pack cache still there), and the second
    set goes into a folder of its own by default.  Only "no" ends the
    run: then the pack cache goes and .done tells the relaunch line to
    delete the tools folder (C6)."""
    wine = fresh
    docs, photos = _docs(), _photos()
    for s in (docs, photos):
        _build_set(tmp_path, s)
    meta = tmp_path / "meta"
    _write_meta(meta, [docs, photos])
    disc_b = tmp_path / "disc_b"
    _write_disc(disc_b, LABEL_B, [docs, photos], [docs, photos],
                _catalog(tmp_path / "cat.db", [docs, photos], None))
    tools = _tools_folder(wine, _Archive(meta, {LABEL_B: disc_b},
                                         [docs, photos]), "lcsas-two")
    _map_drive(wine.prefix, "d", disc_b)
    run = _Run(wine, "C:\\lcsas-two\\recovery\\scripts\\restore.bat",
               {"LCSAS_RELOCATED": "D:\\"})
    try:
        first = run.expect("Which one do you want to restore? [1]: ")
        menu = _menu_lines(first)
        run.send(str(menu.index(docs.name) + 1))
        run.answer("Restore to which folder?", "C:\\docs")
        run.answer("Password: ", docs.password)
        run.expect("Restore another backup set from these discs? [y/N]: ")
        # Between the two: nothing cleaned up yet.
        between_done = (tools / ".done").exists()
        between_cache = (tools / "pack-cache").is_dir()
        run.send("y")
        second = run.expect("Which one do you want to restore? [1]: ")
        run.send(str(_menu_lines(second).index(photos.name) + 1))
        folder = run.expect("Restore to which folder?")
        folder += run.expect("]: ")
        run.send("")  # the default: a folder of its own
        run.answer("Password: ", photos.password)
        run.answer("Restore another backup set from these discs? [y/N]: ", "n")
        run.expect("Press any key")
    finally:
        rc = run.finish()
    out = run.text
    _assert_clean(out)
    assert rc == 0, f"rc={rc}\n{out}"
    assert not between_done, "the tools folder was marked for deletion before 'no'"
    assert between_cache, "the pack cache was removed before 'no'"
    _assert_restored(wine.drive_c / "docs", docs, out)
    own = wine.user_dir / "Documents" / f"restored-{photos.name_cmd}"
    assert f"\\Documents\\restored-{photos.name_cmd}]" in folder, folder
    _assert_restored(own, photos, out)
    assert out.count("Restore complete") == 2, out
    assert (tools / ".done").is_file(), "no .done after 'no'"
    assert not (tools / "pack-cache").exists(), "pack cache left after 'no'"
