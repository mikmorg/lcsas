"""Meta disc builder — assembles the tree for a self-contained meta disc.

A meta disc contains everything needed to restore data from LCSAS
archive discs, minus only the password:

* Portable copies of ``rustic``, ``xorriso``, and ``python3``
  with all required shared libraries.
* The full LCSAS source code.
* The one restore driver, ``restore.sh`` (``recovery/scripts/restore.sh``
  from the source tree), at the disc root.
* A short ``README_RESTORE.md`` that points at the on-disc manuals
  (``recovery/docs/RECOVER.txt``, ``RECOVER_WINDOWS.txt``).
* Project documentation (``docs/``).
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # sqlite3 is imported lazily where it is actually used
    import sqlite3
    from collections.abc import Callable, Collection

    from lcsas.staging.repo_marker import RepoMarker

from lcsas.config.settings import LCSASConfig
from lcsas.meta.bundler import ToolBundler
from lcsas.meta.required_contents import (
    APPROVED_TARGETS,
    required_meta_paths,
)

_logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────


class MetaBuildError(Exception):
    """A meta disc could not be built completely / safely."""


_REQUIRED_TOOLS = ("rustic", "xorriso")
_OPTIONAL_TOOLS = ("dvdisaster",)

# Directories / files to copy from the LCSAS source tree.
_SOURCE_ITEMS = ("src",)
_DOC_ITEMS = ("docs", "README.md", "pyproject.toml")

# FMT-02: the pinned dvdisaster RS03 source tarball is bundled on every
# meta disc so a future engineer can re-implement RS03 repair from the
# exact source docs/DVDISASTER_RS03_FORMAT.md transcribes.  The filename
# is the single source of truth in recovery/UPSTREAM.sha256
# (category ``dvdisaster/src/``); we read it from there rather than
# hard-coding the version twice.
_DVDISASTER_SOURCE_SUBDIR = "tools/src"


def pinned_dvdisaster_source_name(recovery_dir: Path) -> str | None:
    """Return the dvdisaster source tarball filename pinned in UPSTREAM.sha256.

    Reads ``recovery/UPSTREAM.sha256`` and returns the basename of the
    single ``dvdisaster/src/<filename>`` entry, or ``None`` if the
    manifest has no dvdisaster source pin.
    """
    manifest = recovery_dir / "UPSTREAM.sha256"
    if not manifest.is_file():
        return None
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        relpath = parts[-1]
        if relpath.startswith("dvdisaster/src/"):
            return relpath.rsplit("/", 1)[-1]
    return None


def _write_and_sync(path: Path, content: str) -> None:
    """Write *content* to *path* and fsync to disk."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


def _strip_markdown(text: str) -> str:
    """Best-effort conversion of Markdown to plain text.

    Strips ``#`` headings, ``**bold**``, ``*italic*``, ```code fences```,
    ``| table |`` pipes, and ``> blockquotes`` while preserving structure.
    """
    lines: list[str] = []
    in_code_block = False
    for line in text.splitlines():
        stripped = line.strip()
        # Toggle code fences
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                lines.append("")  # blank line before code
            else:
                lines.append("")  # blank line after code
            continue
        if in_code_block:
            lines.append(line)
            continue
        # Headings → plain uppercase text
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            heading = re.sub(r"\*\*(.+?)\*\*", r"\1", heading)
            heading = re.sub(r"\*(.+?)\*", r"\1", heading)
            heading = re.sub(r"`([^`]+)`", r"\1", heading)
            lines.append("")
            lines.append(heading.upper())
            lines.append("-" * len(heading))
            continue
        # Blockquotes
        if stripped.startswith(">"):
            bq = stripped.lstrip("> ").strip()
            bq = re.sub(r"\*\*(.+?)\*\*", r"\1", bq)
            bq = re.sub(r"\*(.+?)\*", r"\1", bq)
            bq = re.sub(r"`([^`]+)`", r"\1", bq)
            lines.append("  " + bq)
            continue
        # Table rows — keep but remove leading/trailing pipes
        if stripped.startswith("|") and stripped.endswith("|"):
            # Skip separator rows like |---|---|
            if re.match(r"^\|[\s\-:|]+\|$", stripped):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Strip inline formatting from each cell
            clean_cells = []
            for cell in cells:
                cell = re.sub(r"\*\*(.+?)\*\*", r"\1", cell)
                cell = re.sub(r"\*(.+?)\*", r"\1", cell)
                cell = re.sub(r"`([^`]+)`", r"\1", cell)
                clean_cells.append(cell)
            lines.append("  " + "  |  ".join(clean_cells))
            continue
        # Inline formatting
        cleaned = line
        cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)  # **bold**
        cleaned = re.sub(r"\*(.+?)\*", r"\1", cleaned)       # *italic*
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)       # `code`
        lines.append(cleaned)
    return "\n".join(lines) + "\n"


def _get_tool_version(tool_path: Path) -> str:
    """Run *tool_path* with common version flags and return the version string.

    Tries ``--version``, then ``version`` (rustic uses bare ``version``).
    Returns ``"unknown"`` if all attempts fail.
    """
    import subprocess

    for args in ([str(tool_path), "--version"], [str(tool_path), "version"]):
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    **os.environ,
                    "LD_LIBRARY_PATH": str(tool_path.parent.parent / "lib")
                    + ":" + os.environ.get("LD_LIBRARY_PATH", ""),
                },
            )
            if result.returncode == 0 and result.stdout.strip():
                # Return first non-empty line
                for line in result.stdout.strip().splitlines():
                    if line.strip():
                        return line.strip()
        except (subprocess.TimeoutExpired, OSError):
            continue
    return "unknown"


# ── On-disc README (src/lcsas/meta/assets/) ──────────────────────────
#
# README_RESTORE.md lives as a plain file under ``assets/`` next to this
# module (#689), read here once at import and written to the meta disc
# byte for byte (``tests/unit/test_meta_assets_golden.py`` pins its
# SHA-256).  It is a short pointer: the restore steps live in one place,
# recovery/docs/RECOVER.txt (and RECOVER_WINDOWS.txt).
#
# The disc has ONE restore driver, recovery/scripts/restore.sh, copied
# to the disc root as restore.sh (#689 retired the legacy bash driver,
# its Python helper and the bash restore-auto.sh).  The non-interactive
# wrapper is recovery/scripts/restore_auto.sh, which runs restore.sh.

_ASSETS_DIR = Path(__file__).resolve().with_name("assets")


def _read_asset(name: str) -> str:
    """The text of ``assets/<name>``, exactly as stored (no newline
    translation), so the file on the disc matches the file in git."""
    return (_ASSETS_DIR / name).read_bytes().decode("utf-8")


README_RESTORE = _read_asset("README_RESTORE.md")


def render_meta_volume_info_txt(built_at: datetime) -> str:
    """The text of the meta disc's VOLUME_INFO.txt (#491, UX 010).

    The plain-text twin of the meta disc's ``volume_info.json``, for an
    heir who cannot read JSON: what this disc is, when it was built, the
    LCSAS version, and that it holds the tools but no catalog.
    """
    from lcsas import __version__

    built = built_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        "ABOUT THIS DISC\n"
        "===============\n"
        "\n"
        "This is the LCSAS META disc.  It holds the recovery tools, the\n"
        "instructions and the source code needed to restore the files on\n"
        "the archive's data discs.  It holds no backup data and no\n"
        "catalog: the catalog is on every data disc.\n"
        "\n"
        f"  Built on:      {built}\n"
        f"  Made with:     LCSAS version {__version__}\n"
        "\n"
        "If you have more than one META disc, use the newest.\n"
        "\n"
        "To get your files back, read START_HERE.txt on this disc first.\n"
        "volume_info.json describes this disc for computer programs.\n"
    )


# The non-interactive wrapper on the meta disc, relative to its root: the
# source tree's recovery/scripts/restore_auto.sh, copied with the rest of
# recovery/.  It runs restore.sh once per backup set.
RESTORE_AUTO_SCRIPT_PATH = "recovery/scripts/restore_auto.sh"

# Files older builds wrote that #689 retired: a rebuild into an old
# output folder removes them, so the disc holds one restore driver.
_RETIRED_META_FILES = (
    "restore_legacy.sh",
    "restore-auto.sh",
    "restore_c89.sh",
    "recovery/scripts/restore_legacy.sh",
    "recovery/scripts/restore-auto.sh",
    "tools/restore_single_drive.py",
)


# ── recovery/ bundling: which files go on the disc (#562) ────────────

# The two manifests under recovery/ that MANIFEST.sha256 never lists: the
# curation rule in recovery/Makefile excludes both (a manifest cannot list
# itself), so neither counts as a file the manifest is missing.
UNLISTED_MANIFESTS = frozenset({"MANIFEST.sha256", "docs/MANIFEST.sha256"})

# Build debris that never belongs on a meta disc (recovery/.gitignore).
# Applied to bin/, which is copied as it stands, and to the whole tree
# when git cannot say which files are tracked.
_RECOVERY_DEBRIS = (
    "build", "build-*", "dist", "__pycache__", "*.pyc", "*.o", "*.a",
)

# Fuzzer output that recovery/fuzz/.gitignore keeps out of git, by the
# directory it sits in: in fuzz/corpus/<target>/, names that start with
# eight hex digits; in fuzz/crashes/<target>/, names that start with a
# hex digit or with "crash_".  Curated inputs there (seed_*, and a few
# named ones such as tree/real_tree.json) are tracked and do not match.
# Only the fallback copy uses this; in a git checkout git decides.
_FUZZ_OUTPUT_PATTERNS = {
    "corpus": ("[0-9a-f]" * 8 + "*",),
    "crashes": ("[0-9a-f]*", "crash_*"),
}

# Environment overrides that would make git answer for some other git
# repository than the one recovery/ sits in (a git hook, for one, sets
# GIT_DIR or GIT_INDEX_FILE for its children).
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def _is_fuzz_output(rel: PurePosixPath) -> bool:
    """True when *rel* (relative to recovery/) is gitignored fuzzer output."""
    parts = rel.parts
    if len(parts) != 4 or parts[0] != "fuzz":
        return False
    patterns = _FUZZ_OUTPUT_PATTERNS.get(parts[1], ())
    return any(fnmatch.fnmatchcase(parts[3], pat) for pat in patterns)


def _fallback_recovery_ignore(
    src_root: Path,
) -> Callable[[str, list[str]], set[str]]:
    """``copytree`` ignore callable for a recovery/ tree outside git.

    Skips build debris and gitignored fuzzer output.  The fuzzer check
    looks at the parent directory, not just the name: tracked files such
    as ``tests/fixtures/repo/index/<sha256>`` are hex-named too.
    """
    debris = shutil.ignore_patterns(*_RECOVERY_DEBRIS)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = set(debris(directory, names))
        rel_dir = PurePosixPath(Path(directory).relative_to(src_root).as_posix())
        skipped.update(n for n in names if _is_fuzz_output(rel_dir / n))
        return skipped

    return ignore


def _git_tracked_files(directory: Path) -> tuple[list[str] | None, str]:
    """List the git-tracked files under *directory*, relative to it.

    This is the oracle ``make -C recovery manifest`` builds
    MANIFEST.sha256 from (``git ls-files --cached -- .``).  Returns
    ``(paths, "")``, or ``(None, reason)`` when git cannot answer: git is
    not installed, *directory* is not in a git work tree, or git tracks
    nothing under it (a copy that merely sits inside some other git
    repository).
    """
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), "ls-files", "--cached", "-z", "--", "."],
            capture_output=True, timeout=120, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"git could not be run: {exc}"
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, lines[-1] if lines else f"git exited {proc.returncode}"
    paths = [os.fsdecode(p) for p in proc.stdout.split(b"\0") if p]
    if not paths:
        return None, "git tracks no files under it"
    return paths, ""


def manifest_listed_paths(manifest: Path) -> set[str]:
    """The recovery/-relative paths a MANIFEST.sha256 lists, without "./"."""
    listed: set[str] = set()
    for line in manifest.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("  ", 1)
        if len(parts) == 2:
            rel = parts[1]
            listed.add(rel[2:] if rel.startswith("./") else rel)
    return listed


def files_outside_manifest(
    recovery_root: Path, listed: Collection[str]
) -> list[str]:
    """Files under *recovery_root* that *listed* does not name, sorted.

    *listed* holds recovery/-relative paths without "./".  These are the
    files ``lcsas meta verify --strict`` reports as EXTRA; the two
    manifests themselves never count (``UNLISTED_MANIFESTS``).
    """
    listed_set = set(listed)
    extras: list[str] = []
    for path in recovery_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(recovery_root).as_posix()
        if rel in UNLISTED_MANIFESTS or rel in listed_set:
            continue
        extras.append(rel)
    return sorted(extras)


# ── Per-repo metadata bundling (#435) ────────────────────────────────

# Wall-clock bound on the "is this mirror reachable?" probe.  A stat()
# against a stale NFS/CIFS mount does not fail — it blocks for the
# mount's timeout, which is unbounded from our side (#427 closed the
# same hole in load_config).  Five seconds is far longer than any live
# mount needs to answer and far shorter than a stale mount's timeout.
_MIRROR_PROBE_TIMEOUT_S = 5.0


def _is_dir_bounded(
    path: Path, timeout: float = _MIRROR_PROBE_TIMEOUT_S
) -> bool | None:
    """``path.is_dir()`` with a wall-clock bound.

    Returns ``True``/``False`` as ``is_dir()`` would, or ``None`` when
    the probe did not answer within *timeout* — i.e. the path is on a
    hung mount and the answer is unknowable without blocking.

    The probe runs in a forked **child process**, not a thread.  That is
    not a style preference: a blocking ``stat()`` cannot be interrupted,
    so the probe must be *abandonable*, and an abandoned daemon thread
    still stops CPython 3.12 from exiting — it never reaches a GIL
    checkpoint where finalization could discard it, so the build hangs
    at exit instead of in the middle.  Measured against a FUSE mount
    whose ``getattr`` never returns: with a thread the process never
    exits; with a child process it exits immediately.  A child that
    ignores SIGKILL (a hard NFS mount's uninterruptible ``stat()``) is
    simply left behind and reparented to init when we exit — which is
    why the kill below is *not* followed by a blocking wait.

    ``OSError`` is caught in the child: ``pathlib`` swallows
    ENOENT/ENOTDIR/ELOOP but not ESTALE, and "cannot read this mirror"
    is the same decision as ``False``.
    """
    if not hasattr(os, "fork"):  # pragma: no cover — Linux build host
        try:
            return path.is_dir()
        except OSError:
            return False
    try:
        pid = os.fork()
    except OSError:  # pragma: no cover — fork exhaustion
        try:
            return path.is_dir()
        except OSError:
            return False

    if pid == 0:
        # Child: report through the exit status and never run any
        # interpreter cleanup (our parent's buffers are shared).
        code = 1
        try:
            # Detach the inherited stdio first.  An abandoned child
            # holding the write end of our stdout keeps a downstream
            # `lcsas meta build | tee` (or any pipeline, or CI's log
            # capture) waiting for EOF long after we exit — measured:
            # the build itself finished in 5s and the pipeline hung
            # forever.  Nothing here writes, so /dev/null loses nothing.
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            code = 0 if path.is_dir() else 1
        except BaseException:
            code = 1
        finally:
            os._exit(code)

    deadline = time.monotonic() + timeout
    while True:
        try:
            reaped, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            # Someone else reaped it (a host application's SIGCHLD
            # handler) — the answer is gone, so report "unknown" rather
            # than invent one.
            return None
        if reaped == pid:
            return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)

    # Abandon it.  SIGKILL is best-effort — a process wedged in an
    # uninterruptible stat() will not take it — so the reap is
    # non-blocking and the child, if still alive, is left to init.
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        os.waitpid(pid, os.WNOHANG)
    return None


@dataclass(frozen=True)
class RepoMetadataResult:
    """Outcome of bundling one catalogued repository's metadata.

    ``bundled`` False means this meta disc carries no keys for the
    repository — its packs cannot be decrypted from this disc alone.  That is
    a survivable, deliberate state (a deregistered repo, a mirror that
    is offline at build time), but never a silent one: it is warned to
    the log and recorded in ``volume_info.json`` on the disc.

    ``retired`` and ``packs_on_discs`` are the two *facts* the key gate
    (#437) needs to tell a real survivability gap from a stale catalog
    row.  This class only reports them; whether they add up to a failed
    build is decided in ``cmd_meta_build``, mirroring the existing
    ``missing_required_contents()`` / RST-05 split.
    """

    repo_id: str
    mirror_path: str
    bundled: bool
    detail: str = ""
    parts: tuple[str, ...] = ()
    # repositories.status == 'retired' (schema v10).  Always False for a
    # catalog older than v10, which has no such column: those repos
    # pre-date retirement and are therefore active.
    retired: bool = False
    # How many of this repo's packs sit on a volume that still exists.
    # 0 means nothing on any disc needs this repo's keys.
    packs_on_discs: int = 0


class MetaVolumeBuilder:
    """Assembles the tree for a self-contained meta disc.

    Usage::

        builder = MetaVolumeBuilder(Path("/tmp/meta"))
        meta_root = builder.build()
        # meta_root is ready for ISO mastering via xorriso

    The meta disc layout::

        output_dir/
        ├── tools/
        │   ├── bin/          rustic, xorriso, python3
        │   └── lib/          shared libs + python stdlib
        ├── lcsas/
        │   └── src/lcsas/    LCSAS Python package
        ├── docs/             architecture docs
        ├── recovery/         tier-1 binaries, scripts (restore_auto.sh
        │                     for automation), docs (RECOVER.txt)
        ├── restore.sh        the one restore driver
        ├── README_RESTORE.md pointer to recovery/docs/RECOVER.txt
        ├── VOLUME_INFO.txt   plain-text "what this disc is"
        └── volume_info.json  self-describing metadata
    """

    # python-build-standalone publishes Windows CPython only for the
    # *-msvc triple, while rustic/restic ship *-gnu — so UPSTREAM.sha256
    # pins the Windows python under x86_64-pc-windows-msvc (see its
    # header note "PBS don't ship gnu").  The per-target copy loop
    # iterates rust triples keyed to rustic; this override remaps ONLY
    # the python cache lookup so the Windows python is actually found
    # on a freshly-fetched cache (#404).
    PYTHON_TRIPLE_OVERRIDE = {
        "x86_64-pc-windows-gnu": "x86_64-pc-windows-msvc",
    }

    def __init__(
        self,
        output_dir: Path,
        project_root: Path | None = None,
        static_rustic_path: Path | None = None,
        config: LCSASConfig | None = None,
        catalog_db_path: Path | None = None,
        recovery_dir: Path | None = None,
        bundle_recovery_toolchain: bool = True,
        allow_no_zstd: bool = False,
        allow_no_dvdisaster_source: bool = False,
        allow_missing_metadata: bool = False,
    ) -> None:
        """
        Args:
            output_dir: Where to build the meta disc directory tree.
            project_root: Root of the LCSAS project (containing ``src/``).
                If *None*, auto-detected from this module's path.
            static_rustic_path: Optional path to a statically-linked
                (musl) rustic binary.  Bundled as ``tools/bin/rustic-static``
                to provide a glibc-independent fallback.
            config: Optional LCSAS configuration.  When provided,
                START_HERE.txt and PASSWORD_INFO.txt are generated on the
                meta disc using the survivability fields.
        """
        self._output = output_dir
        self._static_rustic_path = static_rustic_path
        self._config = config
        self._catalog_db_path = catalog_db_path
        self._bundle_recovery_toolchain = bundle_recovery_toolchain
        self._allow_no_zstd = allow_no_zstd
        self._allow_no_dvdisaster_source = allow_no_dvdisaster_source
        # #437: the owner accepted a known key gap for this build.  The
        # exit-code policy lives in cmd_meta_build, but the flag has to
        # reach the builder because _write_volume_info runs inside build()
        # — the disc itself must carry the fact that gaps were accepted.
        self._allow_missing_metadata = allow_missing_metadata
        # Whether the native ``zstandard`` package was bundled for this
        # build host's arch/CPython.  Recorded in volume_info.json so
        # ``lcsas meta verify`` can report tier-3 native-zstd coverage.
        # (Tier-3 zstd ALSO works via the bundled pure-Python decoder on
        # every target, so a False here is a speed note, not a gap.)
        self._native_zstd_bundled = False
        # Per-repo outcome of _bundle_metadata (#435).  Empty until it
        # runs; stays empty when there is no catalog to read (such a
        # build bundles no repository metadata by design).
        self._metadata_results: list[RepoMetadataResult] = []

        if project_root is None:
            # meta/ → lcsas/ → src/ → (project root)
            self._project_root = Path(__file__).resolve().parents[3]
        else:
            self._project_root = project_root.resolve()

        if recovery_dir is None:
            self._recovery_dir = self._project_root / "recovery"
        else:
            self._recovery_dir = recovery_dir.resolve()

    @property
    def output_dir(self) -> Path:
        return self._output

    @property
    def project_root(self) -> Path:
        return self._project_root

    def build(self) -> Path:
        """Build the complete meta disc tree.

        Returns:
            Path to the meta disc root directory.
        """
        # The disc's one restore driver comes from the recovery/ tree;
        # check for it before the slow tool bundling (#689).
        self._require_restore_driver(self._recovery_dir / "scripts" / "restore.sh")
        self._output.mkdir(parents=True, exist_ok=True)

        # Mark incomplete until all steps succeed
        incomplete_marker = self._output / ".incomplete"
        incomplete_marker.write_text("Meta disc build in progress\n")

        self._bundle_tools()
        self._bundle_source()
        self._bundle_docs()
        self._bundle_dvdisaster_source()
        self._bundle_standalone_restorer()
        self._bundle_keyshare_combiner()
        self._bundle_metadata()
        if self._bundle_recovery_toolchain:
            self._bundle_recovery_toolchain_artifacts()
        self._write_restore_script()
        self._write_readme()
        self._write_readme_txt()
        self._write_volume_info()
        self._write_start_here()

        # Build complete — remove the marker
        incomplete_marker.unlink(missing_ok=True)

        return self._output

    def missing_required_contents(self) -> list[str]:
        """Return required-contents paths absent from the built output.

        RST-05: walks the 2026-06 required-contents contract
        (``required_meta_paths``) against ``self._output``.  A path that
        names a directory (e.g. ``tools``) is satisfied by the directory
        existing; everything else must be a file.  Empty list ⇒ complete.
        """
        missing: list[str] = []
        for rel in required_meta_paths():
            target = self._output / rel
            if rel == "tools":
                if not target.is_dir():
                    missing.append(rel)
            elif not target.is_file():
                missing.append(rel)
        # Per-repo metadata keys (issue #367): each repository copied under
        # metadata/<repo>/ must carry its keys/ subtree — without a
        # repository's keys its packs cannot be decrypted, so a missing
        # keys/ is restore-blocking, not a hedge.
        metadata_root = self._output / "metadata"
        if metadata_root.is_dir():
            for repo_dir in sorted(metadata_root.iterdir()):
                if repo_dir.is_dir() and not (repo_dir / "keys").is_dir():
                    missing.append(f"metadata/{repo_dir.name}/keys")
        return missing

    def missing_recommended_contents(self) -> list[str]:
        """Recommended-but-not-required paths absent from the built output.

        Issue #367: stock restic (the tier-2b "standard tools" hedge) and
        lcsas-keyshare (the SLIP-0039 combiner).  Absence WARNS but does not
        fail the completeness gate — a disc carrying the tier-1 lcsas-restore
        and the tier-3 CPython path can still restore fully without them.
        """
        from lcsas.meta.required_contents import recommended_meta_paths
        return [
            rel for rel in recommended_meta_paths()
            if not (self._output / rel).is_file()
        ]

    # ── Tool bundling ────────────────────────────────────────────

    def _bundle_tools(self) -> None:
        """Bundle rustic, xorriso, and Python with shared libs.

        Also bundles optional tools (dvdisaster) if available on PATH,
        a statically-linked rustic binary if provided, and the
        ``zstandard`` Python package for zstd-compressed repo support.
        """
        tools_dir = self._output / "tools"
        bundler = ToolBundler(tools_dir)

        for tool in _REQUIRED_TOOLS:
            bundler.bundle_binary(tool)

        for tool in _OPTIONAL_TOOLS:
            import shutil as _shutil
            if _shutil.which(tool):
                bundler.bundle_binary(tool)

        bundler.bundle_python()

        # Bundle the native ``zstandard`` package for fast tier-3 restore
        # of zstd-compressed repos (rustic v2 default).  This copies the
        # BUILD HOST's arch/CPython-specific C extension, so it only helps
        # restore hosts that match.  Cross-target / future-CPython hosts
        # fall back to the bundled pure-Python decoder
        # (``lcsas.restore._zstd_pure``), which is slower but always works.
        #
        # RST-04: if the build host has no ``zstandard`` at all the native
        # copy is silently absent; fail loud unless explicitly allowed, so
        # an owner who WANTED the fast path isn't surprised by a
        # pure-Python-only meta disc.
        self._native_zstd_bundled = (
            bundler.bundle_python_package("zstandard") is not None
        )
        if not self._native_zstd_bundled and not self._allow_no_zstd:
            raise MetaBuildError(
                "zstandard is not installed on the build host — the fast "
                "tier-3 restore path for zstd-compressed (default) repositories "
                "would not be bundled. The pure-Python decoder still works "
                "(slower), but if you want the native fast path, install it "
                "(pip install zstandard). Pass --allow-no-zstd to build "
                "anyway."
            )

        # Bundle the stdlib-only SLIP-0039 keyshare package (+ wordlist.txt)
        # so the standalone keyshare_combine.py pre-step can reconstruct a
        # split repository password.  Lands as top-level ``keyshare`` under the
        # bundled stdlib (importlib resolves it to .../lcsas/keyshare).
        bundler.bundle_python_package("lcsas.keyshare")

        # Bundle static rustic binary (glibc-independent fallback).
        # If an explicit path was provided, use it.  Otherwise,
        # auto-detect: if the bundled rustic is already statically
        # linked, copy it as rustic-static too.
        static_src: Path | None = None
        if self._static_rustic_path is not None:
            static_src = Path(self._static_rustic_path).resolve()
            if not static_src.is_file():
                raise FileNotFoundError(
                    f"Static rustic binary not found: {static_src}"
                )
        elif (bundler.bin_dir / "rustic").is_file():
            # Auto-detect: check if the bundled rustic has no shared deps
            from lcsas.meta.bundler import get_shared_libs

            bundled_rustic = bundler.bin_dir / "rustic"
            if not get_shared_libs(bundled_rustic):
                static_src = bundled_rustic

        if static_src is not None:
            dst = bundler.bin_dir / "rustic-static"
            if not dst.exists():
                shutil.copy2(str(static_src), str(dst))
                os.chmod(str(dst), 0o755)

    # ── Source bundling ──────────────────────────────────────────

    def _bundle_source(self) -> None:
        """Copy the LCSAS source tree into the meta disc tree."""
        lcsas_dir = self._output / "lcsas"

        for item_name in _SOURCE_ITEMS:
            src = self._project_root / item_name
            dst = lcsas_dir / item_name
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(str(dst))
                shutil.copytree(
                    str(src),
                    str(dst),
                    ignore=shutil.ignore_patterns(
                        "__pycache__",
                        "*.pyc",
                        "*.egg-info",
                        ".git",
                    ),
                )
            elif src.is_file():
                lcsas_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))

    def _bundle_docs(self) -> None:
        """Copy documentation into the meta disc tree."""
        for item_name in _DOC_ITEMS:
            src = self._project_root / item_name
            dst = self._output / item_name
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(str(dst))
                shutil.copytree(
                    str(src),
                    str(dst),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))

    def _bundle_dvdisaster_source(self) -> None:
        """Bundle the pinned dvdisaster RS03 source tarball (FMT-02).

        ``docs/DVDISASTER_RS03_FORMAT.md`` is only re-implementable
        against the *exact* source it transcribes; that source must
        therefore travel on the meta disc, not at a dormant GitHub URL.
        The tarball is pinned in ``recovery/UPSTREAM.sha256`` (category
        ``dvdisaster/src/``) and fetched into the recovery cache by
        ``recovery/scripts/fetch_upstream.sh``; here we copy it to
        ``tools/src/`` on the meta disc.

        Fail loud if the tarball is pinned but absent from the cache —
        a meta disc whose RS03 spec points at source that isn't on the
        disc is exactly the broken hedge FMT-02 fixes.  Pass
        ``allow_no_dvdisaster_source`` (or ``--allow-no-dvdisaster-source``)
        to build without it.
        """
        name = pinned_dvdisaster_source_name(self._recovery_dir)
        if name is None:
            if self._allow_no_dvdisaster_source:
                return
            raise MetaBuildError(
                "no dvdisaster source tarball is pinned in "
                f"{self._recovery_dir / 'UPSTREAM.sha256'} (category "
                "dvdisaster/src/). docs/DVDISASTER_RS03_FORMAT.md would "
                "point at source that does not travel on the disc. Pin it "
                "or pass --allow-no-dvdisaster-source."
            )

        cache_root_env = os.environ.get("LCSAS_RECOVERY_CACHE")
        if cache_root_env:
            cache_root = Path(cache_root_env)
        else:
            cache_root = Path.home() / ".cache" / "lcsas" / "recovery-binaries"
        src = cache_root / "dvdisaster" / "src" / name

        if not src.is_file():
            if self._allow_no_dvdisaster_source:
                return
            raise MetaBuildError(
                f"pinned dvdisaster source {name} not found in the recovery "
                f"cache ({src}). Run `sh recovery/scripts/fetch_upstream.sh` "
                "to download it, or pass --allow-no-dvdisaster-source to "
                "build a meta disc whose RS03 spec lacks its reference "
                "source."
            )

        dst_dir = self._output / _DVDISASTER_SOURCE_SUBDIR
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst_dir / name))

    # ── Script / doc generation ──────────────────────────────────

    def _bundle_standalone_restorer(self) -> None:
        """Place standalone_restorer.py at the meta disc root.

        This provides the tier-3 Python restore path when no rustic/restic
        binary is available.  The script is auto-generated from the
        LCSAS source modules and has zero external dependencies
        (except optional ``zstandard`` for zstd-compressed repos).
        """
        from lcsas.restore.standalone_builder import build_standalone

        restorer_path = self._output / "standalone_restorer.py"
        _write_and_sync(restorer_path, build_standalone())
        os.chmod(str(restorer_path), 0o755)

    def _bundle_keyshare_combiner(self) -> None:
        """Place keyshare_combine.py at the meta disc root.

        This standalone, stdlib-only pre-step reconstructs a SLIP-0039
        split repository password (any K of N shares) and prints it so an heir
        can feed it to the normal restore flow.  It imports ONLY the
        bundled ``lcsas.keyshare`` package, so reconstruction survives
        even if the rest of LCSAS is broken.

        Manifest pinning: ``keyshare_combine.py`` and the bundled
        ``keyshare/wordlist.txt`` are NOT rows in
        ``recovery/MANIFEST.sha256``.  That "files-we-author" manifest
        is rooted at, and ``find``-verified against, the ``recovery/``
        tree (the tier-1 C binary, vendored sqlite/zstd, recovery
        scripts).  These two artifacts live in the main ``src/`` tree
        and are copied onto the meta disc at build time -- exactly like
        ``standalone_restorer.py``, which is likewise absent from that
        manifest.  They are pinned by git
        (every ``src/`` file is version-controlled) and the wordlist is
        additionally guarded by the 45 official SLIP-0039 conformance
        vectors in the test suite.  Adding ``./src/...`` rows to a
        ``recovery/``-rooted manifest would be flagged as unexpected by
        its own verifier, so the correct treatment is build-time shipping,
        documented here.
        """
        src = Path(__file__).parent / "keyshare_combine.py"
        if not src.is_file():
            raise FileNotFoundError(
                f"keyshare_combine.py missing from source tree: {src}"
            )
        dst = self._output / "keyshare_combine.py"
        shutil.copy2(str(src), str(dst))
        os.chmod(str(dst), 0o755)

    def _bundle_recovery_toolchain_artifacts(self) -> None:
        """Bundle the C89 + POSIX-sh recovery toolchain onto the meta disc.

        Layout produced under ``output_dir/recovery/``::

            recovery/
            ├── bin/<arch>/lcsas-restore       (if built)
            ├── bin/<arch>/lcsas-keyshare      (if built)
            ├── bin/<arch>/lcsas-ecc           (if built)
            ├── bin/<arch>/lcsas-iso9660       (if built)
            ├── src/                            C source
            ├── vendored/                       sqlite + zstd amalgamation
            ├── scripts/                        POSIX-sh drivers
            ├── docs/                           plain-text docs
            ├── Makefile
            └── VERSION

        Which source files are copied is decided by
        ``_copy_recovery_tree``: in a git checkout, only the tracked ones
        plus ``bin/`` (#562).

        Missing per-arch binaries are silently skipped; the recovery
        cascade falls through to the vendored ``rustic-static`` (tier 2)
        and ultimately the Python reader (tier 3) when the
        prebuilt binary is absent.  See ``recovery/scripts/restore.sh``.
        """
        src = self._recovery_dir
        if not src.is_dir():
            # build() and _write_restore_script fail loud without it (#689).
            return

        dst = self._output / "recovery"
        if dst.exists():
            shutil.rmtree(str(dst))
        self._copy_recovery_tree(src, dst)

        # _write_restore_script copies scripts/restore.sh to the disc
        # root as restore.sh, the disc's one restore driver (#689).

        # Surface restore.bat at the meta disc root so Windows heirs
        # who insert the disc see "restore.bat" in File Explorer and
        # can double-click it without descending into recovery/scripts/.
        new_bat = dst / "scripts" / "restore.bat"
        if new_bat.is_file():
            top_bat = self._output / "restore.bat"
            shutil.copy2(str(new_bat), str(top_bat))

        # Phase 21.1: bundle per-target prebuilt rustic + python from the
        # upstream cache populated by `make fetch-recovery`.  Tolerant of
        # a missing or partial cache so single-arch developer builds
        # keep working without the full ~600 MB download.
        self._bundle_upstream_binaries(dst)

        # Phase 21.10.b: bundle per-target prebuilt lcsas-restore (the
        # tier-1 C89 binary) cross-built by `make build-recovery` or
        # `lcsas recovery build --arch X`.  Tolerant of missing builds
        # for the same reason as the upstream bundler.
        self._bundle_tier1_binaries(dst)

        # Phase 21.4: regenerate the meta disc's recovery/MANIFEST.sha256
        # so every bundled binary (tier 1 from _bundle_tier1_binaries,
        # tier 2 + 3 from _bundle_upstream_binaries) is part of the
        # single integrity manifest an heir can `sha256sum -c`
        # against.  Must run after BOTH bundlers, so it stays here at
        # the orchestrator level rather than inside either bundler.
        self._regenerate_recovery_manifest(dst)

    def _copy_recovery_tree(self, src: Path, dst: Path) -> None:
        """Copy the source files of recovery/ that belong on the meta disc.

        recovery/MANIFEST.sha256 lists the files we author, and git is
        its oracle: every tracked file under recovery/ except ``bin/`` and
        the two manifests.  So in a git checkout this copies exactly the
        tracked files, plus ``bin/`` as it stands (build debris aside),
        because ``_regenerate_recovery_manifest`` lists whatever ends up
        under ``bin/``.  Untracked files, such as the thousands of
        fuzzer-generated corpus entries a local ``make audit-gate``
        leaves behind, stay off the disc (#562).  Tracked files are copied
        as they are in the work tree, local edits included.

        Outside a git checkout (a source tarball, say) git cannot say
        what is tracked, so the whole tree is copied minus build debris
        and the fuzzer output ``recovery/fuzz/.gitignore`` names, with a
        warning: any other stray file would fail ``meta verify --strict``.
        """
        tracked, why_not = _git_tracked_files(src)
        if tracked is None:
            _logger.warning(
                "Meta disc: git cannot list the tracked files in %s (%s); "
                "it may not be a git checkout. Copying the "
                "whole directory, minus build debris and fuzzer output. Any "
                "other file there that recovery/MANIFEST.sha256 does not "
                "list will fail `lcsas meta verify --strict`. Build the "
                "meta disc from a git checkout to bundle only tracked "
                "files (#562).",
                src, why_not,
            )
            shutil.copytree(
                str(src), str(dst), ignore=_fallback_recovery_ignore(src),
            )
            return

        dst.mkdir(parents=True)
        gone: list[str] = []
        for rel in tracked:
            if rel.split("/", 1)[0] == "bin":
                continue  # copied whole below
            src_file = src / rel
            if not src_file.is_file():
                gone.append(rel)  # in the index, deleted from the work tree
                continue
            dst_file = dst / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_file), str(dst_file))
        if gone:
            _logger.warning(
                "Meta disc: %d tracked recovery/ file(s) are missing from "
                "the work tree and were not bundled: %s",
                len(gone), ", ".join(gone),
            )

        bin_src = src / "bin"
        if bin_src.is_dir():
            shutil.copytree(
                str(bin_src), str(dst / "bin"),
                ignore=shutil.ignore_patterns(*_RECOVERY_DEBRIS),
            )

    def unlisted_recovery_files(self) -> list[str]:
        """Files under the built recovery/ that its MANIFEST.sha256 omits.

        The same set ``lcsas meta verify --strict`` reports as EXTRA, so
        ``cmd_meta_build`` can warn at build time rather than leave it to
        a verify run (#562).  Call it after ``build()``: later build steps
        still write into recovery/.  Empty when there is no
        recovery/MANIFEST.sha256 to compare against.
        """
        recovery_root = self._output / "recovery"
        manifest = recovery_root / "MANIFEST.sha256"
        if not manifest.is_file():
            return []
        return files_outside_manifest(
            recovery_root, manifest_listed_paths(manifest)
        )

    def _verify_cached_upstream_archives(self, cache_root: Path) -> None:
        """Re-verify every PRESENT cached upstream archive against
        recovery/UPSTREAM.sha256 before copying (issue #372).

        fetch_upstream.sh verifies on download, but a cache entry can rot or
        be tampered with between fetch and burn.  Raise MetaBuildError on the
        first hash mismatch so a corrupted rustic/restic/python never reaches
        a burned disc.  Absent archives are skipped — a partial cache is a
        legitimate single-arch build, and the per-target copy loop already
        skips absent targets — so this fails only on genuine corruption.
        """
        import hashlib
        manifest = self._recovery_dir / "UPSTREAM.sha256"
        if not manifest.is_file():
            return
        mismatches: list[str] = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            expected, relpath = parts[0], parts[1]
            cached = cache_root / relpath
            if not cached.is_file():
                continue  # partial cache — the copy loop skips it too
            digest = hashlib.sha256()
            with cached.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            got = digest.hexdigest()
            if got != expected:
                mismatches.append(f"{relpath}: expected {expected}, got {got}")
        if mismatches:
            raise MetaBuildError(
                "cached upstream artifact(s) FAILED re-verification against "
                "recovery/UPSTREAM.sha256 — refusing to bundle a possibly "
                "corrupted or tampered cache (issue #372):\n  "
                + "\n  ".join(mismatches)
            )

    def _bundle_upstream_binaries(self, recovery_dst: Path) -> None:
        """Copy cached upstream rustic + python tarball contents per target.

        Reads from ``$LCSAS_RECOVERY_CACHE`` (default
        ``~/.cache/lcsas/recovery-binaries``).  For every approved target
        present in the cache, writes:

            recovery_dst/bin/<target>/rustic-static          (from rustic/<target>/rustic)
            recovery_dst/bin/<target>/python/                (from python/<target>/python/)

        Targets with no rustic binary AND no python tree are skipped
        silently — the recovery cascade in ``restore.sh`` handles the
        empty case (falls through tier 2 → tier 3 → fatal).
        """
        cache_root_env = os.environ.get("LCSAS_RECOVERY_CACHE")
        if cache_root_env:
            cache_root = Path(cache_root_env)
        else:
            cache_root = Path.home() / ".cache" / "lcsas" / "recovery-binaries"

        if not cache_root.is_dir():
            return  # No cache → meta build still works for single-arch path.

        # Issue #372 — supply-chain: re-verify the cached upstream archives
        # against recovery/UPSTREAM.sha256 BEFORE copying anything onto the
        # disc.  fetch_upstream.sh verifies on download, but a cache entry
        # could rot or be tampered with between fetch and burn; catch that
        # here rather than shipping a corrupted rustic/restic/python.  Only
        # PRESENT archives are checked (a partial cache is legitimate — the
        # per-target loop below already skips absent targets), so this fails
        # only on genuine corruption, never on incompleteness.
        self._verify_cached_upstream_archives(cache_root)

        # Approved targets per docs/history/CROSS_PLATFORM_META_RFC.md §3.  We
        # iterate this list rather than `os.listdir(cache_root)` so a
        # corrupted/extra directory in the cache can't silently extend
        # what we ship.
        for target in (
            "x86_64-unknown-linux-musl",
            "aarch64-unknown-linux-musl",
            "armv7-unknown-linux-gnueabihf",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
            "x86_64-pc-windows-gnu",
        ):
            bin_dst = recovery_dst / "bin" / target
            staged_any = False

            # ── rustic ─────────────────────────────────────────────
            # The fetch script extracts the tarball next to itself; the
            # binary is named "rustic" (or "rustic.exe" on Windows).
            rustic_cache = cache_root / "rustic" / target
            for cand in (rustic_cache / "rustic", rustic_cache / "rustic.exe"):
                if cand.is_file():
                    bin_dst.mkdir(parents=True, exist_ok=True)
                    out_name = "rustic-static.exe" if cand.suffix == ".exe" else "rustic-static"
                    shutil.copy2(str(cand), str(bin_dst / out_name))
                    os.chmod(str(bin_dst / out_name), 0o755)
                    staged_any = True
                    break

            # ── restic ─────────────────────────────────────────────
            # Stock upstream restic (the audited Go original), bundled so the
            # "standard tools" recovery tier works fully offline.  The fetch
            # script decompresses the per-platform asset to "restic" (or
            # "restic.exe" on Windows).  Kept under its own name (not
            # rustic-static) so the heir runbook can point at it explicitly.
            restic_cache = cache_root / "restic" / target
            for cand in (restic_cache / "restic", restic_cache / "restic.exe"):
                if cand.is_file():
                    bin_dst.mkdir(parents=True, exist_ok=True)
                    out_name = "restic.exe" if cand.suffix == ".exe" else "restic"
                    shutil.copy2(str(cand), str(bin_dst / out_name))
                    os.chmod(str(bin_dst / out_name), 0o755)
                    staged_any = True
                    break

            # ── python ─────────────────────────────────────────────
            # python-build-standalone tarballs extract to a "python/"
            # tree (Linux/macOS) or a flat install tree (Windows;
            # python.exe sits at the top).  Copy the whole tree.
            # The Windows python is cached under the msvc triple (PBS
            # ships no windows-gnu build) — remap the lookup (#404).
            py_target = self.PYTHON_TRIPLE_OVERRIDE.get(target, target)
            python_cache = cache_root / "python" / py_target / "python"
            if python_cache.is_dir():
                py_dst = bin_dst / "python"
                bin_dst.mkdir(parents=True, exist_ok=True)
                if py_dst.exists():
                    shutil.rmtree(str(py_dst))
                shutil.copytree(
                    str(python_cache), str(py_dst), symlinks=True,
                )
                staged_any = True
            else:
                # Windows PBS tarballs extract to "python/" directly or
                # to the target root; try both.
                python_alt = cache_root / "python" / py_target
                python_exe = python_alt / "python.exe"
                if python_exe.is_file():
                    py_dst = bin_dst / "python"
                    bin_dst.mkdir(parents=True, exist_ok=True)
                    if py_dst.exists():
                        shutil.rmtree(str(py_dst))
                    # Copy everything in python_alt EXCEPT the tarball.
                    py_dst.mkdir(parents=True)
                    for entry in python_alt.iterdir():
                        if entry.name.endswith(".tar.gz") or entry.name == ".extracted":
                            continue
                        if entry.is_dir():
                            shutil.copytree(
                                str(entry), str(py_dst / entry.name), symlinks=True,
                            )
                        else:
                            shutil.copy2(str(entry), str(py_dst / entry.name))
                    staged_any = True

            if staged_any:
                # No-op: directory already created by the per-binary
                # branches; documenting intent.
                pass

    def _bundle_tier1_binaries(self, recovery_dst: Path) -> None:
        """Copy cross-built lcsas-restore binaries into per-target dirs.

        ``RecoveryBuilder.cross_build`` (`src/lcsas/recovery/build.py:99`)
        writes the C89 lcsas-restore binary to
        ``recovery/bin/<short-arch>/lcsas-restore[.exe]`` using its
        short-arch convention (``x86_64``, ``aarch64``,
        ``x86_64-windows``, …).  ``recovery/scripts/restore.sh`` (the
        Phase 21.1.c dispatcher) looks for it at
        ``recovery/bin/<rust-triple>/lcsas-restore[.exe]`` using the
        rust-style target triple.

        Phase 21.10.b closes the naming gap at bundle time: for every
        approved rust-triple target whose short-arch mapping has a
        pre-built lcsas-restore in the source recovery tree, copy it
        into the meta disc tree under the rust-triple path.  Targets
        without a mapping (`armv7`, the two macOS targets) are
        skipped silently; targets whose short-arch directory is
        empty are also skipped (owner hasn't built that arch yet).

        KEY-05 extends the same relocation to ``lcsas-keyshare[.exe]``
        (the static share combiner): the heir-facing Windows docs
        name a single on-disc bin dir per target, so the combiner must
        live next to lcsas-restore under the rust triple.  The
        short-arch copies remain on disc too (``_copy_recovery_tree``
        copies the source ``recovery/bin/`` as it stands).

        The owner triggers the underlying cross-builds with
        ``lcsas recovery build --arch <short-arch>`` for each desired
        target, or `make build-recovery` to do all reachable targets
        in one shot.

        See `docs/history/CROSS_PLATFORM_META_RFC.md` §6 Q6 for the gap
        analysis; Phase 21.11 / 21.12 closed the remaining targets.
        """
        # rust-triple → (short_arch_dir_name, lcsas-restore_filename)
        # Phase 21.12 closed the last open mapping; every approved
        # target now has a tier-1 path.  Keyed off APPROVED_TARGETS
        # (RST-05) so the map can never drift from the contract.
        tier1_map: dict[str, tuple[str, str] | None] = {
            "x86_64-unknown-linux-musl":     ("x86_64", "lcsas-restore"),
            "aarch64-unknown-linux-musl":    ("aarch64", "lcsas-restore"),
            "armv7-unknown-linux-gnueabihf": ("armv7", "lcsas-restore"),
            "aarch64-apple-darwin":          ("aarch64-macos", "lcsas-restore"),
            "x86_64-apple-darwin":           ("x86_64-macos", "lcsas-restore"),
            "x86_64-pc-windows-gnu":         ("x86_64-windows", "lcsas-restore.exe"),
        }
        assert set(tier1_map) == set(APPROVED_TARGETS), (
            "tier1_map keys drifted from APPROVED_TARGETS "
            "(src/lcsas/meta/required_contents.py)"
        )

        src_recovery = self._recovery_dir
        for rust_triple, mapping in tier1_map.items():
            if mapping is None:
                continue
            short_arch, exe_name = mapping
            # KEY-05: the static share combiner is part of the
            # tier-1 binary family — relocate it next to lcsas-restore
            # so the heir docs can name one on-disc Windows path
            # (bin\<rust-triple>\lcsas-keyshare.exe).
            #
            # FMT-01: the in-house RS03 ECC tool ``lcsas-ecc`` is likewise
            # a required member of the tier-1 binary family — it is the
            # bundled "my disc is scratched" repair tool restore.sh
            # --check-disc dispatches.  It is copied here next to
            # lcsas-restore; the post-build required-contents gate
            # (required_target_paths → missing_required_contents) FAILS
            # LOUD if any target's lcsas-ecc is absent, so a bare
            # skip-if-absent here can never produce a silently incomplete
            # meta disc (mirrors how lcsas-restore completeness is
            # enforced).
            suffix = ".exe" if exe_name.endswith(".exe") else ""
            for name in (
                exe_name,
                f"lcsas-keyshare{suffix}",
                f"lcsas-ecc{suffix}",
            ):
                src_bin = src_recovery / "bin" / short_arch / name
                if not src_bin.is_file():
                    continue
                dst_bin_dir = recovery_dst / "bin" / rust_triple
                dst_bin_dir.mkdir(parents=True, exist_ok=True)
                dst_bin = dst_bin_dir / name
                shutil.copy2(str(src_bin), str(dst_bin))
                # Preserve the executable bit on Unix targets (Windows
                # binaries don't care about the +x mode, but extra +x
                # never hurts).
                os.chmod(str(dst_bin), 0o755)

    def _regenerate_recovery_manifest(self, recovery_dst: Path) -> None:
        """Merge bundled upstream binaries into recovery/MANIFEST.sha256.

        The source-tree MANIFEST already covers files we author (C
        source, scripts, docs, vendored sqlite/zstd, …).  This step
        adds the per-target rustic + python tree rooted at
        ``bin/<target>/`` so the meta disc ships with a single
        integrity manifest covering everything under ``recovery/``.

        Idempotent: re-running rewrites the file in place; the source
        entries are preserved, the bin/<target>/ entries are recomputed.

        Skipped (silent) when ``MANIFEST.sha256`` is absent from the
        recovery tree (some older builds didn't ship it) — there's
        nothing to merge into.
        """
        import hashlib

        manifest_path = recovery_dst / "MANIFEST.sha256"
        if not manifest_path.is_file():
            return

        # Read existing entries, keep anything NOT under ./bin/ — those
        # rows are about to be regenerated and stale ones would shadow.
        existing_lines: list[str] = []
        for line in manifest_path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                existing_lines.append(line)
                continue
            # Parse "<sha>  <path>".  Drop bin/* entries (they get
            # regenerated below); keep everything else verbatim.
            parts = stripped.split("  ", 1)
            if len(parts) == 2 and parts[1].startswith("./bin/"):
                continue
            existing_lines.append(line)

        # Walk every file under recovery_dst/bin/ and produce a new
        # entry.  Sort by relative path for deterministic output —
        # makes the manifest reviewable and SHA-stable across builds
        # that bundle the same target set.
        bin_root = recovery_dst / "bin"
        bin_entries: list[str] = []
        if bin_root.is_dir():
            for path in sorted(bin_root.rglob("*")):
                if not path.is_file():
                    continue
                # Path inside recovery/ — manifest format is "./relative/path".
                rel = path.relative_to(recovery_dst).as_posix()
                # Compute SHA-256 streaming so this scales to the ~30 MB
                # python tarballs without slurping into memory.
                h = hashlib.sha256()
                with path.open("rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        h.update(chunk)
                bin_entries.append(f"{h.hexdigest()}  ./{rel}")

        merged = "\n".join(existing_lines + bin_entries) + "\n"
        manifest_path.write_text(merged)

    def _bundle_metadata(self) -> None:
        """Copy each repository's metadata (keys, config, index, snapshots) onto the meta disc.

        The meta disc does NOT carry a catalog.db — it would always be
        stale (pre-dating data discs burned after the meta disc).
        Instead, the restore script bootstraps from the catalog on the
        first data disc the heir inserts, and upgrades organically
        when it encounters a fresher catalog on a later disc.

        We do bundle repository metadata (keys, config, index, snapshots)
        because keys are needed to decrypt packs and don't go stale.

        A repo whose mirror is unreachable is skipped rather than fatal
        — a stale catalog row (a deregistered repo, a mirror offline at
        build time) must not block a meta disc.  But the skip is
        recorded, not silent (#435): every outcome lands in
        ``metadata_results``, which the log, the build summary, and
        ``volume_info.json`` on the disc all report.

        Each folder that gets anything copied into it also gets its
        backup set marker, ``LCSAS_REPO.txt`` (rc6 C1): the same file,
        from the same renderer, as on the data discs.
        """
        self._metadata_results = []
        if self._catalog_db_path is None:
            return
        src = Path(self._catalog_db_path)
        if not src.is_file():
            return

        import sqlite3
        conn = sqlite3.connect(
            f"file:{src}?mode=ro&immutable=1", uri=True
        )
        # The split reader (key_escrow.get_split_for_repo) needs named
        # columns; a Row still unpacks like a tuple below.
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT repo_id, mirror_path FROM repositories"
            ).fetchall()
            retired = self._read_retired_repos(conn)
            packs_on_discs = self._read_packs_on_discs(conn)
            markers = self._read_repo_markers(conn, [r[0] for r in rows])
        finally:
            conn.close()

        meta_root = self._output / "metadata"
        meta_root.mkdir(parents=True, exist_ok=True)
        for repo_id, mirror_path in rows:
            is_retired = repo_id in retired
            n_on_discs = packs_on_discs.get(repo_id, 0)
            if not mirror_path:
                self._record_metadata_skip(
                    repo_id, "", "no mirror_path recorded in the catalog",
                    retired=is_retired, packs_on_discs=n_on_discs,
                )
                continue
            mp = Path(mirror_path)
            # Bounded probe only.  The copies below stay in this
            # process: abandoning a shutil.copytree on a timeout would
            # leave it writing a partial keys/ into the output after
            # build() returned successfully — a truncated key is worse
            # than a hang.  A mirror that goes stale *between* this
            # probe and the copy therefore still blocks; the probe is
            # what is bounded, per #435's acceptance criteria.
            reachable = _is_dir_bounded(mp)
            if reachable is None:
                self._record_metadata_skip(
                    repo_id, str(mp),
                    f"mirror did not answer within {_MIRROR_PROBE_TIMEOUT_S:g}s "
                    "(stale or hung mount?)",
                    retired=is_retired, packs_on_discs=n_on_discs,
                )
                continue
            if not reachable:
                self._record_metadata_skip(
                    repo_id, str(mp), "mirror path is not a readable directory",
                    retired=is_retired, packs_on_discs=n_on_discs,
                )
                continue
            dst_repo = meta_root / repo_id
            dst_repo.mkdir(parents=True, exist_ok=True)
            copied: list[str] = []
            for sub in ("config", "keys", "index", "snapshots"):
                s = mp / sub
                d = dst_repo / sub
                if s.is_file() and not d.exists():
                    shutil.copy2(str(s), str(d))
                elif s.is_dir() and not d.exists():
                    shutil.copytree(str(s), str(d))
                if d.exists():
                    copied.append(sub)
            marker = markers.get(repo_id)
            if copied and marker is not None:
                from lcsas.staging.repo_marker import write_repo_marker
                write_repo_marker(dst_repo, marker)
            self._metadata_results.append(
                RepoMetadataResult(
                    repo_id=repo_id,
                    mirror_path=str(mp),
                    bundled=True,
                    parts=tuple(copied),
                    retired=is_retired,
                    packs_on_discs=n_on_discs,
                )
            )

    def _read_repo_markers(
        self, conn: sqlite3.Connection, repo_ids: list[str]
    ) -> dict[str, RepoMarker]:
        """Each catalogued repository's backup set marker (rc6 C1).

        Names: the config key that maps the repo_id when the build has a
        config, else the catalog name, else the id.  Split: the catalog's
        recorded split, else none; only a catalog with no ``key_escrow``
        table (before schema v9) takes the config's split instead.  A
        hand-rolled catalog with no ``name`` column or no ``key_escrow``
        table still gets a marker (name = id).
        """
        from lcsas.db.key_escrow import config_names_by_repo_id
        from lcsas.staging.repo_marker import repo_markers

        config_names: dict[str, str] = {}
        if self._config is not None:
            config_names = config_names_by_repo_id(conn, {
                name: repo.mirror_path
                for name, repo in self._config.repositories.items()
            })
        return repo_markers(
            conn, repo_ids, config=self._config, config_names=config_names,
        )

    @staticmethod
    def _read_retired_repos(conn: sqlite3.Connection) -> set[str]:
        """Repo ids marked retired in the catalog (schema v10+).

        A catalog older than v10 has no ``status`` column at all — and
        this reader is deliberately permissive about catalog vintage, so
        probe for the column rather than letting the query raise.  No
        column means nobody has been retired yet.
        """
        try:
            cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(repositories)"
                ).fetchall()
            }
        except Exception:  # pragma: no cover - malformed catalog
            return set()
        if "status" not in cols:
            return set()
        try:
            return {
                r[0] for r in conn.execute(
                    "SELECT repo_id FROM repositories WHERE status = 'retired'"
                ).fetchall()
            }
        except Exception:  # pragma: no cover - malformed catalog
            return set()

    @staticmethod
    def _read_packs_on_discs(conn: sqlite3.Connection) -> dict[str, int]:
        """Per-repo count of packs that live on a volume which still exists.

        ``volumes.status != 'DESTROYED'`` is the same filter the tier-1 C
        reader applies (see ``TIER1_FROZEN_SURFACE`` in db/schema.py), so
        the key gate and the C reader agree on what "data that
        still exists" means.

        Deliberately NOT filtered on ``packs.is_pruned``: is_pruned is not
        part of the tier-1 frozen surface, so tier-1 will happily restore
        a pruned pack that is still on a disc.  A repo whose only
        on-disc packs are pruned therefore still needs its keys.

        Missing tables yield an empty mapping rather than raising — a
        catalog oddity must never block a meta disc, and hand-rolled
        minimal catalogs (tests, partial legacy catalogs) may carry
        ``repositories`` alone.
        """
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        except Exception:  # pragma: no cover - malformed catalog
            return {}
        if not {"packs", "volume_packs", "volumes"} <= tables:
            return {}
        try:
            return {
                r[0]: r[1] for r in conn.execute(
                    "SELECT p.repo_id, COUNT(DISTINCT p.pack_id) "
                    "FROM packs p "
                    "JOIN volume_packs vp ON vp.pack_id = p.pack_id "
                    "JOIN volumes v ON v.volume_id = vp.volume_id "
                    "WHERE v.status != 'DESTROYED' "
                    "GROUP BY p.repo_id"
                ).fetchall()
            }
        except Exception:  # pragma: no cover - malformed catalog
            return {}

    def _record_metadata_skip(
        self, repo_id: str, mirror_path: str, detail: str,
        retired: bool = False, packs_on_discs: int = 0,
    ) -> None:
        """Log and record a repository whose metadata was NOT bundled."""
        _logger.warning(
            "Meta disc: NO repository metadata bundled for repo '%s' "
            "(mirror %s): %s — this disc carries no keys for that repository, "
            "so its packs cannot be decrypted from the meta disc alone.",
            repo_id, mirror_path or "<unset>", detail,
        )
        self._metadata_results.append(
            RepoMetadataResult(
                repo_id=repo_id,
                mirror_path=mirror_path,
                bundled=False,
                detail=detail,
                retired=retired,
                packs_on_discs=packs_on_discs,
            )
        )

    @property
    def metadata_results(self) -> list[RepoMetadataResult]:
        """Per-repo outcome of the last ``_bundle_metadata`` run (#435)."""
        return list(self._metadata_results)

    def _require_restore_driver(self, driver: Path) -> None:
        """Fail loud when the recovery/ tree's restore.sh is missing.

        The meta disc has one restore driver, ``recovery/scripts/restore.sh``
        (#689).  The builder used to fall back to a legacy bash driver
        when there was no recovery/ tree; that driver is retired, and a
        meta disc without the recovery/ tree has no tier-1 binaries
        either.  So a build from an install with no recovery/ tree (a
        wheel, or the host-A Docker image, which copies only ``src/``)
        stops here with a clear message instead of burning a disc that
        cannot restore.
        """
        if not self._bundle_recovery_toolchain:
            raise MetaBuildError(
                "bundle_recovery_toolchain=False leaves the meta disc with no "
                "restore driver: recovery/scripts/restore.sh comes from the "
                "recovery/ tree (#689)."
            )
        if driver.is_file():
            return
        raise MetaBuildError(
            f"the restore driver {driver} is missing. A meta disc needs "
            "the recovery/ tree of an LCSAS source checkout: run "
            "`lcsas meta build` from the checkout, or pass "
            "--project-root <checkout>."
        )

    def _write_restore_script(self) -> None:
        """Install the meta disc's one restore driver as ``/restore.sh``.

        It is the POSIX-sh driver ``recovery/scripts/restore.sh`` from the
        bundled recovery/ tree, stamped with the build SHA and date.  It
        is Python-free for tiers 1-2 and only touches Python at tier 3
        (LCSAS_ALLOW_PYTHON_TIER).  The copy left in recovery/scripts/
        becomes a two-line redirect to the root one, so the disc holds
        the driver once.  No recovery/ tree is a build error (#689).
        """
        script_path = self._output / "restore.sh"
        new_driver = self._output / "recovery" / "scripts" / "restore.sh"
        self._require_restore_driver(new_driver)
        # Files an older build left behind: the retired drivers (#689).
        for rel in _RETIRED_META_FILES:
            (self._output / rel).unlink(missing_ok=True)
        shutil.copy2(str(new_driver), str(script_path))
        os.chmod(str(script_path), 0o755)
        # Stamp build SHA + date into the restore script placeholders.
        import subprocess as _sp
        try:
            _sha = _sp.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(Path(__file__).resolve().parent),
                text=True,
                stderr=_sp.DEVNULL,
            ).strip()
        except Exception:
            _sha = "unknown"
        _build_date = date.today().isoformat()
        _content = script_path.read_text(encoding="utf-8")
        _content = _content.replace("@@BUILD_SHA@@", _sha).replace(
            "@@BUILD_DATE@@", _build_date
        )
        _write_and_sync(script_path, _content)
        # Replace the inner copy with a redirect so heirs (and agents)
        # who navigate into recovery/scripts/ land on the root-level
        # entry, not a silent duplicate.  "$(dirname "$0")/../../restore.sh"
        # resolves to /mnt/restore.sh wherever the disc is mounted.
        _write_and_sync(
            new_driver,
            "#!/bin/sh\n"
            "exec \"$(dirname \"$0\")/../../restore.sh\" \"$@\"\n",
        )
        os.chmod(str(new_driver), 0o755)
        # The recovery/MANIFEST.sha256 row for scripts/restore.sh was
        # written from the source-tree file, but we just replaced that
        # file with the redirect stub above.  Refresh the row so an heir's
        # `sha256sum -c recovery/MANIFEST.sha256` matches the stub on
        # disc instead of reporting a scary MISMATCH on the single most
        # important recovery file (issue #365).
        self._refresh_recovery_manifest_row(new_driver, "./scripts/restore.sh")

    def _refresh_recovery_manifest_row(
        self, on_disc: Path, rel: str, *, add_missing: bool = False
    ) -> None:
        """Update one row of recovery/MANIFEST.sha256 to match a file we
        rewrote after the manifest was generated.

        ``rel`` is the manifest-relative path (e.g. ``./scripts/restore.sh``).
        No-op when the manifest is absent (older builds).  A manifest with
        no such row is left alone, unless ``add_missing`` is set: then the
        row is appended, for a file the builder itself adds under
        recovery/ (``lcsas meta verify --strict`` and the BOOT-07 gate
        reject any file there that has no row).  Keeps every other row
        untouched.
        """
        import hashlib

        manifest = self._output / "recovery" / "MANIFEST.sha256"
        if not manifest.is_file():
            return
        h = hashlib.sha256()
        with on_disc.open("rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        new_row = f"{h.hexdigest()}  {rel}"
        out: list[str] = []
        replaced = False
        for line in manifest.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split("  ", 1)
            if len(parts) == 2 and parts[1] == rel:
                out.append(new_row)
                replaced = True
            else:
                out.append(line)
        if not replaced and add_missing:
            out.append(new_row)
            replaced = True
        if replaced:
            _write_and_sync(manifest, "\n".join(out) + "\n")

    def _write_readme(self) -> None:
        """Write the human-readable restore instructions."""
        readme_path = self._output / "README_RESTORE.md"
        _write_and_sync(readme_path, README_RESTORE)

    def _write_readme_txt(self) -> None:
        """Write a plain-text version of README_RESTORE.

        Markdown is hard to read on bare terminals.  This converts
        the Markdown to best-effort plain text by stripping formatting.
        """
        txt = _strip_markdown(README_RESTORE)
        _write_and_sync(self._output / "README_RESTORE.txt", txt)

    def _write_volume_info(self) -> None:
        """Write self-describing metadata for the meta disc."""
        # Determine which optional tools were actually bundled
        tools_bin = self._output / "tools" / "bin"
        bundled_tools = list(_REQUIRED_TOOLS) + ["python3"]
        for tool in _OPTIONAL_TOOLS:
            if (tools_bin / tool).exists():
                bundled_tools.append(tool)
        if (tools_bin / "rustic-static").exists():
            bundled_tools.append("rustic-static")

        # Collect tool versions
        tool_versions = {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }
        for tool_name in ("rustic", "xorriso", "dvdisaster"):
            tool_path = tools_bin / tool_name
            if tool_path.exists():
                tool_versions[tool_name] = _get_tool_version(tool_path)
        if (tools_bin / "rustic-static").exists():
            tool_versions["rustic-static"] = _get_tool_version(
                tools_bin / "rustic-static"
            )

        built_at = datetime.now(UTC)
        info = {
            "type": "meta",
            "description": "LCSAS meta disc — tools + source for restoring the archive",
            "created_at": built_at.isoformat(),
            "platform": f"linux-{os.uname().machine}",
            "python_version": (
                f"{sys.version_info.major}.{sys.version_info.minor}"
                f".{sys.version_info.micro}"
            ),
            "contents": {
                "tools": bundled_tools,
                "tool_versions": tool_versions,
                "lcsas_source": True,
                "restore_script": "restore.sh",
                # Key name kept for readers of older discs.  Since #689
                # it names the one non-interactive wrapper,
                # recovery/scripts/restore_auto.sh.
                "restore_auto_script": RESTORE_AUTO_SCRIPT_PATH,
                "documentation": True,
            },
            # Tier-3 zstd capability (RST-04).  ``native_zstd`` records
            # whether the build host's ``zstandard`` C extension was bundled
            # (fast path, host-arch/CPython-specific).  ``pure_python_zstd``
            # is always True: the stdlib-only decoder
            # (lcsas.restore._zstd_pure) ships in the LCSAS source and works
            # on every target / CPython minor.
            "zstd_support": {
                "native_zstd": self._native_zstd_bundled,
                "native_zstd_arch": (
                    f"linux-{os.uname().machine}-cp"
                    f"{sys.version_info.major}{sys.version_info.minor}"
                    if self._native_zstd_bundled else None
                ),
                "pure_python_zstd": True,
            },
            # Which catalogued repositories' metadata (keys/, config,
            # index, snapshots) this disc actually carries (#435).  A
            # repo listed under ``not_bundled`` has NO keys here — its
            # packs cannot be decrypted from this disc alone.  Both
            # lists are empty on a no-catalog build, which bundles no
            # repository metadata by design.
            # ``acknowledged`` is true when this disc was built with
            # --allow-missing-metadata (#437): the owner was told a
            # live repo's keys are absent and chose to master it anyway.
            # Recorded here so the *disc* carries that fact — years later
            # a restoring heir can tell an accepted gap from a defect.
            "repo_metadata": {
                "acknowledged": self._allow_missing_metadata,
                "bundled": [
                    {"repo_id": r.repo_id, "parts": list(r.parts)}
                    for r in self._metadata_results if r.bundled
                ],
                "not_bundled": [
                    {
                        "repo_id": r.repo_id,
                        "mirror_path": r.mirror_path,
                        "reason": r.detail,
                        "retired": r.retired,
                        "packs_on_discs": r.packs_on_discs,
                    }
                    for r in self._metadata_results if not r.bundled
                ],
            },
            "requires": {
                "key_file": "The repository's password (typed, or in a password file)",
                "data_isos": "LCSAS data discs or their ISO images",
            },
        }
        info_path = self._output / "volume_info.json"
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _write_and_sync(
            self._output / "VOLUME_INFO.txt", render_meta_volume_info_txt(built_at)
        )

    def _write_start_here(self) -> None:
        """Write START_HERE.txt to the meta disc.

        One renderer serves both build variants (UX-05): the
        production (config) build gets the same per-OS dispatch and
        path-qualified commands as the no-config build, plus the
        survivability fields.  The data-disc generator
        (``HolographicInjector.write_start_here``) is deliberately
        NOT used here — its text points at data-disc-only files
        (RESTORE_INSTRUCTIONS.txt) and carries no runnable command.
        """
        from lcsas.staging.metadata import HolographicInjector

        injector = HolographicInjector(self._output)
        injector.write_disc_care(self._config)
        recorded = self._recorded_splits()
        if self._config is not None:
            injector.write_key_info(self._config, recorded=recorded)
            injector.write_config_summary(self._config)
        text = self._render_meta_start_here(self._config, recorded)
        _write_and_sync(self._output / "START_HERE.txt", text)

    def _recorded_splits(self) -> dict[str, tuple[int, int]]:
        """Each configured repository's recorded split K/N, by name (KEY-08).

        Read from the catalog the build was given, so the meta disc prints
        the split actually performed rather than the config's K/N (#573,
        #587).  Empty without a config or a catalog.
        """
        if self._config is None or self._catalog_db_path is None:
            return {}
        src = Path(self._catalog_db_path)
        if not src.is_file():
            return {}
        import sqlite3

        from lcsas.db.key_escrow import recorded_splits_by_name

        conn = sqlite3.connect(f"file:{src}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return recorded_splits_by_name(conn, {
                name: repo.mirror_path
                for name, repo in self._config.repositories.items()
            })
        except sqlite3.DatabaseError:  # pragma: no cover - not a catalog
            return {}
        finally:
            conn.close()

    def _render_meta_start_here(
        self,
        config: LCSASConfig | None,
        recorded: dict[str, tuple[int, int]] | None = None,
    ) -> str:
        """Render the meta disc's START_HERE.txt text (UX-05).

        The document always carries the per-OS dispatch (Windows /
        macOS / Linux) with mount-path-qualified commands and the
        honest no-OS routing (UX-03).  When *config* is given, the
        survivability fields (owner, description, password-storage hints,
        technical contact) and the share-combining pre-step are merged in.
        Every file the text references must exist on the meta disc
        (guarded by tests/unit/test_meta_builder.py).
        """
        from lcsas.staging.metadata import share_recovery_lines, split_view

        # The meta disc is always labelled LCSAS_META, whatever the archive's
        # label_prefix (#588): the estate card and every heir doc name that
        # label, and docs/workflows/meta-disc.md masters it with -V LCSAS_META.
        label = "LCSAS_META"

        # No LCSAS disc is bootable (the live-boot path was dropped per
        # BOOT-01/BOOT-07), so the no-OS route must never claim the disc
        # boots (UX-03): heirs get the borrow-a-computer + live-USB
        # routing that works today.
        no_os_block = """\
  >>> No working computer at all <<<
       These discs are NOT bootable, but they do NOT require
       a special computer.  Use any other computer — a
       friend's, a library's, or a cheap second-hand laptop.
       Windows, macOS and Linux all work (pick your section
       above).
       If a computer has no operating system at all, ask a
       helper to make a "live Linux USB stick" (free; search
       for "create Ubuntu live USB"), start the computer
       from it, then follow the macOS/Linux steps above.
       (See recovery/docs/BOOT.txt for the steps.)
"""

        about_block = ""
        key_hints_block = ""
        key_info_line = ""
        split_block = ""
        contact_block = ""
        if config is not None:
            owner = config.archive_owner or "the person who created this archive"
            description = config.archive_description or (
                "digital files backed up using LCSAS"
            )
            repo_line = ""
            if config.repositories:
                repo_names = ", ".join(sorted(config.repositories.keys()))
                repo_line = f"  Repositories in this archive: {repo_names}\n"
            about_block = (
                "WHOSE FILES ARE THESE?\n"
                "\n"
                "  These discs hold backup copies of digital files\n"
                f"  created by {owner}.\n"
                "\n"
                f"  Contents: {description}\n"
                f"{repo_line}"
                "\n"
            )
            if config.key_storage_hints:
                hints = "\n".join(
                    f"      {line.strip()}"
                    for line in config.key_storage_hints.strip().splitlines()
                )
                key_hints_block = f"    Where to find the password:\n{hints}\n"
            key_info_line = (
                "  * PASSWORD_INFO.txt on this disc lists which password\n"
                "    unlocks which repository.\n"
            )
            if split_view(config, recorded=recorded) is not None:
                # Same share-combining pre-step text as PASSWORD_INFO.txt — one
                # source so the doc-contract gates cover both.
                split_block = "\n" + "\n".join(
                    share_recovery_lines(config, recorded=recorded)
                ).rstrip() + "\n"
            if config.technical_contact:
                contact_block = (
                    "  The archive owner suggested this technical contact:\n"
                    f"      {config.technical_contact}\n"
                    "\n"
                )

        return f"""\
╔══════════════════════════════════════════════════════════╗
║                    START HERE                           ║
╚══════════════════════════════════════════════════════════╝

This is the LCSAS META disc — it contains everything needed
to restore the files on the LCSAS archive discs.

{about_block}╔══ Pick the section for your operating system ═══════════╗

  >>> Windows 10 or 11 <<<
       Open this disc in File Explorer and double-click
       restore.bat
       (See recovery/docs/RECOVER_WINDOWS.txt for details.)

  >>> macOS <<<
       Open Terminal, then run:
           sh /Volumes/{label}/restore.sh ~/restored
       (See recovery/docs/RECOVER.txt for details.)

  >>> Linux <<<
       Open a terminal, then run:
           sh /media/$USER/{label}/restore.sh ~/restored
       (or:  sudo mount /dev/sr0 /mnt
        then: sh /mnt/restore.sh ~/restored)
       (See recovery/docs/RECOVER.txt for details.)

{no_os_block}
╚══════════════════════════════════════════════════════════╝

IF THE COMPUTER REFUSES TO RUN IT

  The files on this disc are fine: a safety setting on the
  computer is in the way.  Copy the disc to your home folder
  and run the copy instead (it needs about 1 GB of space;
  delete ~/lcsas-meta when the restore has finished).

  macOS says "cannot be opened because the developer cannot
  be verified", or the restore stops with "Killed":
      cp -R /Volumes/{label} ~/lcsas-meta
      xattr -dr com.apple.quarantine ~/lcsas-meta
      sh ~/lcsas-meta/restore.sh ~/restored

  Linux says "Permission denied", says this disc "lacks all
  recovery binaries" although the files are there, or falls
  back to the slow Python tier (tier 3).  That happens when
  the files sit on a USB stick or another place that may not
  run programs:
      cp -R /media/$USER/{label} ~/lcsas-meta
      chmod -R u+x ~/lcsas-meta/recovery/bin
      sh ~/lcsas-meta/restore.sh ~/restored

WHAT YOU NEED

  * The password for the repository you want to restore (the
    owner should have written it down separately from the
    discs).
{key_hints_block}{key_info_line}  * Enough free disk space on your computer for the restored
    files.  Typical archives are 10 GB to several TB.
  * A USB Blu-ray reader (or any optical drive that can read
    the disc format used by this archive).
{split_block}
WHAT HAPPENS IF YOU LOSE THE PASSWORD

  The data cannot be restored.  This is by design — only the
  password unlocks the encryption.  There is no back door, no
  spare copy of the password held anywhere, and no company or
  service can unlock it for you.

INHERITED A WHOLE STACK OF DISCS?

  Look at the disc labels.  At least one of them will be
  labelled {label} (or similar): a META disc like this one.
  Start with it.  If there are several, use the newest; if
  that one fails, use the one burned closest in date to the
  data discs.  The restore first asks for your newest data
  disc: the latest year in its label, then the highest number
  (the full label is printed on the disc).  It knows the
  newest backups.  After that the restore tells you which data
  disc to insert next.

NEED HELP?

{contact_block}  Take all the discs plus the password to any computer
  professional.  Any system administrator or IT professional
  should be able to follow the instructions in
  recovery/docs/RECOVER.txt.

  The full source code is in recovery/src/, so a sufficiently
  motivated future implementer can rebuild the tooling from
  scratch even if every prebuilt binary on the disc has gone
  unusable.
"""
