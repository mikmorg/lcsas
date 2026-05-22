"""Phase 14: tier-1 ↔ tier-2 differential oracle.

For each content profile, build a real restic-format repo via
`rustic init`+`backup`, restore it via BOTH the tier-1 C binary
(`lcsas-restore`) and tier-2 (`rustic restore`), and assert the
restored trees are byte-identical.

Why: semantic divergence between the durable C binary and the upstream
reference rustic is otherwise undetectable until an operator restores
from disc years from now and gets wrong bytes.

Opt-in via `LCSAS_DIFF=1` (other than the smoke variant which is in
the default integration suite).  Skips when rustic isn't on PATH or
the lcsas-restore binary hasn't been built.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    diff_trees,
    restore_with_tier1,
    restore_with_tier2,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_BIN_CANDIDATES = [
    REPO_ROOT / "recovery" / "build" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64-linux-musl" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore",
]


def _find_restore_bin() -> Path | None:
    for p in RESTORE_BIN_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


@dataclass(frozen=True)
class Profile:
    name: str
    make_source: Callable[[Path], None]
    smoke: bool = False


# ── Source-content generators (each materialises files under src/) ──


def _profile_small_file(src: Path) -> None:
    (src / "alpha.txt").write_text("hello tier-1 vs tier-2\n")


def _profile_many_small_files(src: Path) -> None:
    for i in range(50):
        (src / f"f{i:03d}.txt").write_text(f"content {i}\n")


def _profile_one_large_file(src: Path) -> None:
    # 6 MiB — exceeds restic's default chunk size, so rustic splits it
    # into multiple data blobs.  Exercises multi-blob file restore.
    big = (b"A" * 4096 + b"B" * 4096) * 1024  # 8 MiB, predictable pattern
    (src / "large.bin").write_bytes(big[:6 * 1024 * 1024])


def _profile_deep_tree(src: Path) -> None:
    p = src
    for i in range(6):
        p = p / f"level{i}"
        p.mkdir()
    (p / "deep.txt").write_text("at the bottom\n")


def _profile_unicode_names(src: Path) -> None:
    (src / "café.txt").write_text("avec accent\n")
    (src / "日本語.txt").write_text("Japanese filename\n")
    (src / "emoji-🦀.txt").write_text("crab\n")


def _profile_symlinks_and_modes(src: Path) -> None:
    (src / "exec.sh").write_text("#!/bin/sh\necho ok\n")
    (src / "exec.sh").chmod(0o755)
    (src / "secret.txt").write_text("private\n")
    (src / "secret.txt").chmod(0o600)
    (src / "normal.txt").write_text("normal\n")
    (src / "normal.txt").chmod(0o644)
    # Relative symlinks only.  Tier-1 intentionally rejects absolute
    # symlinks for security (lcsas_path_safe_symlink in path.c) — a
    # known/documented divergence from tier-2 (rustic), not a bug.
    (src / "link_rel").symlink_to("normal.txt")
    (src / "link_subdir").symlink_to("../normal.txt")


def _profile_empty_dir(src: Path) -> None:
    (src / "alpha.txt").write_text("present\n")
    (src / "empty_subdir").mkdir()


def _profile_large_dir_node(src: Path) -> None:
    big = src / "big_dir"
    big.mkdir()
    for i in range(500):
        (big / f"n{i:04d}.txt").write_text(f"{i}\n")


PROFILES = [
    Profile("small_file", _profile_small_file, smoke=True),
    Profile("many_small_files", _profile_many_small_files),
    Profile("one_large_file", _profile_one_large_file),
    Profile("deep_tree", _profile_deep_tree),
    Profile("unicode_names", _profile_unicode_names),
    Profile("symlinks_and_modes", _profile_symlinks_and_modes),
    Profile("empty_dir", _profile_empty_dir),
    Profile("large_dir_node", _profile_large_dir_node),
]


def _should_run(profile: Profile) -> bool:
    """The smoke profile runs by default in the integration suite;
    everything else is opt-in via LCSAS_DIFF=1."""
    if profile.smoke:
        return True
    return bool(os.environ.get("LCSAS_DIFF"))


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_tier1_vs_tier2_byte_identical(profile: Profile, tmp_path: Path) -> None:
    if not _should_run(profile):
        pytest.skip(
            f"profile {profile.name!r}: set LCSAS_DIFF=1 to run "
            "full differential suite"
        )
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    bin_path = _find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    src = tmp_path / "src"
    src.mkdir()
    profile.make_source(src)
    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text("test-password\n")

    build_rustic_repo(src, repo, pwfile)

    a = tmp_path / "tier1_out"
    b = tmp_path / "tier2_out"
    restore_with_tier1(repo, a, pwfile, bin_path)
    restore_with_tier2(repo, b, pwfile)

    # Both restorers may write inside a copy of the original source
    # tree rooted at the original absolute path.  Compute the common
    # relative root each used by walking until we find content.
    a_root = _find_restored_root(a)
    b_root = _find_restored_root(b)

    diffs = diff_trees(a_root, b_root)
    assert not diffs, (
        f"tier-1 vs tier-2 mismatch in profile {profile.name!r}:\n"
        + "\n".join(diffs)
    )


def _find_restored_root(target: Path) -> Path:
    """Restorers may place the restored tree under `<target>/<abs_src_path>/`.

    Walk down until we hit a directory that has more than one entry
    OR a non-directory entry, treating that as the effective root.
    Compares like-for-like across both restorers because they use the
    same convention."""
    cur = target
    while True:
        try:
            entries = list(cur.iterdir())
        except FileNotFoundError:
            return target
        if len(entries) != 1:
            return cur
        only = entries[0]
        if not only.is_dir() or only.is_symlink():
            return cur
        cur = only
