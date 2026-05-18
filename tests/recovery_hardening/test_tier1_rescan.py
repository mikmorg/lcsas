"""Hardening test: tier-1 binary rescans mount parents on each retry.

The bug this catches:

Before this commit, `lcsas-restore` read its `--pack-search` arg list
ONCE at startup.  When a pack was missing and the binary prompted
"press ENTER to retry", swapping a disc -- which auto-mounts at, say,
``/media/$USER/LCSAS_002/`` -- did NOT make the binary look there:
the new mount point was never in its search list.  The user could
keep hitting ENTER forever.

``recovery/docs/MULTI_DISC_DESIGN.txt`` (lines 63-64) spells out the
intended behaviour:

    Otherwise, re-scan all known search paths plus the standard
    mount-point parents (so a newly-inserted disc auto-mounted by
    the OS is found).

This test verifies three things end-to-end against the production
binary:

  1. Mount-parent rediscovery actually works -- the binary finds a
     disc whose path was NEVER passed via ``--pack-search``, only
     under a ``--mount-parent`` (or ``$LCSAS_MOUNT_DIRS``).

  2. The catalog is re-picked on retry -- if the freshest catalog
     lives on the disc the user is about to insert, the prompt's
     hash->label resolution reflects that catalog once it is mounted.

  3. ``$LCSAS_MOUNT_DIRS`` works as a parity surface with the shell
     driver -- the same env var configures both sides.

How this test guards against regressions:

  - If a refactor reverts to "stat() once at startup", case (1) fails.
  - If catalog handling is collapsed back to "one open at main()
    entry", case (2) fails -- the binary won't see the newer catalog
    and the test won't see the expected volume label in the prompt.
  - If the env-var parsing is removed from main.c, case (3) fails.

These are pedantic by design: if any of them fail, multi-disc restore
breaks for any user with one optical drive and >1 data disc -- which
is the dominant production case.  Fail closed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_DIR = REPO_ROOT / "recovery"
BINARY = RECOVERY_DIR / "build" / "lcsas-restore"

# The fixture builder lives under recovery/tests/.
sys.path.insert(0, str(RECOVERY_DIR / "tests"))


def _require_binary() -> None:
    if not BINARY.exists():
        pytest.skip(
            f"{BINARY} not built; run `lcsas recovery build --arch host` first",
        )


def _build_split_fixture(tmp: Path) -> tuple[Path, Path, Path, Path, dict[str, bytes]]:
    """Build a multi-pack repo and split packs across two simulated discs.

    Returns (repo, pwfile, disc_a, disc_b, files).  Each disc directory
    contains a ``data/`` subdir with some of the repo's pack files.
    The repo's own ``data/`` is empty after the split (so the binary
    cannot find anything without searching the disc dirs).
    """
    import test_e2e  # type: ignore[import-not-found]

    repo = tmp / "repo"
    pwfile = tmp / "pw"
    pwfile.write_text("correct-horse-battery-staple\n")

    files = {
        "alpha.txt": b"alpha " * 1024,
        "beta.bin": os.urandom(8192),
        "gamma.bin": os.urandom(8192),
        "delta.bin": os.urandom(8192),
    }
    test_e2e.build_repo(
        repo, "correct-horse-battery-staple", files,
        v2=False, split_packs=4,
    )

    data = repo / "data"
    packs = sorted(p for p in data.iterdir() if p.is_file())
    assert len(packs) >= 2, f"need >=2 packs after split, got {len(packs)}"

    disc_a = tmp / "disc_a"
    disc_b = tmp / "disc_b"
    (disc_a / "data").mkdir(parents=True)
    (disc_b / "data").mkdir(parents=True)
    half = len(packs) // 2 or 1
    for p in packs[:half]:
        shutil.move(str(p), str(disc_a / "data" / p.name))
    for p in packs[half:]:
        shutil.move(str(p), str(disc_b / "data" / p.name))
    return repo, pwfile, disc_a, disc_b, files


def _verify_restore(target: Path, files: dict[str, bytes]) -> None:
    for name, content in files.items():
        got = (target / name).read_bytes()
        assert got == content, f"{name} mismatch ({len(got)} vs {len(content)})"


# ──────────────────────────────────────────────────────────────────
# Case 1: end-to-end rediscovery via --mount-parent.
#
# `--pack-search` deliberately does NOT include disc_b at startup.
# Instead, a `--mount-parent` is passed that initially contains nothing,
# and disc_b is "inserted" (copied into a child of the parent) only
# AFTER the prompt fires.  A correct implementation re-enumerates the
# parent on retry and discovers disc_b's child path; a regression that
# only re-stats the startup search list cannot recover.
# ──────────────────────────────────────────────────────────────────


def test_tier1_rediscovers_mount_parent_children_on_retry() -> None:
    _require_binary()
    tmp = Path(tempfile.mkdtemp(prefix="lcsas_rescan_"))
    try:
        repo, pwfile, disc_a, disc_b, files = _build_split_fixture(tmp)
        target = tmp / "out"
        target.mkdir()

        # `mount_parent_dir` is initially empty; the binary cannot know
        # about disc_b through any --pack-search argument.
        mount_parent_dir = tmp / "fake-media"
        mount_parent_dir.mkdir()

        proc = subprocess.Popen(
            [
                str(BINARY),
                "--repo", str(repo),
                "--password-file", str(pwfile),
                "--target", str(target),
                "--snapshot", "latest",
                "--pack-search", str(disc_a),
                "--mount-parent", str(mount_parent_dir),
                "--interactive", "on",
                "--verbose",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        prompted = threading.Event()

        def reader() -> None:
            assert proc.stderr is not None
            saw_prompt = False
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                sys.stderr.write(line)
                if "is required for the next file" in line and not saw_prompt:
                    saw_prompt = True
                    # "Insert disc_b" into the parent we told the binary
                    # to scan.  This child path was never in the startup
                    # --pack-search list.
                    shutil.copytree(
                        str(disc_b),
                        str(mount_parent_dir / "LCSAS_DISC_B"),
                    )
                    time.sleep(0.1)
                    try:
                        assert proc.stdin is not None
                        proc.stdin.write("\n")
                        proc.stdin.flush()
                    except (BrokenPipeError, ValueError):
                        pass
                    prompted.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        rc = proc.wait(timeout=120)
        t.join(timeout=5)

        assert prompted.is_set(), "binary never prompted for the missing pack"
        assert rc == 0, f"binary exited {rc}; expected 0 after rediscovery"
        _verify_restore(target, files)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────
# Case 2: catalog refresh on retry surfaces the new disc's label.
#
# We seed a STALE catalog (no record of disc_b's packs) at startup
# and arrange for a FRESHER catalog -- mtime > stale's -- to be on
# disc_b when the user "inserts" it.  Correct rescan picks up the
# fresh catalog and the next prompt iteration surfaces disc_b's
# volume label.
# ──────────────────────────────────────────────────────────────────


def test_tier1_refreshes_catalog_on_retry() -> None:
    _require_binary()
    pytest.importorskip("sqlite3")
    # Production catalog APIs live in src/lcsas; the build doesn't run
    # under installed-mode in CI runners, so make them importable from
    # the worktree.
    src_dir = REPO_ROOT / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    import sqlite3

    from lcsas.db import schema as db_schema
    from lcsas.db.packs import register_pack
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume

    tmp = Path(tempfile.mkdtemp(prefix="lcsas_rescan_cat_"))
    try:
        repo, pwfile, disc_a, disc_b, files = _build_split_fixture(tmp)
        target = tmp / "out"
        target.mkdir()

        a_packs = [(f.name, f.stat().st_size)
                   for f in sorted((disc_a / "data").iterdir())]
        b_packs = [(f.name, f.stat().st_size)
                   for f in sorted((disc_b / "data").iterdir())]

        def _populate_catalog(path: Path, with_b: bool) -> None:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            db_schema.create_all(conn)
            conn.execute(
                "INSERT OR IGNORE INTO repositories "
                "(repo_id, name, mirror_path) VALUES (?, ?, ?)",
                ("repo-rescan", "rescan", "/srv/rescan"),
            )
            a_rows = [register_pack(conn, sha, size, "repo-rescan")
                      for sha, size in a_packs]
            vol_a = create_volume(
                conn, label="vol-RESCAN-A", uuid="uuid-rescan-a",
                media_type="BD25", capacity_bytes=26843545600,
                status="VERIFIED", commit=False,
            )
            bulk_link_packs(
                conn, vol_a.volume_id, [p.pack_id for p in a_rows],
                commit=False,
            )
            if with_b:
                b_rows = [register_pack(conn, sha, size, "repo-rescan")
                          for sha, size in b_packs]
                vol_b = create_volume(
                    conn, label="vol-FRESH-B", uuid="uuid-fresh-b",
                    media_type="BD25", capacity_bytes=26843545600,
                    status="VERIFIED", commit=False,
                )
                bulk_link_packs(
                    conn, vol_b.volume_id, [p.pack_id for p in b_rows],
                    commit=False,
                )
            conn.commit()
            conn.close()

        # Stale catalog at startup: knows only about disc_a.
        stale_catalog = tmp / "catalog-stale.db"
        _populate_catalog(stale_catalog, with_b=False)
        os.utime(str(stale_catalog), (100, 100))

        # Fresh catalog destined for disc_b (will appear under the
        # mount-parent once the user "inserts" it).  Knows BOTH discs.
        fresh_catalog = tmp / "catalog-fresh.db"
        _populate_catalog(fresh_catalog, with_b=True)
        os.utime(str(fresh_catalog), (1_000_000, 1_000_000))

        mount_parent_dir = tmp / "fake-media"
        mount_parent_dir.mkdir()
        # Pre-stage disc_b's catalog.db (and a marker directory) under
        # the mount parent.  Do NOT include disc_b's packs yet -- those
        # arrive AFTER the first prompt fires.  This guarantees:
        #   1. The first prompt fires (packs still missing).
        #   2. By the time the prompt is printed, the locator has
        #      already refresh_discovered() and picked up the fresher
        #      catalog.db -- so the prompt shows vol-FRESH-B.
        #   3. The press-Enter rescan then locates the packs we drop in.
        pre_staged = mount_parent_dir / "LCSAS_DISC_B"
        pre_staged.mkdir()
        shutil.copy(str(fresh_catalog), str(pre_staged / "catalog.db"))
        os.utime(str(pre_staged / "catalog.db"), (1_000_000, 1_000_000))

        proc = subprocess.Popen(
            [
                str(BINARY),
                "--repo", str(repo),
                "--password-file", str(pwfile),
                "--target", str(target),
                "--snapshot", "latest",
                "--pack-search", str(disc_a),
                "--mount-parent", str(mount_parent_dir),
                "--catalog", str(stale_catalog),
                "--interactive", "on",
                "--verbose",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        prompted_once = threading.Event()
        saw_fresh_label = threading.Event()

        def reader() -> None:
            assert proc.stderr is not None
            inserted = False
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                sys.stderr.write(line)
                if "vol-FRESH-B" in line:
                    saw_fresh_label.set()
                if "is required for the next file" in line and not inserted:
                    inserted = True
                    # Drop disc_b's packs into the pre-staged dir so
                    # the press-Enter rescan finds them.
                    src_data = disc_b / "data"
                    dst_data = pre_staged / "data"
                    if not dst_data.exists():
                        shutil.copytree(str(src_data), str(dst_data))
                    time.sleep(0.1)
                    try:
                        assert proc.stdin is not None
                        proc.stdin.write("\n")
                        proc.stdin.flush()
                    except (BrokenPipeError, ValueError):
                        pass
                    prompted_once.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        rc = proc.wait(timeout=120)
        t.join(timeout=5)

        assert prompted_once.is_set(), "binary never prompted for missing pack"
        assert rc == 0, f"binary exited {rc}; expected 0 after rediscovery"
        # The stale catalog has no record of disc_b's packs, so the
        # vol-FRESH-B label can ONLY appear if the locator picked up
        # the fresher catalog from the inserted disc.  This is the
        # tightest assertion we can make about catalog refresh.
        assert saw_fresh_label.is_set(), (
            "prompt did not include 'vol-FRESH-B'; catalog was not refreshed "
            "from the inserted disc's catalog.db"
        )
        _verify_restore(target, files)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────
# Case 2b: catalog refresh must NEVER swap to an OLDER catalog.
#
# Inverse of case 2: when the caller passes a FRESHER `--catalog`,
# the locator must NOT overwrite it with a stale catalog.db found on
# a mounted disc.  Otherwise the prompt's hash->label resolution
# silently regresses just because a disc is mounted.
# ──────────────────────────────────────────────────────────────────


def test_tier1_does_not_replace_caller_catalog_with_older_disc_catalog() -> None:
    _require_binary()
    pytest.importorskip("sqlite3")
    src_dir = REPO_ROOT / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    import sqlite3

    from lcsas.db import schema as db_schema
    from lcsas.db.packs import register_pack
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume

    tmp = Path(tempfile.mkdtemp(prefix="lcsas_rescan_floor_"))
    try:
        repo, pwfile, disc_a, disc_b, files = _build_split_fixture(tmp)
        target = tmp / "out"
        target.mkdir()

        a_packs = [(f.name, f.stat().st_size)
                   for f in sorted((disc_a / "data").iterdir())]
        b_packs = [(f.name, f.stat().st_size)
                   for f in sorted((disc_b / "data").iterdir())]

        def _populate(path: Path, label_b: str) -> None:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            db_schema.create_all(conn)
            conn.execute(
                "INSERT OR IGNORE INTO repositories "
                "(repo_id, name, mirror_path) VALUES (?, ?, ?)",
                ("repo-floor", "floor", "/srv/floor"),
            )
            a_rows = [register_pack(conn, sha, size, "repo-floor")
                      for sha, size in a_packs]
            b_rows = [register_pack(conn, sha, size, "repo-floor")
                      for sha, size in b_packs]
            vol_a = create_volume(
                conn, label="vol-FLOOR-A", uuid="uuid-floor-a",
                media_type="BD25", capacity_bytes=26843545600,
                status="VERIFIED", commit=False,
            )
            vol_b = create_volume(
                conn, label=label_b, uuid="uuid-" + label_b,
                media_type="BD25", capacity_bytes=26843545600,
                status="VERIFIED", commit=False,
            )
            bulk_link_packs(
                conn, vol_a.volume_id, [p.pack_id for p in a_rows],
                commit=False,
            )
            bulk_link_packs(
                conn, vol_b.volume_id, [p.pack_id for p in b_rows],
                commit=False,
            )
            conn.commit()
            conn.close()

        # Caller-provided catalog: newer, has the AUTHORITATIVE label.
        caller_catalog = tmp / "catalog-caller.db"
        _populate(caller_catalog, "vol-AUTH-B")
        os.utime(str(caller_catalog), (5_000_000, 5_000_000))

        # Disc-side catalog: older, has a DIFFERENT label that must NOT win.
        stale_disc_catalog = tmp / "catalog-disc-stale.db"
        _populate(stale_disc_catalog, "vol-STALE-B")
        os.utime(str(stale_disc_catalog), (1_000_000, 1_000_000))

        mount_parent_dir = tmp / "fake-media"
        mount_parent_dir.mkdir()
        pre_staged = mount_parent_dir / "LCSAS_DISC_B"
        pre_staged.mkdir()
        shutil.copy(str(stale_disc_catalog), str(pre_staged / "catalog.db"))
        os.utime(str(pre_staged / "catalog.db"), (1_000_000, 1_000_000))

        proc = subprocess.Popen(
            [
                str(BINARY),
                "--repo", str(repo),
                "--password-file", str(pwfile),
                "--target", str(target),
                "--snapshot", "latest",
                "--pack-search", str(disc_a),
                "--mount-parent", str(mount_parent_dir),
                "--catalog", str(caller_catalog),
                "--interactive", "on",
                "--verbose",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        prompted = threading.Event()
        saw_auth_label = threading.Event()
        saw_stale_label = threading.Event()

        def reader() -> None:
            assert proc.stderr is not None
            inserted = False
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                sys.stderr.write(line)
                if "vol-AUTH-B" in line:
                    saw_auth_label.set()
                if "vol-STALE-B" in line:
                    saw_stale_label.set()
                if "is required for the next file" in line and not inserted:
                    inserted = True
                    src_data = disc_b / "data"
                    dst_data = pre_staged / "data"
                    if not dst_data.exists():
                        shutil.copytree(str(src_data), str(dst_data))
                    time.sleep(0.1)
                    try:
                        assert proc.stdin is not None
                        proc.stdin.write("\n")
                        proc.stdin.flush()
                    except (BrokenPipeError, ValueError):
                        pass
                    prompted.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        rc = proc.wait(timeout=120)
        t.join(timeout=5)

        assert prompted.is_set(), "binary never prompted for missing pack"
        assert rc == 0, f"binary exited {rc}; expected 0 after rediscovery"
        assert saw_auth_label.is_set(), (
            "prompt did not show vol-AUTH-B; caller-provided catalog was "
            "discarded despite being newer"
        )
        assert not saw_stale_label.is_set(), (
            "prompt showed vol-STALE-B; locator regressed to a STALE disc "
            "catalog even though --catalog was fresher"
        )
        _verify_restore(target, files)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────
# Case 3: $LCSAS_MOUNT_DIRS env-var parity with the shell driver.
#
# The driver script accepts LCSAS_MOUNT_DIRS (colon-separated) as the
# override surface for "where to look for new discs."  The C binary
# MUST honour the same env var so the two sides stay in sync.  This
# test asserts the env-var path actually drives the locator without
# any --mount-parent CLI flag.
# ──────────────────────────────────────────────────────────────────


def test_restore_sh_exports_lcsas_mount_dirs_for_binary() -> None:
    """`restore.sh` must export LCSAS_MOUNT_DIRS before exec'ing the
    binary, otherwise the binary's idea of "where to scan for new
    discs" diverges from the shell's.

    Regression case: the variable was set as a shell-local (no
    `export`), so the binary saw $LCSAS_MOUNT_DIRS only when the
    user happened to set it from the outside.  Modern systemd auto-
    mounts under /run/media/$USER/ would then be invisible to the
    binary even though the shell already discovered them.
    """
    restore_sh = RECOVERY_DIR / "scripts" / "restore.sh"
    text = restore_sh.read_text()
    # The export line MUST come after LCSAS_MOUNT_DIRS_EFFECTIVE is
    # computed; assert the expected idiom appears.
    assert "export LCSAS_MOUNT_DIRS=" in text, (
        "restore.sh does not export LCSAS_MOUNT_DIRS; the binary's "
        "mount-parent scan will see a different list than the shell"
    )


def test_tier1_honors_lcsas_mount_dirs_env_var() -> None:
    _require_binary()
    tmp = Path(tempfile.mkdtemp(prefix="lcsas_rescan_env_"))
    try:
        repo, pwfile, disc_a, disc_b, files = _build_split_fixture(tmp)
        target = tmp / "out"
        target.mkdir()

        env_mount_parent = tmp / "env-media"
        env_mount_parent.mkdir()
        other_parent = tmp / "env-other"
        other_parent.mkdir()

        env = os.environ.copy()
        env["LCSAS_MOUNT_DIRS"] = f"{other_parent}:{env_mount_parent}"

        proc = subprocess.Popen(
            [
                str(BINARY),
                "--repo", str(repo),
                "--password-file", str(pwfile),
                "--target", str(target),
                "--snapshot", "latest",
                "--pack-search", str(disc_a),
                # NO --mount-parent; env var must take effect.
                "--interactive", "on",
                "--verbose",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        prompted = threading.Event()
        saw_env_parent_in_count = threading.Event()

        def reader() -> None:
            assert proc.stderr is not None
            saw_prompt = False
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                sys.stderr.write(line)
                # The verbose banner reports the mount-parent count.
                # Two colon-separated entries -> "mount-parents=2".
                if "mount-parents=2" in line:
                    saw_env_parent_in_count.set()
                if "is required for the next file" in line and not saw_prompt:
                    saw_prompt = True
                    shutil.copytree(
                        str(disc_b),
                        str(env_mount_parent / "LCSAS_DISC_B"),
                    )
                    time.sleep(0.1)
                    try:
                        assert proc.stdin is not None
                        proc.stdin.write("\n")
                        proc.stdin.flush()
                    except (BrokenPipeError, ValueError):
                        pass
                    prompted.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        rc = proc.wait(timeout=120)
        t.join(timeout=5)

        assert prompted.is_set(), "binary never prompted for missing pack"
        assert saw_env_parent_in_count.is_set(), (
            "verbose banner did not show mount-parents=2; $LCSAS_MOUNT_DIRS "
            "was not parsed by the binary"
        )
        assert rc == 0, f"binary exited {rc}; expected 0 after rediscovery"
        _verify_restore(target, files)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
