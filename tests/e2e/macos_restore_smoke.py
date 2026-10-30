#!/usr/bin/env python3
"""macOS ``restore.sh`` journey harness (#517) -- the macOS twin of windows-e2e.

``.github/workflows/macos-e2e.yml`` builds a fixture on Linux (``fixture``
sub-command), then on hosted ``macos-14`` (arm64) and ``macos-15-intel``
(x86_64) runners drives the REAL ``restore.sh`` off ISOs attached with
``hdiutil attach``, the way a Mac heir with discs in a USB drive would:

``happy``           every ISO attached, ``LCSAS_PASSWORD`` set, stdin closed.
                    ``LCSAS_NO_RELOCATE`` is deliberately NOT set: the meta
                    ISO is a read-only mount, so this proves the single-drive
                    "copy yourself to RAM and re-exec" path as well as tier 1.
                    The restored tree is compared byte-exact with the manifest.
``wrong-password``  exit 77, no tier-2 fall-through, no restored files.
``swap``            only the meta ISO and ONE data ISO attached.  restore.sh
                    runs under a pty (see below); each time tier 1 prints its
                    "insert the right disc" prompt the harness reads the
                    volume label the prompt names, attaches that ISO, and
                    answers.  Alternate swaps answer with ENTER and with
                    nothing at all (the #455 auto-mount rescan), so both
                    documented ways of continuing are exercised.
``detach-all``      belt-and-braces cleanup for an ``if: always()`` step.
``fixture``         Linux only: build the multi-disc fixture (rustic + xorriso).

Why a pty and not a FIFO for the swap case: tier 1 only prompts when
``isatty(0)`` -- restore.sh does not pass ``--interactive on`` -- so a piped
stdin makes a missing pack fail fast with "pack not found" and no prompt.  A
heir at Terminal.app always has a tty; the harness has to provide one.

Stdlib only, Python >= 3.9 syntax at runtime.  ``--attach extract`` replaces
``hdiutil`` with a xorriso extraction into a throw-away directory so the
non-macOS parts can be dry-run on Linux without mounting anything.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

META_LABEL = "LCSAS_META"
TENANT = "win"  # build_windows_fixture's tenant / repo name
MARKER_NAME = ".lcsas-restore-marker"

# Fixture sizing.  TEST_TINY discs are 2 MiB and the burn pipeline reserves
# 700 kB of each for the holographic metadata, so a disc takes ~1.4 MB of
# packs; rustic is configured for ~256 KiB data packs.  The first-fit-decreasing
# packer fills discs to that budget, which leaves the fullest disc only tens
# of kB under the cap whatever the file size (56 x 70 KiB and 60 x 68 KiB
# overflow it and abort the build; 12 x 64 KiB, windows-e2e's fixture, fits
# one disc).  52 x 72 KiB (3.7 MiB) is the measured sweet spot: 4 data discs,
# the fullest 127 kB under the cap.  Pack sizes of incompressible data do not
# depend on the random bytes, so the layout is the same on every run; the
# retry loop below is for the day a metadata change moves it.
FIXTURE_FILE_COUNT = 52
FIXTURE_FILE_BYTES = 72 * 1024
MIN_DATA_ISOS = 3
FIXTURE_ATTEMPTS = 4

# Strings printed by restore.sh / lcsas-restore that the assertions pin.
RELOCATED_MARK = "[lcsas-restore] copied recovery binaries to"
PROMPT_MARK = "is required for the next file"
PROMPT_END_RE = re.compile(r"\+-{20,}\+\s*\n> ")
LIVES_ON_RE = re.compile(
    r"It is on any disc labelled:(?P<body>.*?)Currently searching:", re.S
)
# Both catalog-failure wordings: "could not be queried" (schema skew) and,
# since #543, "could not be read".
CATALOG_UNQUERYABLE_MARK = "catalog could not be"
STILL_MISSING_MARK = "(still not found"
AUTO_DETECT_MARK = "new disc detected -- no ENTER needed"
PACK_PREFIX_RE = re.compile(r"Pack ([0-9a-f]{16})\.\.\.")
CONTINUING_RE = re.compile(r"found [0-9a-f]{16}\.\.\.; continuing\.")
WRONG_PW_MARK = "could not decrypt any key file"


class CheckError(Exception):
    """An assertion about restore.sh's behaviour did not hold."""


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Fixture (Linux)
# ---------------------------------------------------------------------------


def pack_map(isos: list[Path]) -> dict[str, list[str]]:
    """Which pack files (data/<xx>/<64 hex>) each data ISO carries."""
    out: dict[str, list[str]] = {}
    for iso in isos:
        proc = subprocess.run(
            ["xorriso", "-indev", str(iso), "-find", "/data", "-type", "f"],
            capture_output=True, text=True, check=True,
        )
        packs = [
            shlex.split(line)[0].rsplit("/", 1)[-1]
            for line in proc.stdout.splitlines()
            if line.startswith("'/data/")
        ]
        out[iso.stem] = sorted(packs)
    return out


def cmd_fixture(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fixture_lib  # type: ignore[import-not-found]

    work = Path(args.workdir).resolve()
    upload = Path(args.upload).resolve()
    last_err = "no attempt ran"
    for attempt in range(1, FIXTURE_ATTEMPTS + 1):
        # Pack boundaries vary run to run, so a build can land a disc a few
        # kB over the TEST_TINY cap (the orchestrator raises) or come out with
        # fewer discs than the swap case needs.  Retry from a clean slate.
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        try:
            fx = fixture_lib.build_windows_fixture(
                work, file_count=FIXTURE_FILE_COUNT, file_bytes=FIXTURE_FILE_BYTES
            )
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            log(f"fixture attempt {attempt}/{FIXTURE_ATTEMPTS} failed: {last_err}")
            continue
        if len(fx.data_isos) < MIN_DATA_ISOS:
            last_err = f"only {len(fx.data_isos)} data ISO(s); need >= {MIN_DATA_ISOS}"
            log(f"fixture attempt {attempt}/{FIXTURE_ATTEMPTS}: {last_err}")
            continue
        if upload.exists():
            shutil.rmtree(upload)
        upload.mkdir(parents=True)
        for iso in [fx.meta_iso, *fx.data_isos]:
            shutil.copy2(iso, upload / iso.name)
        shutil.copy2(fx.manifest_path, upload / "manifest.json")
        shutil.copy2(fx.password_file, upload / "repo.pw")
        (upload / "packmap.json").write_text(
            json.dumps(pack_map(fx.data_isos), indent=2, sort_keys=True)
        )
        log(f"fixture ok on attempt {attempt}: {len(fx.data_isos)} data ISOs")
        for iso in sorted(upload.glob("*.iso")):
            log(f"  {iso.name}  {iso.stat().st_size} bytes")
        return
    raise SystemExit(f"fixture build failed after {FIXTURE_ATTEMPTS} attempts: {last_err}")


# ---------------------------------------------------------------------------
# Disc attach / detach
# ---------------------------------------------------------------------------


class Fixture:
    def __init__(self, directory: str) -> None:
        self.dir = Path(directory).resolve()
        isos = sorted(self.dir.glob("*.iso"))
        self.meta = next((p for p in isos if p.stem == META_LABEL), None)
        self.data = [p for p in isos if p.stem != META_LABEL]
        if self.meta is None:
            raise SystemExit(f"no {META_LABEL}.iso in {self.dir}")
        if len(self.data) < MIN_DATA_ISOS:
            raise SystemExit(
                f"{self.dir}: {len(self.data)} data ISO(s), need >= {MIN_DATA_ISOS} "
                "so the swap prompt is exercised"
            )
        self.manifest: dict[str, str] = json.loads(
            (self.dir / "manifest.json").read_text()
        )
        self.password = (self.dir / "repo.pw").read_text().strip()
        # {volume label: [pack ids on that disc]}, written by `fixture`.  Lets the
        # swap case tell which disc a prompt's pack lives on even when the
        # catalog on the attached disc predates the disc that holds it.
        packmap = self.dir / "packmap.json"
        self.packmap: dict[str, list[str]] = (
            json.loads(packmap.read_text()) if packmap.exists() else {}
        )

    @property
    def all(self) -> list[Path]:
        assert self.meta is not None
        return [self.meta, *self.data]


class Mounts:
    """Attach ISOs at ``<root>/<volume label>`` and detach them again."""

    def __init__(self, root: Path, mode: str) -> None:
        self.root = root
        self.mode = mode
        self.sudo = False  # set once hdiutil needed it to attach
        self.attached: dict[str, Path] = {}

    def point(self, iso: Path) -> Path:
        return self.root / iso.stem

    def attach(self, iso: Path) -> Path:
        mp = self.point(iso)
        if self.mode == "extract":
            self._extract(iso, mp)
        else:
            self._hdiutil_attach(iso, mp)
        if not mp.is_dir():
            raise CheckError(f"attached {iso.name} but {mp} is not a directory")
        self.attached[iso.stem] = mp
        log(f"attached {iso.name} -> {mp}")
        return mp

    def _hdiutil_attach(self, iso: Path, mp: Path) -> None:
        # The mount point is created first (as the runner user, then via sudo if
        # /Volumes will not let us) and hdiutil is told to use it.  Letting
        # hdiutil pick a name would not do: a Joliet volume id is 16 characters,
        # so LCSAS_TEST_TINY_2026_000N would all become "LCSAS_TEST_TINY_".
        errors = []
        for use_sudo in (False, True):
            pre = ["sudo", "-n"] if use_sudo else []
            subprocess.run(pre + ["mkdir", "-p", str(mp)], capture_output=True)
            cmd = pre + [
                "hdiutil", "attach", "-readonly", "-nobrowse",
                "-mountpoint", str(mp), str(iso),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and os.path.ismount(mp):
                if use_sudo:
                    log("NOTE: hdiutil attach needed sudo")
                self.sudo = use_sudo
                return
            errors.append(
                f"$ {' '.join(cmd)}\n  rc={proc.returncode}\n  {proc.stdout}{proc.stderr}"
            )
        raise CheckError("hdiutil attach failed:\n" + "\n".join(errors))

    def _extract(self, iso: Path, mp: Path) -> None:
        if mp.exists():
            _rmtree_force(mp)
        mp.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", "/", str(mp)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Read-only, like an optical mount: restore.sh's relocation test is
        # `[ ! -w "$SCRIPT_DIR" ]`.
        for dirpath, dirnames, filenames in os.walk(mp):
            for name in dirnames + filenames:
                p = Path(dirpath) / name
                if not p.is_symlink():
                    p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        mp.chmod(mp.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    def detach(self, label: str) -> None:
        mp = self.root / label
        if self.mode == "extract":
            if mp.exists():
                _rmtree_force(mp)
        else:
            pre = ["sudo", "-n"] if self.sudo else []
            if os.path.ismount(mp):
                proc = subprocess.run(
                    pre + ["hdiutil", "detach", str(mp)], capture_output=True, text=True
                )
                if proc.returncode != 0:
                    log(f"hdiutil detach {mp} failed ({proc.stderr.strip()}); retrying -force")
                    subprocess.run(pre + ["hdiutil", "detach", "-force", str(mp)], check=False)
            if mp.is_dir() and not os.path.ismount(mp):
                # The empty directory attach() created; rmdir leaves a full one be.
                subprocess.run(pre + ["rmdir", str(mp)], capture_output=True)
        self.attached.pop(label, None)

    def detach_all(self, labels: list[str]) -> None:
        for label in labels:
            with contextlib.suppress(Exception):
                self.detach(label)


def _rmtree_force(path: Path) -> None:
    for dirpath, dirnames, _ in os.walk(path):
        os.chmod(dirpath, 0o755)
        for d in dirnames:
            with contextlib.suppress(OSError):
                os.chmod(Path(dirpath) / d, 0o755)
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Running restore.sh
# ---------------------------------------------------------------------------


def restore_env(
    mount_root: Path, password: str, repo: str | None = None
) -> dict[str, str]:
    """The environment an heir has, plus the password.  Everything LCSAS_* from
    the CI environment is dropped, in particular LCSAS_NO_RELOCATE.  ``repo``
    sets LCSAS_REPO (see the repo-picker note on ``case_happy``)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LCSAS_")}
    env["LCSAS_PASSWORD"] = password
    if repo:
        env["LCSAS_REPO"] = repo
    if mount_root != Path("/Volumes"):
        env["LCSAS_MOUNT_DIRS"] = str(mount_root)  # dry-run on Linux only
    return env


def expected_triple() -> str:
    sysname, machine = platform.system(), platform.machine()
    if sysname == "Darwin":
        return "aarch64-apple-darwin" if machine in ("arm64", "aarch64") else "x86_64-apple-darwin"
    return {
        "x86_64": "x86_64-unknown-linux-musl",
        "aarch64": "aarch64-unknown-linux-musl",
    }.get(machine, "x86_64-unknown-linux-musl")


def restore_cmd(meta_mp: Path, target: Path) -> list[str]:
    # The heir's invocation: `sh /Volumes/LCSAS_META/restore.sh ...`.
    return ["sh", str(meta_mp / "restore.sh"), "--target", str(target)]


def run_noninteractive(
    cmd: list[str], env: dict[str, str], log_path: Path, timeout: int
) -> tuple[int, str]:
    with open(log_path, "wb") as out:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            env=env, start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise CheckError(f"restore.sh still running after {timeout}s; killed") from None
    return rc, log_path.read_text(errors="replace")


def dump(title: str, text: str) -> None:
    print(f"::group::{title}", flush=True)
    print(text, flush=True)
    print("::endgroup::", flush=True)


def tree_root(target: Path) -> Path:
    # rustic restores the original source path under the target; the leaf
    # directory is the one holding file_000.bin.
    hits = sorted(target.rglob("file_000.bin"))
    if not hits:
        raise CheckError(f"restored tree has no file_000.bin under {target}")
    return hits[0].parent


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compare_tree(root: Path, manifest: dict[str, str]) -> list[str]:
    """Problems between the restored tree and the manifest (empty = identical)."""
    problems = []
    seen = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            seen.add(p.relative_to(root).as_posix())
    for rel, want in sorted(manifest.items()):
        f = root / rel
        if not f.is_file():
            problems.append(f"MISSING: {rel}")
        elif sha256(f) != want:
            problems.append(f"HASH MISMATCH: {rel}")
    for rel in sorted(seen - set(manifest)):
        problems.append(f"EXTRA: {rel}")
    return problems


def verify_restored(fx: Fixture, target: Path) -> None:
    root = tree_root(target)
    log(f"restored tree root: {root}")
    problems = compare_tree(root, fx.manifest)
    if problems:
        raise CheckError("restored tree does not match manifest:\n  " + "\n  ".join(problems))
    log(f"restored tree is byte-identical to the manifest ({len(fx.manifest)} files)")


def check_tier1_ran(output: str) -> None:
    triple = expected_triple()
    if f"[tier 1] using prebuilt lcsas-restore ({triple})" not in output:
        raise CheckError(f"tier 1 ({triple}) did not run; no '[tier 1] using prebuilt' line")
    for bad in ("falling through", "tier 2", "tier 3"):
        if bad in output:
            raise CheckError(f"restore.sh mentions {bad!r}: the cascade did not stay on tier 1")


def check_relocated(output: str) -> None:
    if RELOCATED_MARK not in output:
        raise CheckError(
            "restore.sh did not relocate itself off the read-only meta ISO "
            f"({RELOCATED_MARK!r} missing)"
        )


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def case_happy(
    fx: Fixture, mounts: Mounts, work: Path, timeout: int, repo: str | None
) -> None:
    # Every data disc carries metadata/<tenant>/ (the holographic layout).
    # Until #533 repo discovery de-duplicated candidates by PATH, so with N
    # data discs mounted a single-tenant archive was offered N identical
    # "win" repositories and, with stdin closed, restore.sh exited 1 at the
    # picker.  The workflow runs this case with and without `--repo win`.
    for iso in fx.all:
        mounts.attach(iso)
    target = work / "restored"
    rc, out = run_noninteractive(
        restore_cmd(mounts.point(fx.meta), target),
        restore_env(mounts.root, fx.password, repo), work / "happy.log", timeout,
    )
    dump("restore.sh output (happy path)", out)
    log(f"restore.sh exit code: {rc}")
    if rc != 0:
        raise CheckError(f"restore.sh failed (rc={rc})")
    check_relocated(out)
    check_tier1_ran(out)
    verify_restored(fx, target)


def case_wrong_password(
    fx: Fixture, mounts: Mounts, work: Path, timeout: int, repo: str | None
) -> None:
    for iso in fx.all:
        mounts.attach(iso)
    target = work / "restored-wrong"
    rc, out = run_noninteractive(
        restore_cmd(mounts.point(fx.meta), target),
        restore_env(mounts.root, "definitely-the-wrong-password", repo),
        work / "wrong.log", timeout,
    )
    dump("restore.sh output (wrong password)", out)
    log(f"wrong-password exit code: {rc}")
    # #384: a wrong password is TERMINAL with the distinct status 77, and a
    # later tier must never run (it could leave a partial tree).
    if rc != 77:
        raise CheckError(f"restore.sh must exit 77 on a wrong password; got {rc}")
    if "tier 2" in out:
        raise CheckError("restore.sh fell through to tier 2 on a wrong password")
    if WRONG_PW_MARK not in out:
        raise CheckError(f"wrong-password message {WRONG_PW_MARK!r} missing from output")
    # restore.sh drops .lcsas-restore-marker before any tier runs; that is
    # script state, everything else would be a partial tree.
    left = [
        p for p in target.rglob("*")
        if p.is_file() and p.name != MARKER_NAME
    ] if target.exists() else []
    if left:
        raise CheckError("wrong-password run left files behind: " + ", ".join(map(str, left)))
    log("wrong-password case failed cleanly (rc=77, terminal, no partial tree)")


class PtyRun:
    """A command on a pty, with everything it prints kept in ``self.text``."""

    def __init__(self, cmd: list[str], env: dict[str, str], log_path: Path) -> None:
        import pty

        self.log_file = open(log_path, "wb")  # noqa: SIM115 (closed in kill())
        self.text = ""
        self.rc: int | None = None
        pid, fd = pty.fork()
        if pid == 0:  # child: the pty slave is stdin/stdout/stderr
            try:
                os.execvpe(cmd[0], cmd, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self._eof = False
        self.last_data = time.monotonic()

    def pump(self, timeout: float) -> None:
        """Read what is available (up to ``timeout`` s) and note a child exit."""
        if not self._eof:
            ready, _, _ = select.select([self.fd], [], [], timeout)
            if ready:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:  # EIO: the slave side closed (Linux)
                    data = b""
                if data:
                    self.last_data = time.monotonic()
                    self.log_file.write(data)
                    self.log_file.flush()
                    self.text += data.decode("utf-8", "replace").replace("\r\n", "\n")
                else:
                    self._eof = True
        else:
            time.sleep(min(timeout, 0.2))
        if self.rc is None:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.rc = os.waitstatus_to_exitcode(status)

    @property
    def finished(self) -> bool:
        # The child has exited and its output has drained.  Darwin's pty master
        # does not always report EOF once the slave closes, so also accept "exited
        # and silent for a few seconds".
        if self.rc is None:
            return False
        return self._eof or time.monotonic() - self.last_data > 3.0

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def kill(self) -> None:
        if self.rc is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                _, status = os.waitpid(self.pid, 0)
                self.rc = os.waitstatus_to_exitcode(status)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self.log_file.close()


def at_prompt(text: str, since: int) -> bool:
    """True when tier 1 is sitting at a disc-swap prompt that it printed at or
    after offset ``since`` (a whole new box, or the one-line "still not found"
    re-prompt, either way ending in the bare ``> `` it waits behind)."""
    if not text.endswith("> "):
        return False
    return text.rfind(PROMPT_MARK) >= since or text.rfind(STILL_MISSING_MARK) >= since


def last_box(text: str) -> str:
    """The most recent full prompt box ("Pack ... is required ..." to its ``+---+``)."""
    mark = text.rfind(PROMPT_MARK)
    start = text.rfind("| Pack ", 0, mark + 1)  # the line that holds the pack id
    box = text[start if start >= 0 else mark:]
    m = PROMPT_END_RE.search(box)
    return box[: m.end()] if m else box


def labels_named(box: str, known: list[str]) -> list[str]:
    """Volume labels the box says the pack lives on (from the catalog hint)."""
    m = LIVES_ON_RE.search(box)
    section = m.group("body") if m else ""
    return [label for label in known if label in section]


def check_prompt_text(box: str) -> None:
    for want in (
        PROMPT_MARK,
        "Currently searching:",
        "Insert the right disc and press ENTER to retry.",
        "Type 'q' then ENTER to abort.",
        "Single-drive restore",  # restore.sh passed --meta-disc
    ):
        if want not in box:
            raise CheckError(f"swap prompt lacks {want!r}:\n{box}")


def right_label(
    box: str, named: list[str], free: list[str], packmap: dict[str, list[str]]
) -> str | None:
    """The still-unattached disc that carries the pack the prompt asks for: the
    one the catalog hint names, else the one the fixture's pack map says."""
    for label in named:
        if label in free:
            return label
    m = PACK_PREFIX_RE.search(box)
    if m:
        for label in free:
            if any(pack.startswith(m.group(1)) for pack in packmap.get(label, [])):
                return label
    return None


def case_swap(
    fx: Fixture, mounts: Mounts, work: Path, timeout: int, repo: str | None
) -> None:
    """Meta ISO + first data ISO attached; tier 1 asks for the rest, one by one.

    The harness plays the heir at a single-drive Mac.  Swap 1 deliberately
    inserts a WRONG disc and answers ENTER (tier 1 must say "still not found"
    and re-prompt); swap 2 inserts the right disc and answers ENTER (the
    documented path: it must resume); every later swap inserts the right disc
    and types nothing, so only tier 1's own 2 s rescan (#455) can notice it.
    "Right" comes from the prompt's catalog hint when it has one, else from the
    fixture's pack map -- the catalog on disc 1 predates the later discs, so the
    first prompt names no volume.
    """
    data_labels = [p.stem for p in fx.data]
    by_label = {p.stem: p for p in fx.data}
    mounts.attach(fx.meta)
    mounts.attach(fx.data[0])

    target = work / "restored-swap"
    run = PtyRun(
        restore_cmd(mounts.point(fx.meta), target),
        restore_env(mounts.root, fx.password, repo), work / "swap.log",
    )
    deadline = time.monotonic() + timeout
    swaps = 0
    answered = 0  # text offset: prompts before it have been dealt with
    try:
        while True:
            while not (run.finished or at_prompt(run.text, answered)):
                if time.monotonic() > deadline:
                    raise CheckError("timed out waiting for a disc-swap prompt or for exit")
                run.pump(0.5)
            if run.finished:
                break
            box = last_box(run.text)
            if swaps == 0:
                check_prompt_text(box)
                if CATALOG_UNQUERYABLE_MARK in box:
                    # #534: the on-disc catalog.db was a WAL-mode database, which
                    # SQLite cannot open on a read-only volume, so the first prompt
                    # said "likely written by a newer LCSAS" instead of naming a
                    # disc.  The burn now writes it with a rollback journal (#542),
                    # and tier 1 reads a WAL one as immutable (#543).
                    raise CheckError(
                        "tier 1 could not query the on-disc catalog.db in place "
                        f"(read-only volume); the first prompt says:\n{box}"
                    )
            named = labels_named(box, data_labels)
            free = [label for label in data_labels if label not in mounts.attached]
            if not free:
                raise CheckError(
                    f"tier 1 still prompts (names {named or 'no disc'}) with every "
                    f"data disc attached: {sorted(mounts.attached)}"
                )
            right = right_label(box, named, free, fx.packmap)
            swaps += 1
            if right is None:  # no hint and no map: try the discs in order
                label, wrong = free[0], False
            elif swaps == 1 and len(free) > 1:
                label, wrong = next(x for x in free if x != right), True
            else:
                label, wrong = right, False
            enter = swaps <= 2
            log(
                f"--- swap {swaps}: pack lives on {right or 'an unknown disc'}; attaching "
                f"{label}{' (deliberately wrong)' if wrong else ''}, "
                + ("answering ENTER" if enter else "answering nothing (auto-rescan)")
            )
            answered = len(run.text)
            mounts.attach(by_label[label])
            if enter:
                run.send(b"\n")
            # Tier 1 must react without any further input: resume, or say the
            # pack is still missing.  No reaction means it did not see the disc.
            while True:
                new = run.text[answered:]
                if CONTINUING_RE.search(new) or STILL_MISSING_MARK in new or run.finished:
                    break
                if time.monotonic() > deadline:
                    raise CheckError(f"tier 1 did not react to {label} being attached")
                run.pump(0.5)
            new = run.text[answered:]
            if wrong and STILL_MISSING_MARK not in new:
                raise CheckError(f"wrong disc {label} did not earn '(still not found'")
            if right == label and not CONTINUING_RE.search(new) and not run.finished:
                raise CheckError(f"right disc {label} attached but tier 1 did not resume")
            if right == label and not enter and AUTO_DETECT_MARK not in new:
                raise CheckError(
                    f"{label} was attached with no ENTER but tier 1 never reported "
                    f"{AUTO_DETECT_MARK!r}"
                )
        while run.rc is None and time.monotonic() < deadline:
            run.pump(0.5)
        rc = run.rc
    except CheckError:
        dump("restore.sh output (swap case)", run.text)
        raise
    finally:
        run.kill()

    dump("restore.sh output (swap case)", run.text)
    log(f"restore.sh exit code: {rc}; swaps: {swaps}")
    if rc != 0:
        raise CheckError(f"restore.sh failed (rc={rc})")
    if swaps != len(fx.data) - 1:
        raise CheckError(
            f"expected {len(fx.data) - 1} swaps (one per extra data disc); saw {swaps}"
        )
    check_relocated(run.text)
    check_tier1_ran(run.text)
    verify_restored(fx, target)


CASES = {
    "happy": case_happy,
    "wrong-password": case_wrong_password,
    "swap": case_swap,
}


def cmd_case(args: argparse.Namespace) -> None:
    fx = Fixture(args.fixture)
    root = Path(args.mount_root)
    mounts = Mounts(root, args.attach)
    work = Path(args.work or tempfile.mkdtemp(prefix="lcsas-macos-e2e."))
    work.mkdir(parents=True, exist_ok=True)
    labels = [p.stem for p in fx.all]
    try:
        CASES[args.cmd](fx, mounts, work, args.timeout, args.repo)
    except CheckError as exc:
        print(f"::error::{args.cmd}: {exc}", flush=True)
        raise SystemExit(1) from None
    finally:
        mounts.detach_all(labels)
    log(f"PASS: {args.cmd}")


def cmd_detach_all(args: argparse.Namespace) -> None:
    fx = Fixture(args.fixture)
    Mounts(Path(args.mount_root), args.attach).detach_all([p.stem for p in fx.all])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fixture", help="build the multi-disc fixture (Linux)")
    p.add_argument("workdir")
    p.add_argument("upload")
    p.set_defaults(func=cmd_fixture)

    for name in (*CASES, "detach-all"):
        p = sub.add_parser(name)
        p.add_argument("fixture", help="directory with the fixture ISOs + manifest.json + repo.pw")
        p.add_argument("--mount-root", default="/Volumes")
        p.add_argument("--attach", choices=("hdiutil", "extract"), default="hdiutil")
        if name != "detach-all":
            p.add_argument("--work", help="scratch directory (default: a fresh temp dir)")
            p.add_argument("--timeout", type=int, default=300, help="seconds for restore.sh")
            p.add_argument(
                "--repo", help=f"set LCSAS_REPO (this fixture's tenant is {TENANT!r})"
            )
            p.set_defaults(func=cmd_case)
        else:
            p.set_defaults(func=cmd_detach_all)

    args = ap.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
