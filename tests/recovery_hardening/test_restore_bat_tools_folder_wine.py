r"""restore.bat's tools folder under wine (#609, #611, #612, #613, #615).

Release drill #476 ran restore.bat from a META disc on a Windows 10 VM
with ONE optical drive.  Its start printed "Access is denied." (#611),
then it copied the whole recovery\bin, 744 MB for six kinds of computer,
without a word (#612), into a random %TEMP% folder that every run
deleted, so carrying on meant the META disc and the whole copy again
(#609).  A click in the window paused it, and nothing said so (#613).

Now the copy goes to the tools folder, %LOCALAPPDATA%\LCSAS-restore
(LCSAS_TOOLS_DIR names another), holds only this computer's programs,
names each on screen, and stays until the restore has finished.  A
restore that stopped carries on from it with "Resume LCSAS restore.bat",
with no META disc.

What wine can and cannot run here:

* The copy itself: it uses copy and xcopy, which wine runs.  The META
  folder is made read-only, so the write probe fails as on a disc.
* NOT the relaunch line.  wine's cmd does not end a batch at
  ``(goto) 2>nul``: it skips the rest of the line and carries on from
  the disc, not relocated, and with its stderr still going to nul, so
  tier 1's disc prompt (on stderr) is never seen.  So a run from the
  META folder checks the copy and stops at the password prompt (an
  empty password ends it), and the later runs start the copy in the
  tools folder the way the relaunch line does (LCSAS_RELOCATED=D:\).
* NOT the launcher's own ``(goto)`` either: the resume step runs the
  launcher's line as it stands, minus that first command.
* wine's ``call`` hands back 1 for any non-zero exit code, and its ``fc``
  answers "same" for any two files.  So the exit code 75 is checked on
  runs that do not go through ``call``, and the "replace a tools folder
  from another version" branch is left to windows-e2e.

windows-e2e (Invoke-RelocationLeg, Invoke-StopAndResumeLeg) runs all of
it on a real Windows runner, the relaunch and launcher lines included.

Wine's ``set /p`` takes everything waiting on stdin at its first read, so
the harness writes each answer only once its prompt is on screen.
LCSAS_INTERACTIVE=on makes tier 1 ask at the disc prompt although its
stdin is a pipe.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
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
WIN_BIN = RECOVERY / "bin" / "x86_64-windows"
PROGRAMS = ("lcsas-ecc.exe", "lcsas-keyshare.exe", "lcsas-restore.exe")
GUIDE = RECOVERY / "docs" / "RECOVER_WINDOWS.txt"

TRIPLE = "x86_64-pc-windows-gnu"
# Invented for this test: a UUID id and a name that is not the id, as
# real discs have them.
SET_ID = "5b0e7c2a-9f14-4d3b-8a61-2c7e40d9f3b5"
SET_NAME = "Family documents"
PASSWORD = "stop-and-carry-on"
LABELS = {"disc_a": "LCSAS_TEST_2026_0001", "disc_b": "LCSAS_TEST_2026_0002"}
FILES = {
    "a0.txt": b"file zero\n" * 400,
    "a1.txt": b"file one\n" * 400,
    "a2.bin": bytes(range(256)) * 16,
    "a3.txt": b"file three\n" * 400,
}
TARGET_WIN = "C:\\restored"
LAUNCHER = "Resume LCSAS restore.bat"
DEADLINE_S = 240
MAX_BOXES = 6

BOX_END = re.compile(r"\+-{58}\+\r?\n> ")
PACK_RE = re.compile(r"\| Pack ([0-9a-f]{16})\.\.\.")
ACCESS_DENIED = re.compile(r"Access (is )?denied", re.IGNORECASE)

_WINE_OK = shutil.which("wine") is not None
_BINS_OK = all((WIN_BIN / p).is_file() for p in PROGRAMS)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_WINE_OK and RESTORE_BAT.is_file() and _BINS_OK),
        reason=(
            f"restore.bat tools-folder wine test requires wine "
            f"(present={_WINE_OK}), {RESTORE_BAT} and the Windows programs "
            f"in {WIN_BIN}"
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


def _make_writable(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(dirpath, name), 0o644)


def _make_read_only(root: Path) -> None:
    """A disc: nothing in it can be written, so the write probe fails."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            os.chmod(os.path.join(dirpath, name), 0o444)
        os.chmod(dirpath, 0o555)


@dataclass
class _Fixture:
    prefix: Path
    env: dict[str, str]
    meta: Path
    discs: dict[str, Path]
    empty: Path
    user: str

    @property
    def drive_c(self) -> Path:
        return self.prefix / "drive_c"

    @property
    def user_dir(self) -> Path:
        return self.drive_c / "users" / self.user

    @property
    def default_tools(self) -> Path:
        return self.user_dir / "AppData" / "Local" / "LCSAS-restore"

    @property
    def default_tools_win(self) -> str:
        return f"C:\\users\\{self.user}\\AppData\\Local\\LCSAS-restore"

    @property
    def desktop(self) -> Path:
        return self.user_dir / "Desktop"

    @property
    def target(self) -> Path:
        return self.drive_c / "restored"

    def disc_holding(self, pack_prefix: str) -> Path | None:
        for disc in self.discs.values():
            if any(p.name.startswith(pack_prefix)
                   for p in (disc / "data").rglob("*") if p.is_file()):
                return disc
        return None


@pytest.fixture
def fx(tmp_path: Path):
    """A read-only META folder holding programs for several kinds of
    computer, two data-disc folders, and a wine prefix with D: on the
    META folder (the one optical drive)."""
    sys.path.insert(0, str(RECOVERY / "tests"))
    import test_e2e  # sibling fixture builders
    import test_multidisc

    from lcsas.restore.restic_fallback import PurePythonRestorer
    from lcsas.staging.repo_marker import RepoMarker, write_repo_marker

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
        dst = root / "metadata" / SET_ID
        shutil.copytree(repo, dst, ignore=shutil.ignore_patterns("data"))
        write_repo_marker(dst, RepoMarker(repo_id=SET_ID, name=SET_NAME))

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

    # The META disc, laid out as on a real one: both copies of
    # restore.bat, restore.sh (tier 1 checks it to see the META disc is
    # still in), programs for this computer AND for others, the guide.
    meta = tmp_path / "meta"
    scripts = meta / "recovery" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RESTORE_BAT, scripts / "restore.bat")
    shutil.copy2(RESTORE_BAT, meta / "restore.bat")
    (scripts / "restore.sh").write_text("#!/bin/sh\n")
    win = meta / "recovery" / "bin" / TRIPLE
    win.mkdir(parents=True)
    for prog in PROGRAMS:
        shutil.copy2(WIN_BIN / prog, win / prog)
    # What a Windows restore never runs: tier 2 (no set here holds its
    # own data\), restic, the Python of tier 3.
    (win / "rustic-static.exe").write_bytes(b"MZ not copied")
    (win / "restic.exe").write_bytes(b"MZ not copied")
    (win / "python").mkdir()
    (win / "python" / "python.exe").write_bytes(b"MZ not copied")
    for other in ("x86_64-unknown-linux-musl", "aarch64-apple-darwin",
                  "x86_64-windows"):
        (meta / "recovery" / "bin" / other).mkdir()
        (meta / "recovery" / "bin" / other / "lcsas-restore").write_bytes(b"\x7fELF")
    (meta / "recovery" / "docs").mkdir()
    shutil.copy2(GUIDE, meta / "recovery" / "docs" / "RECOVER_WINDOWS.txt")
    copy_set(meta)
    (meta / "volume_info.json").write_text(json.dumps({"type": "meta"}))

    empty = tmp_path / "empty_drive"
    empty.mkdir()

    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)
    users = [p.name for p in (prefix / "drive_c" / "users").iterdir()
             if p.is_dir() and p.name.lower() != "public"]
    assert len(users) == 1, users
    run_env = {k: v for k, v in env.items()
               if not k.startswith("LCSAS_") and k.upper() != "TARGET"}
    run_env.update({"LCSAS_TARGET": TRIPLE, "LCSAS_INTERACTIVE": "on"})

    _make_read_only(meta)
    _map_drive(prefix, "d", meta)  # the META disc is in the one drive
    fixture = _Fixture(prefix=prefix, env=run_env, meta=meta, discs=discs,
                       empty=empty, user=users[0])
    # _init_prefix made the Desktop a folder of the prefix's own: the
    # restore writes its launcher there.
    assert fixture.desktop.is_dir() and not fixture.desktop.is_symlink()
    try:
        yield fixture
    finally:
        _make_writable(meta)


class _Run:
    """One wine command with its stdin held open and its output pumped."""

    def __init__(self, fx: _Fixture, cmd: list[str],
                 extra_env: dict[str, str] | None = None) -> None:
        self.proc = subprocess.Popen(
            cmd, env={**fx.env, **(extra_env or {})}, cwd=str(fx.drive_c),
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


def _drive(fx: _Fixture, run: _Run, *, target_win: str | None = TARGET_WIN,
           quit_at: int | None = None) -> list[str]:
    """Answer restore.bat's prompts, then tier 1's disc prompts.

    ``target_win`` None: a resumed restore, which asks no folder.  At each
    disc prompt the disc holding the named pack goes into D: (the
    symlink is re-pointed) and ENTER is pressed; at prompt ``quit_at``,
    q.  Returns the text of every disc prompt, in order.
    """
    if target_win is not None:
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


def _run_from_the_disc(fx: _Fixture,
                       extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    """restore.bat from the META folder in D:, as a double-click starts
    it, up to the password prompt; an empty password ends the run (1).
    Under wine the run goes on from the disc after the copy (see the
    module notes), so tier 1 is never reached this way."""
    run = _Run(fx, ["wine", "cmd", "/c", "D:\\restore.bat"], extra_env)
    try:
        assert run.wait_for(lambda t: "Restore to which folder?" in t), run.text
        run.send(TARGET_WIN)
        assert run.wait_for(lambda t: "Password:" in t), run.text
        run.send("")
        run.wait_for(lambda t: "Press any key" in t)
    finally:
        rc = run.finish()
    out = run.text
    assert rc == 1 and "ERROR: empty password" in out, f"rc={rc}\n{out}"
    assert "[tier 1]" not in out, out
    return rc, out


def _assert_quiet(out: str) -> None:
    """#611: no "Access is denied." (wine says "Access denied.")."""
    assert not ACCESS_DENIED.search(out), out


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix()
                  for p in root.rglob("*") if p.is_file())


def _resume_cfg(tools: Path) -> dict[str, str]:
    raw = (tools / "resume.cfg").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), raw
    out = {}
    for line in raw.decode().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key] = value
    return out


def _replay_launcher(fx: _Fixture, launcher: Path) -> Path:
    """The launcher as wine can run it: its (goto) line without the
    `(goto) 2>nul & ` that wine cannot run, every other line as it is."""
    lines = launcher.read_bytes().decode().split("\r\n")
    goto = [i for i, ln in enumerate(lines) if ln.startswith("(goto) 2>nul & ")]
    assert len(goto) == 1, lines
    lines[goto[0]] = lines[goto[0]][len("(goto) 2>nul & "):]
    replay = fx.drive_c / "resume-replay.bat"
    replay.write_bytes("\r\n".join(lines).encode())
    return replay


def test_stop_and_carry_on_from_the_tools_folder(fx: _Fixture) -> None:
    """The whole journey on one drive: relocate from the META disc, stop
    with q at a disc prompt, carry on later with no META disc.

    1. From the META disc (read-only): the copy holds only this computer's
       programs, is named on screen, and nothing says "Access denied".
    2. The copy in the tools folder, as the relaunch line runs it: q at
       the second disc prompt ends tier 1 with 75; the tools folder stays,
       with resume.cfg (no password) and the launcher, also on the
       Desktop.
    3. The launcher, with the META disc's drive EMPTY at the start (so
       only resume.cfg's letters can find it) and a data disc put in at
       the prompt: the restore finishes, and the tools folder and the
       Desktop launcher are gone."""
    tools = fx.default_tools
    tools_win = fx.default_tools_win

    # --- 1. the copy, from the META disc -----------------------------
    rc, out = _run_from_the_disc(fx)
    _assert_quiet(out)
    note = out.index("PAUSES the restore")
    assert note < out.index("copying the restore programs for this computer"), out
    assert f"copied recovery files to {tools_win}\r\n" in out, out
    bin_dir = tools / "recovery" / "bin"
    assert [p.name for p in bin_dir.iterdir()] == [TRIPLE], _files(tools)
    assert sorted(p.name for p in (bin_dir / TRIPLE).iterdir()) == list(PROGRAMS), (
        _files(tools))
    for prog in PROGRAMS:
        assert (bin_dir / TRIPLE / prog).read_bytes() == (WIN_BIN / prog).read_bytes()
        assert f"recovery\\bin\\{TRIPLE}\\{prog}" in out, out
    for other in ("x86_64-unknown-linux-musl", "aarch64-apple-darwin",
                  "x86_64-windows", "rustic-static.exe", "restic.exe", "python.exe"):
        assert other not in out, f"{other} named in the copy:\n{out}"
    assert (tools / "recovery" / "docs" / "RECOVER_WINDOWS.txt").read_bytes() == (
        GUIDE.read_bytes())
    assert (tools / "recovery" / "scripts" / "restore.bat").read_bytes() == (
        RESTORE_BAT.read_bytes())
    assert f"metadata\\{SET_ID}" in out, out
    assert (tools / "metadata" / SET_ID / "LCSAS_REPO.txt").is_file()
    assert (tools / "metadata" / SET_ID / "keys").is_dir()
    assert not list(fx.meta.glob("lcsas-probe-*")), "probe file left on the disc"
    # Not finished: the tools folder stays.
    assert (tools / "recovery" / "scripts" / "restore.bat").is_file()

    # --- 2. the copy in the tools folder, as the relaunch runs it ------
    _map_drive(fx.prefix, "d", fx.meta)
    copy = f"{tools_win}\\recovery\\scripts\\restore.bat"
    run = _Run(fx, ["wine", "cmd", "/c", copy],
               extra_env={"LCSAS_RELOCATED": "D:\\"})
    try:
        boxes = _drive(fx, run, quit_at=2)
    finally:
        rc = run.finish()
    out = run.text
    _assert_quiet(out)
    assert "PAUSES the restore" not in out, "the copy said the note again"
    assert len(boxes) == 2, out
    assert rc == 75, f"rc={rc}\n{out}"
    assert "The restore stopped before it finished (code 75)." in out, out
    assert f'"{tools_win}\\{LAUNCHER}"' in out, out
    assert "Desktop" in out and "The META disc is not needed" in out, out
    assert "[tier 2]" not in out, out
    got = fx.target / "a0.txt"  # from disc A, before the q
    assert got.is_file() and _sha(got.read_bytes()) == _sha(FILES["a0.txt"]), out

    cfg = _resume_cfg(tools)
    assert cfg["set"] == SET_ID, cfg
    assert cfg["target"] == TARGET_WIN, cfg
    assert cfg["meta"] == "D:\\", cfg
    assert "D" in cfg["letters"].split(), cfg
    for kept in (tools / "resume.cfg", tools / LAUNCHER, fx.desktop / LAUNCHER):
        raw = kept.read_bytes()
        assert PASSWORD.encode() not in raw, f"password in {kept}"
        assert b"\n" not in raw.replace(b"\r\n", b""), f"{kept} is not CRLF"
    assert (fx.desktop / LAUNCHER).read_bytes() == (tools / LAUNCHER).read_bytes()
    assert any((tools / "pack-cache").rglob("*")), "disc A was not cached"
    temp = fx.user_dir / "Temp"
    assert not list(temp.glob("lcsas-pw-*")), "password file left in %TEMP%"

    # --- 3. carry on: the launcher, no META disc -----------------------
    _map_drive(fx.prefix, "d", fx.empty)  # the drive is empty at first
    replay = _replay_launcher(fx, fx.desktop / LAUNCHER)
    run = _Run(fx, ["wine", "cmd", "/c", f"C:\\{replay.name}"])
    try:
        boxes = _drive(fx, run, target_win=None)
    finally:
        rc = run.finish()
    out = run.text
    _assert_quiet(out)
    assert "Carrying on the restore into:" in out, out
    assert "Restore to which folder?" not in out, out
    # The empty drive is searched only because resume.cfg names it.
    assert "drives searched for discs: D:\\" in out, out
    assert 1 <= len(boxes) <= 2, out
    assert "Restore complete" in out, out
    assert rc == 0, f"rc={rc}\n{out}"
    for name, data in FILES.items():
        got = fx.target / name
        assert got.is_file() and _sha(got.read_bytes()) == _sha(data), (
            f"{name} not restored byte-exact\n{out}")
    assert not tools.exists(), f"tools folder left: {_files(tools)}"
    assert not (fx.desktop / LAUNCHER).exists(), "Desktop launcher left"


def test_tools_dir_override_refuses_a_busy_folder_and_reuses_its_copy(
    fx: _Fixture,
) -> None:
    """LCSAS_TOOLS_DIR names the tools folder (C4).  A folder that holds
    other files is refused before anything is copied: the tools folder is
    deleted once a restore has finished.  A second run from the META disc
    uses the copy already there (here: wine's fc calls any two files the
    same, so this checks the "reuse" branch, not the byte compare)."""
    busy = fx.drive_c / "mine"
    busy.mkdir()
    (busy / "notes.txt").write_text("keep me\n")
    res = subprocess.run(
        ["wine", "cmd", "/c", "D:\\restore.bat"],
        env={**fx.env, "LCSAS_TOOLS_DIR": "C:\\mine"}, cwd=str(fx.drive_c),
        input=b"\n\n", capture_output=True, timeout=120,
    )
    out = (res.stdout + res.stderr).decode("utf-8", "replace")
    assert res.returncode == 1, f"rc={res.returncode}\n{out}"
    assert "a folder that already holds other files" in out, out
    assert _files(busy) == ["notes.txt"], _files(busy)
    assert (busy / "notes.txt").read_text() == "keep me\n"

    tools = fx.drive_c / "lcsas tools"
    tools_win = "C:\\lcsas tools"
    for attempt in ("copy", "reuse"):
        _map_drive(fx.prefix, "d", fx.meta)
        rc, out = _run_from_the_disc(
            fx, {"LCSAS_TOOLS_DIR": tools_win + "\\"})
        _assert_quiet(out)
        if attempt == "copy":
            assert f"copied recovery files to {tools_win}\r\n" in out, out
        else:
            assert f"using the recovery files already copied to {tools_win}\r\n" in out, out
            assert "copying the restore programs" not in out, out
        assert (tools / "recovery" / "bin" / TRIPLE / "lcsas-restore.exe").is_file()
    assert not fx.default_tools.exists(), "the default folder was used"
