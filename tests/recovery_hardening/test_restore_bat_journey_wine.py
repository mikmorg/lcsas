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

from tests.recovery_hardening._wine_helpers import stop_and_remove_prefix, stop_wineserver
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
        stop_and_remove_prefix(prefix)


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


def _write_meta(meta: Path, sets: list[_Set],
                created_at: str = "2026-09-01T10:00:00.000000+00:00") -> None:
    """The META disc: both copies of restore.bat, restore.sh (tier 1
    checks it to see the META disc is in), this computer's programs, the
    guide, and the metadata of the sets that existed when it was burned.
    No catalog, no label.  Its volume_info.json says when it was made,
    on a line of its own, as the META builder writes it."""
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
    (meta / "volume_info.json").write_text(json.dumps(
        {"type": "meta", "created_at": created_at}, indent=2))
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
    .lcsas-tools (the folder is the restore's own), restore.bat, this
    computer's programs, the guide, the META disc's metadata.  It runs as
    the relaunch line starts it (LCSAS_RELOCATED)."""
    tools = wine.drive_c / name
    (tools / "recovery" / "scripts").mkdir(parents=True)
    (tools / ".lcsas-tools").write_text("made by the test\r\n")
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
    # A card kept on disk shows up as its words in a row.  Single share
    # words are everyday English and turn up in RECOVER_WINDOWS.txt by
    # chance (the cards are new every run), so look for three in a row,
    # from both typed cards, in every file of the tools folder.
    runs = {" ".join(card.split()[i:i + 3])
            for card in (photos.shares[0], photos.shares[2])
            for i in range(len(card.split()) - 2)}
    for f in (q for q in tools.rglob("*") if q.is_file()):
        text = f.read_text(errors="replace")
        assert not [r for r in runs if r in text], f"a card in the tools folder: {f}"


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


# --------------------------------------------------------------------
# Review fixes: carrying on, the tools folder, disc text, the questions
# --------------------------------------------------------------------

LABEL_C = "LCSAS_TEST_2026_0003"
LAUNCHER = "Resume LCSAS restore.bat"
BOX_END = re.compile(r"\+-{58}\+\r?\n> ")
PACK_RE = re.compile(r"\| Pack ([0-9a-f]{16})\.\.\.")
ANY_LABEL = re.compile(r"LCSAS_TEST_\d{4}_\d{4}")


def _catalog_of(path: Path, s: _Set, discs: list[tuple[str, list[Path]]]) -> Path:
    """A catalog naming the packs on each of ``discs``, as burned on the
    last of them."""
    import sqlite3

    from lcsas.db import schema as db_schema
    from lcsas.db.packs import register_pack
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    db_schema.create_all(conn)
    conn.execute("INSERT OR IGNORE INTO repositories (repo_id, name, mirror_path)"
                 " VALUES (?, ?, ?)", (s.repo_id, s.name, "/srv/backup"))
    conn.commit()
    for label, packs in discs:
        regs = [register_pack(conn, p.name, p.stat().st_size, s.repo_id)
                for p in packs]
        vol = create_volume(conn, label=label, uuid=f"uuid-{label}",
                            media_type="BD25", capacity_bytes=26843545600,
                            status="VERIFIED", commit=False)
        bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in regs],
                        commit=False)
    conn.commit()
    conn.close()
    return path


def _answer_boxes(wine: _Wine, run: _Run, holder: dict[str, Path],
                  quit_at: int | None = None) -> list[str]:
    """Tier 1's disc prompts, once the password is in: at each, the disc
    holding the named pack goes into D: (the one drive) and ENTER is
    pressed; at prompt ``quit_at``, q.  Returns each prompt's text."""
    boxes: list[str] = []
    while True:
        while time.monotonic() < run.deadline:
            text = run.text
            if (len(BOX_END.findall(text)) > len(boxes)
                    or "Press any key" in text[run.pos:]
                    or run.proc.poll() is not None):
                break
            time.sleep(0.2)
        text = run.text
        ends = [m.end() for m in BOX_END.finditer(text)]
        if len(ends) <= len(boxes):
            return boxes
        end = ends[len(boxes)]
        boxes.append(text[text.rfind("| Pack ", 0, end):end])
        if len(boxes) == quit_at or len(boxes) > 6:
            run.send("q")
            continue
        m = PACK_RE.search(boxes[-1])
        disc = holder.get(m.group(1)) if m else None
        if disc is not None:
            _map_drive(wine.prefix, "d", disc)  # eject, insert: same letter
        run.send("")


def _replay_launcher(wine: _Wine, launcher: Path) -> str:
    """The launcher as wine can run it: its (goto) line without the
    `(goto) 2>nul & ` that wine cannot run, every other line as it is."""
    lines = launcher.read_bytes().decode().split("\r\n")
    goto = [i for i, ln in enumerate(lines) if ln.startswith("(goto) 2>nul & ")]
    assert len(goto) == 1, lines
    lines[goto[0]] = lines[goto[0]][len("(goto) 2>nul & "):]
    (wine.drive_c / "resume-replay.bat").write_bytes("\r\n".join(lines).encode())
    return "C:\\resume-replay.bat"


def _quick(wine: _Wine, cmd: str, env: dict[str, str]) -> tuple[int, str]:
    """A run that should end at the start: two ENTERs as its whole input
    (wine's set /p takes all of it at the first question)."""
    res = subprocess.run(["wine", "cmd", "/c", cmd], env={**wine.env, **env},
                         cwd=str(wine.drive_c), input=b"\n\n",
                         capture_output=True, timeout=DEADLINE_S)
    return res.returncode, (res.stdout + res.stderr).decode("utf-8", "replace")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def test_three_discs_one_drive_carry_on_keeps_the_newest_catalog(
    fresh: _Wine, tmp_path: Path,
) -> None:
    """The one-drive journey with THREE data discs (#605, #609, #617):
    the newest-disc step reads disc 3, q at the first disc prompt stops
    the restore (75), and "Resume LCSAS restore.bat" carries it on with
    the OLDEST disc already in the drive.

    The run that carries on must keep the catalog copy taken from disc 3.
    It used to copy the catalog of whatever disc was in a drive over it:
    disc 1's, which has no record of disc 2's packs, so the prompt for
    disc 2 named no disc (F9 again).  Here every prompt names its disc.

    The discs are read-only, as discs are.  A .done that an earlier run
    could not delete is in the tools folder at the start: it must not
    outlive the stop, or the folder would go with it."""
    wine = fresh
    docs = _docs()
    docs.files = {f"part-{i}.bin": bytes([65 + i]) * 2500 for i in range(6)}
    sys.path.insert(0, str(RECOVERY / "tests"))
    import test_e2e
    docs.repo = tmp_path / f"repo-{docs.repo_id}"
    test_e2e.build_repo(docs.repo, docs.password, docs.files, split_packs=3)
    packs = _packs(docs)
    assert len(packs) == 3, packs
    labels = (LABEL_A, LABEL_B, LABEL_C)
    discs: dict[str, Path] = {}
    catalogs: dict[str, Path] = {}
    for i, label in enumerate(labels):
        cat = _catalog_of(tmp_path / f"cat-{i}.db", docs,
                          [(lb, [packs[j]]) for j, lb in enumerate(labels[:i + 1])])
        stamp = 1_790_000_000 + i * 86_400  # each disc burned a day later
        os.utime(cat, (stamp, stamp))
        disc = tmp_path / f"disc_{i}"
        _write_disc(disc, label, [docs], [docs], cat, only={packs[i].name})
        discs[label] = disc
        catalogs[label] = cat
    holder = {p.name[:16]: discs[lb] for p, lb in zip(packs, labels, strict=True)}
    meta = tmp_path / "meta"
    _write_meta(meta, [docs])
    tools = _tools_folder(wine, _Archive(meta, discs, [docs]), "lcsas-three")
    tools_win = "C:\\lcsas-three"
    (tools / ".done").write_text("")
    for root in (meta, *discs.values()):
        _make_read_only(root)
    try:
        # --- 1. the step reads disc 3; q at the first disc prompt --------
        _map_drive(wine.prefix, "d", meta)
        run = _Run(wine, f"{tools_win}\\recovery\\scripts\\restore.bat",
                   {"LCSAS_RELOCATED": "D:\\"})
        try:
            run.expect("Newest data disc: ")
            _map_drive(wine.prefix, "d", discs[LABEL_C])
            run.send("")
            run.answer("Restore to which folder?", "C:\\three")
            run.answer("Password: ", docs.password)
            first = _answer_boxes(wine, run, holder, quit_at=1)
        finally:
            rc = run.finish()
        out = run.text
        _assert_clean(out)
        assert rc == 75, f"rc={rc}\n{out}"
        assert f"newest data disc: {LABEL_C} in drive D:" in out, out
        assert len(first) == 1 and ANY_LABEL.search(first[0]), first
        assert (tools / "catalog.db").read_bytes() == catalogs[LABEL_C].read_bytes()
        assert not (tools / ".done").exists(), "a stale .done outlived the stop"
        assert (tools / "resume.cfg").is_file(), out
        assert (wine.desktop / LAUNCHER).is_file(), out

        # --- 2. carry on, with the OLDEST disc in the drive ---------------
        _map_drive(wine.prefix, "d", discs[LABEL_A])
        run = _Run(wine, _replay_launcher(wine, wine.desktop / LAUNCHER))
        try:
            before = run.answer("Password: ", docs.password)
            boxes = _answer_boxes(wine, run, holder)
        finally:
            rc = run.finish()
        out = run.text
        _assert_clean(out)
        assert rc == 0, f"rc={rc}\n{out}"
        assert "Carrying on the restore into:" in before, before
        assert f"using the catalog copy in {tools_win}" in before, before
        assert "using catalog D:\\catalog.db" not in before, before
        assert boxes, f"no disc prompt: disc 2 was never asked for\n{out}"
        for box in boxes:
            assert LABEL_B in box and "no record" not in box, box
        _assert_restored(wine.drive_c / "three", docs, out)
        assert not tools.exists(), f"tools folder left: {_files(tools)}"
        assert not (wine.desktop / LAUNCHER).exists(), "Desktop launcher left"
    finally:
        for root in (meta, *discs.values()):
            _make_writable(root)


def test_tools_folder_must_be_the_restores_own(fresh: _Wine, tmp_path: Path) -> None:
    """#609 (C4, C6): the tools folder is deleted once a restore has
    finished, so restore.bat uses only a folder that is new, empty or its
    own (.lcsas-tools).  It used to check only a folder LCSAS_TOOLS_DIR
    named, only on the copy from a disc, and only when it held no
    recovery\\ or pack-cache\\: a META disc copied to a USB stick and named
    there had its recovery\\ and metadata\\ deleted.

    1. LCSAS_TOOLS_DIR names a META copy with the reader's own file in
       it: refused (exit 1), nothing in it changed.
    2. The same folder with LCSAS_NO_RELOCATE=1 (no copy at all).
    3. LCSAS_TOOLS_DIR=F:\\, a whole drive.
    4. A run that does not relocate, while a stopped restore waits in the
       usual tools folder: it restores using TOOLS\\in-place, and every
       file of the stopped restore is still there afterwards."""
    wine = fresh
    docs = _docs()
    _build_set(tmp_path, docs)
    meta = tmp_path / "meta"
    _write_meta(meta, [docs])
    disc_b = tmp_path / "disc_b"
    _write_disc(disc_b, LABEL_B, [docs], [docs],
                _catalog(tmp_path / "cat.db", [docs], None))
    usb = wine.drive_c / "usb-meta"
    (usb / "recovery" / "scripts").mkdir(parents=True)
    shutil.copy2(RESTORE_BAT, usb / "recovery" / "scripts" / "restore.bat")
    (usb / "metadata" / docs.repo_id).mkdir(parents=True)
    (usb / "metadata" / docs.repo_id / "keep.txt").write_text("the reader's\n")
    (usb / "my notes.txt").write_text("keep me\n")
    usb_before = _snapshot(usb)
    root_f = tmp_path / "drive_f"
    (root_f / "recovery").mkdir(parents=True)
    (root_f / "recovery" / "notes.txt").write_text("on the stick\n")
    tools = wine.user_dir / "AppData" / "Local" / "LCSAS-restore"
    tools_win = f"C:\\users\\{wine.user}\\AppData\\Local\\LCSAS-restore"
    _make_read_only(meta)
    try:
        _map_drive(wine.prefix, "d", meta)
        _map_drive(wine.prefix, "f", root_f)
        quick = {"LCSAS_NEWEST_DISC_PROMPT": "0", "LCSAS_PASSWORD": "x"}
        for n, env in enumerate((
            {"LCSAS_TOOLS_DIR": "C:\\usb-meta"},
            {"LCSAS_TOOLS_DIR": "C:\\usb-meta", "LCSAS_NO_RELOCATE": "1"},
            {"LCSAS_TOOLS_DIR": "F:\\"},
        ), 1):
            rc, out = _quick(wine, "D:\\restore.bat", {**quick, **env})
            assert rc == 1, f"case {n}: rc={rc}\n{out}"
            assert "a folder that already holds other files, or a whole drive" in out, (
                f"case {n}:\n{out}")
            assert "Restore to which folder?" not in out, f"case {n}:\n{out}"
            assert _snapshot(usb) == usb_before, f"case {n}: the USB copy changed"
            assert (root_f / "recovery" / "notes.txt").is_file(), f"case {n}"

        # --- 4. a stopped restore waits in the usual tools folder ---------
        (tools / "metadata" / docs.repo_id).mkdir(parents=True)
        (tools / "metadata" / docs.repo_id / "keep.txt").write_text("stopped\n")
        (tools / "pack-cache" / "ab").mkdir(parents=True)
        (tools / "pack-cache" / "ab" / "abcd.pack").write_bytes(b"cached")
        (tools / "catalog.db").write_bytes(b"the stopped run's catalog")
        (tools / "resume.cfg").write_text(
            f"set={docs.repo_id}\r\ntarget=C:\\stopped\r\nmeta=D:\\\r\nletters= D\r\n")
        (tools / ".lcsas-tools").write_text("made by restore.bat\r\n")
        stopped = _snapshot(tools)
        _map_drive(wine.prefix, "e", disc_b)
        run = _Run(wine, "D:\\recovery\\scripts\\restore.bat", {
            "LCSAS_NO_RELOCATE": "1", "LCSAS_PASSWORD": docs.password,
            "LCSAS_ANOTHER_SET": "0"})
        try:
            run.answer("Restore to which folder?", "C:\\docs-here")
            run.expect("Restore complete")
        finally:
            rc = run.finish()
        out = run.text
        _assert_clean(out)
        assert rc == 0, f"rc={rc}\n{out}"
        assert f"using catalog E:\\catalog.db (a copy in {tools_win}\\in-place)" in out, out
        _assert_restored(wine.drive_c / "docs-here", docs, out)
        after = _snapshot(tools)
        # .lcsas-tools is written again when the folder stays: by name.
        assert after.pop(".lcsas-tools", None) is not None, _files(tools)
        stopped.pop(".lcsas-tools")
        assert after == stopped, "the stopped restore's files changed"
        assert not (tools / "in-place").exists(), _files(tools)
    finally:
        _make_writable(meta)


def test_disc_text_with_a_quote_is_never_run(fresh: _Wine, tmp_path: Path) -> None:
    """The label in a disc's VOLUME_INFO.txt, and the name_cmd=, split=
    and split_id= lines of a set's LCSAS_REPO.txt, are text from a disc.
    restore.bat put them in lines with %...%, in quotes: a '"' in them
    ended the quotes there, and the rest of the line ran (#605, #606).
    LCSAS writes none of them with a quote; a disc made by hand can.
    Here each one carries a command that would leave a file in C:\\.
    None runs, the disc counts as one with no label, and its sets show
    by their ids or by a name_cmd that is clean."""
    wine = fresh
    docs = _docs()
    _build_set(tmp_path, docs)
    meta = tmp_path / "meta"
    _write_meta(meta, [docs])
    odd = tmp_path / "odd_disc"
    (odd / "data" / "00").mkdir(parents=True)
    (odd / "VOLUME_INFO.txt").write_text(
        "LCSAS VOLUME INFORMATION\n"
        '  Label:         X"&echo INJ>C:\\inj_label.txt&rem "\n')
    (odd / "volume_info.json").write_text(json.dumps({"uuid": "u-odd"}))
    odd_one = "b8e1f0a2-7c3d-4e59-a6b4-1d2c3e4f5a6b"
    odd_two = "c9f2a1b3-8d4e-4f6a-b7c5-2e3d4f5a6b7c"
    markers = {
        odd_one: ('name=Odd one\n'
                  'name_cmd=Odd"&echo INJ>C:\\inj_nc.txt&rem "\n'
                  "split=2-of-3\n"
                  'split_id=1"&echo INJ>C:\\inj_sid.txt&rem "\n'),
        odd_two: ("name=Odd two\n"
                  "name_cmd=Odd two\n"
                  'split=1"&echo INJ>C:\\inj_split.txt&rem "-of-3\n'
                  "split_id=\n"),
    }
    for set_id, lines in markers.items():
        dst = odd / "metadata" / set_id
        for part in ("keys", "index", "snapshots"):
            (dst / part).mkdir(parents=True)
        (dst / "LCSAS_REPO.txt").write_text(
            "# LCSAS backup set marker. Restore works without this file.\n"
            f"format=1\nid={set_id}\n{lines}")
    _make_read_only(meta)
    try:
        _map_drive(wine.prefix, "d", meta)
        _map_drive(wine.prefix, "e", odd)
        run = _Run(wine, "D:\\recovery\\scripts\\restore.bat",
                   {"LCSAS_NO_RELOCATE": "1"})
        try:
            seen = run.answer("Which one do you want to restore? [1]: ", "q")
        finally:
            rc = run.finish()
        out = run.text
        injected = sorted(p.name for p in wine.drive_c.glob("inj_*.txt"))
        assert not injected, f"disc text ran as a command: {injected}\n{out}"
        _assert_clean(out)
        assert rc == 65, f"rc={rc}\n{out}"
        assert "newest data disc:" not in out, out
        assert sorted(_menu_lines(seen)) == sorted([
            docs.name_cmd,
            f"{odd_one} (password split: any 2 of 3 share cards)",
            "Odd two",
        ]), seen
    finally:
        _make_writable(meta)


def test_folder_question_takes_q_and_wants_a_whole_path(
    fresh: _Wine, tmp_path: Path,
) -> None:
    """The folder question (#615).  Q then ENTER stops (65, nothing
    made), as at every other question: "q" used to become the folder
    C:\\q, and the files were restored there.  A folder typed without its
    drive ("restored") is asked again: it would be made wherever cmd was.
    And a restore folder that cannot be written to is reported by tier 1,
    never with "Access is denied" from the script's own marker (#611)."""
    wine = fresh
    docs = _docs()
    _build_set(tmp_path, docs)
    meta = tmp_path / "meta"
    _write_meta(meta, [docs])
    disc_b = tmp_path / "disc_b"
    _write_disc(disc_b, LABEL_B, [docs], [docs],
                _catalog(tmp_path / "cat.db", [docs], None))
    env = {"LCSAS_NO_RELOCATE": "1", "LCSAS_NEWEST_DISC_PROMPT": "0"}
    _make_read_only(meta)
    read_only = wine.drive_c / "read-only"
    read_only.mkdir()
    try:
        _map_drive(wine.prefix, "d", meta)
        run = _Run(wine, "D:\\recovery\\scripts\\restore.bat", env)
        try:
            ask = run.answer("Restore to which folder?", "restored")
            again = run.answer("Restore to which folder?", "q")
            run.expect("Press any key")
        finally:
            rc = run.finish()
        out = run.text
        _assert_clean(out)
        assert rc == 65, f"rc={rc}\n{out}"
        assert "Q then ENTER quits" in ask, ask
        assert "restored is not a whole path" in again, again
        assert "Stopped; nothing was restored." in out, out
        assert "Password: " not in out, out
        for made in ("q", "restored"):
            assert not (wine.drive_c / made).exists(), f"C:\\{made} was made"

        # A restore folder that cannot be written to.
        os.chmod(read_only, 0o555)
        _map_drive(wine.prefix, "e", disc_b)
        run = _Run(wine, "D:\\recovery\\scripts\\restore.bat",
                   {**env, "LCSAS_PASSWORD": docs.password})
        try:
            run.answer("Restore to which folder?", "C:\\read-only")
            run.expect("Press any key")
        finally:
            rc = run.finish()
        out = run.text
        _assert_clean(out)
        assert rc != 0, f"rc={rc}\n{out}"
        assert "[tier 1] running" in out, out
        assert "The restore stopped before it finished" in out, out
    finally:
        os.chmod(read_only, 0o755)
        _make_writable(meta)


def test_tools_folder_from_another_meta_disc_is_replaced(
    fresh: _Wine, tmp_path: Path,
) -> None:
    """C6: a tools folder is used again as it is only by a run from the
    very same META disc (its volume_info.json "created_at", kept in
    archive.txt).  A folder left by another archive's META disc, or an
    older META disc, holds that disc's sets, its catalog copy and its
    merged-from.txt, whose labels repeat from one archive to the next.
    restore.bat used to reuse it whenever restore.bat itself was the
    same.  (wine's fc calls any two files the same, so this checks the
    META disc alone.)  Each run stops with Q at the newest-disc step."""
    wine = fresh
    docs, photos = _docs(), _photos()
    for s in (docs, photos):
        _build_set(tmp_path, s)
    meta_a, meta_b = tmp_path / "meta_a", tmp_path / "meta_b"
    _write_meta(meta_a, [docs], created_at="2026-09-01T10:00:00.000000+00:00")
    _write_meta(meta_b, [docs, photos], created_at="2026-10-15T18:30:00.000000+00:00")
    tools = wine.user_dir / "AppData" / "Local" / "LCSAS-restore"
    for meta in (meta_a, meta_b):
        _make_read_only(meta)
    try:
        said = []
        for meta in (meta_a, meta_b, meta_b):
            _map_drive(wine.prefix, "d", meta)
            run = _Run(wine, "D:\\restore.bat")
            try:
                run.answer("Newest data disc: ", "q")
            finally:
                rc = run.finish()
            out = run.text
            _assert_clean(out)
            assert rc == 65, f"rc={rc}\n{out}"
            said.append("copied" if "copied recovery files to" in out
                        else "reused" if "using the recovery files already copied" in out
                        else out)
        assert said == ["copied", "copied", "reused"], said
        assert (tools / "archive.txt").read_text().strip() == (
            "2026-10-15T18:30:00.000000+00:00")
        assert (tools / "metadata" / photos.repo_id).is_dir(), _files(tools)
        assert (tools / ".lcsas-tools").is_file()
    finally:
        for meta in (meta_a, meta_b):
            _make_writable(meta)


def test_read_only_password_file_and_leftovers_in_temp(
    fresh: _Wine, tmp_path: Path,
) -> None:
    """#608.  A window closed while a password or typed share cards were
    in %TEMP% left them there for good: each run picks new names.  The
    first pass now removes every lcsas-pw-*.txt and lcsas-shares-* there
    (read-only ones too).

    LCSAS_PWFILE names a read-only file with a wrong password: the script
    says so, and the password typed next restores.  On Windows the copy
    of a read-only file is read-only, and the plain del and `>` used
    before could not replace it: the wrong password was tried for ever.
    (wine's copy does not keep the mark, so here this part only shows
    the path works; the static pins guard the del /f and attrib -r.)"""
    wine = fresh
    docs = _docs()
    _build_set(tmp_path, docs)
    meta = tmp_path / "meta"
    _write_meta(meta, [docs])
    disc_b = tmp_path / "disc_b"
    _write_disc(disc_b, LABEL_B, [docs], [docs],
                _catalog(tmp_path / "cat.db", [docs], None))
    _tools_folder(wine, _Archive(meta, {LABEL_B: disc_b}, [docs]), "lcsas-ro-pw")
    _map_drive(wine.prefix, "d", disc_b)
    old_pw = wine.temp / "lcsas-pw-4242-0707.txt"
    old_pw.write_text("left by a closed window\r\n")
    old_pw.chmod(0o444)
    old_cards = wine.temp / "lcsas-shares-31337"
    old_cards.mkdir()
    (old_cards / "card1.txt").write_text("academic acid acne\r\n")
    pw_file = wine.drive_c / "read-only-pw.txt"
    pw_file.write_bytes(b"not-the-password\r\n")
    pw_file.chmod(0o444)
    try:
        run = _Run(wine, "C:\\lcsas-ro-pw\\recovery\\scripts\\restore.bat",
                   {"LCSAS_RELOCATED": "D:\\", "LCSAS_PWFILE": "C:\\read-only-pw.txt"})
        try:
            run.answer("Restore to which folder?", "C:\\docs-ro")
            wrong = run.answer("Password: ", docs.password)
            run.expect("Restore complete")
        finally:
            rc = run.finish()
        out = run.text
        _assert_clean(out)
        assert rc == 0, f"rc={rc}\n{out}"
        assert "using the password in the file LCSAS_PWFILE names" in wrong, wrong
        assert "That password is wrong" in wrong, wrong
        _assert_restored(wine.drive_c / "docs-ro", docs, out)
        left = sorted(p.name for p in wine.temp.glob("lcsas-*"))
        assert left == [], f"left in %TEMP%: {left}"
        assert pw_file.read_bytes() == b"not-the-password\r\n", "the reader's file changed"
    finally:
        for p in (pw_file, old_pw):
            with contextlib.suppress(OSError):
                p.chmod(0o644)


def test_q_at_a_disc_prompt_stops_a_set_with_its_own_data(
    fresh: _Wine, tmp_path: Path,
) -> None:
    """C5 (#615, #617): q at a disc prompt (75) ends the run also for a
    set whose folder holds a data\\ folder of its own, as a set copied
    whole holds: tier 2 must not run then, although it would for another
    failure.  The folder holds one pack of three; the others are on no
    disc in a drive.  A decoy tier 2 would say "[tier 2] running"."""
    wine = fresh
    docs = _docs()
    _build_set(tmp_path, docs)
    root = tmp_path / "whole"
    _write_meta(root, [])
    win = root / "recovery" / "bin" / TRIPLE
    (win / "rustic-static.exe").write_bytes(b"MZ decoy")
    dst = _put_set(root, docs)
    first = _packs(docs)[0]
    (dst / "data" / first.name[:2]).mkdir(parents=True)
    shutil.copy2(first, dst / "data" / first.name[:2] / first.name)
    _map_drive(wine.prefix, "d", root)
    run = _Run(wine, "D:\\recovery\\scripts\\restore.bat", {
        "LCSAS_NEWEST_DISC_PROMPT": "0", "LCSAS_PASSWORD": docs.password})
    try:
        run.answer("Restore to which folder?", "C:\\whole-set")
        boxes = _answer_boxes(wine, run, {}, quit_at=1)
    finally:
        rc = run.finish()
    out = run.text
    _assert_clean(out)
    assert len(boxes) == 1, out
    assert rc == 75, f"rc={rc}\n{out}"
    assert "The restore stopped before it finished (code 75)." in out, out
    assert "trying tier 2" not in out and "[tier 2]" not in out, out
