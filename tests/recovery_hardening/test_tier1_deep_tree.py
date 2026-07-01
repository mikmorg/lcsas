"""T1C-04: tier-1 tree-walk recursion-depth cap.

The tier-1 C restorer walks directory trees by recursing on the C
stack, one frame per directory level, with no depth bound before
T1C-04.  Under the default 8 MB stack a tree ~1300+ levels deep killed
the process with SIGSEGV and zero diagnostic.  The fix caps recursion
at ``lcsas_tree_max_depth`` (default 1000) and fails loud with a named
error; ``LCSAS_MAX_TREE_DEPTH`` + a raised stack ulimit overrides it.

These tests build real deep trees, back them up with rustic, and drive
the real ``lcsas-restore`` binary.  They are opt-in / skip-if-absent:
they need rustic on PATH and a built binary.  Local-only until the
GATE recovery-hardening-in-CI plan promotes them to a merge gate.

Path budget note: tier-1 restores to ``<target>/<absolute-source-path>``
and *separately* caps any restored path at 4095 bytes (the other
T1C-04 guard).  So both the source base and the restore target are kept
SHORT here, and the depth-override test uses 1100 levels (over the
default 1000 cap, but ~2.2 KB of path — comfortably under 4095) rather
than a depth whose restored path would trip the path guard first.

Marked integration so they stay out of the default unit run.
"""
from __future__ import annotations

import contextlib
import os
import resource
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    find_restore_bin,
)

pytestmark = pytest.mark.integration

# Over the default cap (1000) but small enough that the restored path
# (~2 levels-bytes each) stays well under the 4095-byte path guard.
OVER_CAP_DEPTH = 1100


def _make_deep_tree(base: Path, depth: int, leaf_name: str,
                    leaf_text: str) -> None:
    """Create `depth` nested 1-char dirs under `base` and drop a file
    at the bottom.  Uses incremental chdir so the absolute path never
    has to fit in a single buffer (the source side would otherwise hit
    PATH_MAX well before 2000 levels)."""
    cwd = os.getcwd()
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chdir(base)
        for _ in range(depth):
            os.mkdir("d")
            os.chdir("d")
        with open(leaf_name, "w") as f:
            f.write(leaf_text)
    finally:
        os.chdir(cwd)


def _rmtree_deep(base: Path) -> None:
    """Remove a directory tree that may be thousands of levels deep.

    Both shutil.rmtree (Python-recursive — hits the interpreter
    recursion limit) and absolute-path os.rmdir (a ~4000-char path
    trips ENAMETOOLONG at PATH_MAX) fail on the trees these tests
    build.  Walk via chdir instead: descend into one child at a time
    (always a short relative name), unlink files, then climb back up
    removing each now-empty directory.  No recursion, no long paths."""
    cwd = os.getcwd()
    if not base.exists():
        return
    try:
        os.chdir(base)
        depth = 0
        while True:
            # Unlink any non-dir entries at this level.
            subdir = None
            for name in os.listdir("."):
                if os.path.isdir(name) and not os.path.islink(name):
                    subdir = name
                else:
                    with contextlib.suppress(OSError):
                        os.unlink(name)
            if subdir is not None:
                os.chdir(subdir)
                depth += 1
                continue
            # Leaf reached: climb up, removing each emptied directory.
            if depth == 0:
                break
            here = os.path.basename(os.getcwd())
            os.chdir("..")
            with contextlib.suppress(OSError):
                os.rmdir(here)
            depth -= 1
    finally:
        os.chdir(cwd)
        with contextlib.suppress(OSError):
            os.rmdir(base)


@pytest.fixture()
def short_base() -> Iterator[Path]:
    """A short-named scratch dir under /tmp.  Both the deep source tree
    and the restore target live here so restored absolute paths stay
    short enough not to trip the 4095-byte path guard."""
    d = Path(tempfile.mkdtemp(prefix="lt", dir="/tmp"))
    try:
        yield d
    finally:
        _rmtree_deep(d)


def _restore(bin_path: Path, repo: Path, target: Path, pwfile: Path,
             env: dict | None = None,
             preexec=None) -> subprocess.CompletedProcess:
    target.mkdir(parents=True, exist_ok=True)
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [str(bin_path),
         "--repo", str(repo),
         "--target", str(target),
         "--password-file", str(pwfile)],
        capture_output=True, text=True, timeout=300,
        env=full_env, preexec_fn=preexec,
    )


def _bottom_file(target: Path, leaf_name: str) -> Path | None:
    """Locate ``leaf_name`` under ``target`` without ``os.walk``.

    ``os.walk`` recurses via ``yield from`` on some CPython builds (it did
    on the CI runner's interpreter, though this dev box's os.walk is
    iterative), so a >1000-level tree blows the interpreter recursion
    limit deep inside ``_walk`` — the same trap ``_rmtree_deep`` documents
    (issue #378: it reddened master's CI for weeks).  Descend with chdir
    one short relative level at a time instead: iterative, and keeping the
    working directory at each level means a deep chain never trips
    PATH_MAX either.  The deep-tree fixtures are single ``d/`` chains, so
    following the one subdirectory per level reaches the bottom file."""
    cwd = os.getcwd()
    if not target.exists():
        return None
    try:
        os.chdir(target)
        rel: list[str] = []
        while True:
            found = None
            subdir = None
            for name in os.listdir("."):
                if os.path.isdir(name) and not os.path.islink(name):
                    subdir = name
                elif name == leaf_name:
                    found = name
            if found is not None:
                return target.joinpath(*rel, found)
            if subdir is None:
                return None
            os.chdir(subdir)
            rel.append(subdir)
    finally:
        os.chdir(cwd)


def _requirements() -> Path:
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    bin_path = find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")
    return bin_path


def test_deep_tree_900_levels_restores_byte_identical(
    short_base: Path,
) -> None:
    """A 900-level tree (under both the path-length and depth caps)
    restores cleanly and the bottom file is byte-identical."""
    bin_path = _requirements()
    src = short_base / "s"
    leaf, text = "deep.txt", "at the bottom\n"
    _make_deep_tree(src, 900, leaf, text)
    repo = short_base / "repo"
    pwfile = short_base / "pw"
    pwfile.write_text("test-password\n")
    try:
        build_rustic_repo(src, repo, pwfile)
        target = short_base / "o"
        res = _restore(bin_path, repo, target, pwfile)
        assert res.returncode == 0, (
            f"900-level restore failed (rc={res.returncode})\n"
            f"stderr:\n{res.stderr[-1500:]}"
        )
        bottom = _bottom_file(target, leaf)
        assert bottom is not None, "bottom file not restored"
        assert bottom.read_text() == text
    finally:
        _rmtree_deep(src)


def test_deep_tree_over_cap_fails_loud_not_signal(short_base: Path) -> None:
    """A tree deeper than the default 1000 cap must exit non-zero with a
    NAMED depth error, NOT die by signal (SIGSEGV=-11).  Depth is chosen
    so the depth cap (level 1001) fires before the 4095-byte path guard
    (~level 2000) — this isolates the depth-cap behaviour."""
    bin_path = _requirements()
    src = short_base / "s"
    _make_deep_tree(src, OVER_CAP_DEPTH, "deep.txt", "too deep\n")
    repo = short_base / "repo"
    pwfile = short_base / "pw"
    pwfile.write_text("test-password\n")
    try:
        build_rustic_repo(src, repo, pwfile)
        target = short_base / "o"
        res = _restore(bin_path, repo, target, pwfile)
        assert res.returncode > 0, (
            f"expected positive exit (clean failure), got rc={res.returncode} "
            f"(negative = died by signal — the SIGSEGV T1C-04 fixes)\n"
            f"stderr:\n{res.stderr[-1500:]}"
        )
        assert "deeper than 1000 levels" in res.stderr, (
            "missing the named depth-cap error; operator gets no "
            f"diagnostic.\nstderr:\n{res.stderr[-1500:]}"
        )
    finally:
        _rmtree_deep(src)


def test_deep_tree_over_cap_restores_with_override_and_raised_stack(
    short_base: Path,
) -> None:
    """With LCSAS_MAX_TREE_DEPTH raised above the tree depth and an
    unlimited stack ulimit, the over-cap tree restores fully — proving
    the documented escape hatch works."""
    bin_path = _requirements()
    src = short_base / "s"
    leaf, text = "deep.txt", "override path\n"
    _make_deep_tree(src, OVER_CAP_DEPTH, leaf, text)
    repo = short_base / "repo"
    pwfile = short_base / "pw"
    pwfile.write_text("test-password\n")

    def _raise_stack() -> None:
        resource.setrlimit(
            resource.RLIMIT_STACK,
            (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
        )

    try:
        build_rustic_repo(src, repo, pwfile)
        target = short_base / "o"
        res = _restore(
            bin_path, repo, target, pwfile,
            env={"LCSAS_MAX_TREE_DEPTH": "3000"},
            preexec=_raise_stack,
        )
        assert res.returncode == 0, (
            f"override restore failed (rc={res.returncode})\n"
            f"stderr:\n{res.stderr[-1500:]}"
        )
        bottom = _bottom_file(target, leaf)
        assert bottom is not None, "bottom file not restored under override"
        assert bottom.read_text() == text
    finally:
        _rmtree_deep(src)
