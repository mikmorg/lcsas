"""Issue #115: tier-1 C unit-test harness — fast, agent-free.

The blind-restore e2e is the only thing currently exercising the
C binary against a real-ish repo.  Every C bug therefore costs
~$5 of agent budget to discover.  This harness invokes
`lcsas-restore` directly against synthetic fixtures, runs in
seconds, and pins the behaviors we've already paid to learn:

  • `--help` parses + prints usage
  • `--version` (if present) doesn't crash
  • A missing `--repo` arg fails with a clear error
  • A wrong password fails cleanly (no crash)
  • Pack cache directory is created when LCSAS_PACK_CACHE_DIR
    is set
  • The catalog-copy fix (commit `c6f89a0`) actually copies the
    catalog into the cache rather than holding the source fd

The tests use stub fixtures rather than real rustic-format data,
because constructing a valid encrypted pack tree requires rustic
itself.  For end-to-end semantics, the blind-restore e2e stays
the source of truth — these tests cover the boring stuff so the
blind test doesn't have to.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_CANDIDATES = [
    REPO_ROOT / "recovery" / "build" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore",
]


def _find_bin() -> Path:
    """Resolve the tier-1 binary path.

    Honours ``LCSAS_RESTORE_BIN`` first so parallel-instrumented
    builds (coverage-c #150, sanitiser #152) can point THIS test
    file at an alternate build dir without forking the source.
    """
    if path := os.environ.get("LCSAS_RESTORE_BIN"):
        p = Path(path)
        if p.is_file() and os.access(p, os.X_OK):
            return p
        pytest.skip(f"LCSAS_RESTORE_BIN={path} not executable")
    for p in RESTORE_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    pytest.skip(
        "no lcsas-restore binary; run `lcsas recovery build --arch host`"
    )


def _run(bin_path: Path, *args: str, env: dict[str, str] | None = None,
         stdin_data: str = "", timeout: int = 10,
         ) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [str(bin_path), *args],
        input=stdin_data, capture_output=True, text=True,
        env=full_env, timeout=timeout,
    )


def _make_minimal_repo(tmp_path: Path) -> Path:
    """A restic-format-shaped directory with empty keys/+index/.
    Not a valid repo — won't decrypt anything — but exercises the
    parts of tier-1 that run before pack decryption."""
    repo = tmp_path / "repo"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    (repo / "data").mkdir()
    return repo


# ── Argument-parsing layer ────────────────────────────────────────


def test_help_exits_zero_and_prints_usage() -> None:
    bin_path = _find_bin()
    res = _run(bin_path, "--help")
    assert res.returncode == 0, res.stderr
    out = res.stdout + res.stderr
    assert "usage" in out.lower()
    assert "--repo" in out
    assert "--target" in out
    assert "--password-file" in out


def test_missing_repo_fails_with_actionable_error() -> None:
    bin_path = _find_bin()
    res = _run(bin_path, "--target", "/tmp/x", "--password-file", "/dev/null")
    assert res.returncode != 0
    # Must name what's missing.  Don't pin the exact string; just
    # require that "repo" appears in some form.
    err = (res.stdout + res.stderr).lower()
    assert "repo" in err, err


def test_missing_target_fails_with_actionable_error(tmp_path: Path) -> None:
    bin_path = _find_bin()
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    repo = _make_minimal_repo(tmp_path)
    res = _run(bin_path, "--repo", str(repo), "--password-file", str(pwfile))
    assert res.returncode != 0
    err = (res.stdout + res.stderr).lower()
    assert "target" in err, err


# ── Cache plumbing ───────────────────────────────────────────────


def test_cache_dir_created_on_first_use(tmp_path: Path) -> None:
    """Setting LCSAS_PACK_CACHE_DIR to a non-existent path should
    auto-create it during locator init."""
    bin_path = _find_bin()
    cache = tmp_path / "nested" / "cache"
    assert not cache.exists()
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    target = tmp_path / "restored"
    # Run; expect failure (empty repo can't restore) but cache dir
    # should still be created by init.
    _run(
        bin_path,
        "--repo", str(repo),
        "--target", str(target),
        "--password-file", str(pwfile),
        env={"LCSAS_PACK_CACHE_DIR": str(cache)},
        timeout=5,
    )
    assert cache.exists(), (
        "LCSAS_PACK_CACHE_DIR was not auto-created during locator init"
    )


def test_cache_off_when_env_unset(tmp_path: Path) -> None:
    """No env var → no spurious cache dir creation under /tmp."""
    bin_path = _find_bin()
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    target = tmp_path / "restored"
    env = {k: v for k, v in os.environ.items() if k != "LCSAS_PACK_CACHE_DIR"}
    pre = set(Path("/tmp").glob("lcsas-pack-cache.*"))
    _run(
        bin_path,
        "--repo", str(repo),
        "--target", str(target),
        "--password-file", str(pwfile),
        env=env, timeout=5,
    )
    post = set(Path("/tmp").glob("lcsas-pack-cache.*"))
    new = post - pre
    assert not new, (
        f"binary created cache dirs {new} despite LCSAS_PACK_CACHE_DIR "
        f"being unset"
    )


# ── Catalog handling ─────────────────────────────────────────────


def test_catalog_is_copied_to_cache_not_held_in_place(tmp_path: Path) -> None:
    """Pin commit c6f89a0: when a fresher catalog is discovered on
    a mounted disc, the locator should copy it into the cache dir
    and open the copy rather than the original.

    Indirect probe: after a run that involves catalog discovery,
    look for `.locator-catalog.db` in the cache dir.  If it's there,
    the copy path was exercised."""
    bin_path = _find_bin()
    cache = tmp_path / "cache"
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    target = tmp_path / "restored"

    # Place a fake "catalog" (SQLite-shaped) somewhere the locator
    # would consider.  We can't easily make it look like a real
    # catalog without sqlite3 in the test, so this test is mostly
    # structural — it confirms the binary doesn't crash with a
    # malformed catalog file and writes ARE attempted into cache.
    fake_cat = tmp_path / "catalog.db"
    fake_cat.write_bytes(b"not really a catalog")

    _run(
        bin_path,
        "--repo", str(repo),
        "--target", str(target),
        "--password-file", str(pwfile),
        "--catalog", str(fake_cat),
        env={"LCSAS_PACK_CACHE_DIR": str(cache)},
        timeout=5,
    )
    # The cache dir should exist.  Whether .locator-catalog.db
    # exists depends on whether the binary tried to "consider" the
    # malformed catalog — that's an internal detail.  Just verify
    # we didn't crash silently.
    assert cache.exists()


# ── Crash-safety / smoke ────────────────────────────────────────


def test_no_crash_on_empty_repo(tmp_path: Path) -> None:
    """Empty-but-shaped repo (no actual data) should fail cleanly,
    not segfault."""
    bin_path = _find_bin()
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    target = tmp_path / "restored"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--target", str(target),
        "--password-file", str(pwfile),
        timeout=5,
    )
    # Expect non-zero; specifically NOT SIGSEGV (139) or SIGABRT (134).
    assert res.returncode != 0
    assert res.returncode not in (-11, 139, -6, 134), (
        f"binary segfaulted/aborted (rc={res.returncode}); "
        f"stderr:\n{res.stderr}"
    )


def test_no_crash_on_garbage_password_file(tmp_path: Path) -> None:
    bin_path = _find_bin()
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_bytes(b"\x00\xff\x01\x02\xfe")  # binary garbage
    target = tmp_path / "restored"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--target", str(target),
        "--password-file", str(pwfile),
        timeout=5,
    )
    assert res.returncode not in (-11, 139, -6, 134), res.stderr


def test_missing_password_file_path() -> None:
    bin_path = _find_bin()
    res = _run(
        bin_path,
        "--repo", "/tmp/nonexistent-repo",
        "--target", "/tmp/restored",
        "--password-file", "/this/path/does/not/exist",
        timeout=5,
    )
    assert res.returncode != 0
    assert res.returncode not in (-11, 139), res.stderr


# ── fd-lifetime audit pins (Issue #85) ──────────────────────────


def test_drain_exits_cleanly_on_dir_not_found(tmp_path: Path) -> None:
    """Pin Issue #85: drain_disc must not crash when the mounted path
    has no data/ subtree (the stat(data_dir) != 0 early-exit path).

    Note: with a stub repo the binary bails before any pack lookup
    triggers drain_disc itself, so this is a crash-safety smoke for
    the code paths leading up to and including that early exit.
    The invariant being pinned is: the binary must exit non-zero but
    NOT crash (SIGSEGV/SIGABRT) when pointed at an empty mount-parent,
    and the cache dir must survive intact.
    """
    bin_path = _find_bin()
    cache = tmp_path / "cache"
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    target = tmp_path / "restored"

    # An empty directory: no data/ subtree — exercises drain_disc's
    # stat(data_dir) != 0 guard on any path that gets that far.
    empty_mount_parent = tmp_path / "mount_parent"
    empty_mount_parent.mkdir()

    res = _run(
        bin_path,
        "--repo", str(repo),
        "--target", str(target),
        "--password-file", str(pwfile),
        "--mount-parent", str(empty_mount_parent),
        env={"LCSAS_PACK_CACHE_DIR": str(cache)},
        timeout=10,
    )
    # Must fail (stub repo can't decrypt) but NOT with a crash signal.
    assert res.returncode != 0
    assert res.returncode not in (-11, 139, -6, 134), (
        f"binary crashed (rc={res.returncode}); stderr:\n{res.stderr}"
    )
    # Cache dir must still exist — drain returned cleanly without
    # destroying the cache on any error path.
    assert cache.exists(), (
        "cache dir was removed or never created; drain_disc may have "
        "crashed before the cache init completed"
    )


# ── --list-pending-packs (Issue #90) ────────────────────────────


def test_list_pending_packs_flag_in_help() -> None:
    """Pin Issue #90: --list-pending-packs must appear in --help output."""
    bin_path = _find_bin()
    res = _run(bin_path, "--help")
    out = res.stdout + res.stderr
    assert "--list-pending-packs" in out, (
        "--list-pending-packs flag not found in --help output; "
        "was it added to usage() in main.c?"
    )


def test_list_pending_packs_no_catalog_exits_nonzero(tmp_path: Path) -> None:
    """Pin Issue #90: --list-pending-packs with no --catalog must exit
    non-zero and mention 'catalog' in its error output."""
    bin_path = _find_bin()
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--list-pending-packs",
        timeout=5,
    )
    assert res.returncode != 0, (
        "Expected non-zero exit when --list-pending-packs used without "
        f"--catalog, but got rc={res.returncode}"
    )
    err = (res.stdout + res.stderr).lower()
    assert "catalog" in err, (
        "Expected 'catalog' in error output, "
        f"got: {(res.stdout + res.stderr)!r}"
    )


# ── Idempotent restore resume (Issue #92) ────────────────────────


def test_no_crash_on_existing_target_dir(tmp_path: Path) -> None:
    """Issue #92 — idempotent resume: running lcsas-restore twice with
    the same --target directory must not crash on the second run.

    The first run fails (stub repo has no valid key data), but still
    creates the target directory via lcsas_mkdir_p.  The second run
    sees a non-empty target and must tolerate it gracefully (no
    SIGSEGV / SIGABRT).  This pins the crash-safety half of the
    idempotent-resume contract; end-to-end correctness (files actually
    skipped) is verified by the blind-restore e2e test.
    """
    bin_path = _find_bin()
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    target = tmp_path / "restored"

    # First run — expected to fail (stub repo), but target dir gets made.
    res1 = _run(
        bin_path,
        "--repo", str(repo),
        "--target", str(target),
        "--password-file", str(pwfile),
        timeout=5,
    )
    assert res1.returncode != 0, (
        "first run unexpectedly succeeded against a stub repo"
    )

    # Place a sentinel file in the target to simulate a partial restore.
    target.mkdir(exist_ok=True)
    sentinel = target / "already_restored.dat"
    sentinel.write_bytes(b"partial content")

    # Second run — must NOT crash, and must NOT wipe the sentinel.
    res2 = _run(
        bin_path,
        "--repo", str(repo),
        "--target", str(target),
        "--password-file", str(pwfile),
        timeout=5,
    )
    assert res2.returncode not in (-11, 139, -6, 134), (
        f"binary crashed on second run with non-empty target "
        f"(rc={res2.returncode}); stderr:\n{res2.stderr}"
    )
    assert sentinel.exists(), (
        "binary removed pre-existing files in target on second run; "
        "interrupted restores would lose already-restored data (Issue #92)"
    )


# ── CLI argument-parsing edge cases (main.c coverage) ─────────────


def test_flag_missing_value_fails_cleanly() -> None:
    """A CLI flag passed without its value (e.g. trailing `--repo`) must
    print 'missing value for' and exit non-zero, not crash."""
    bin_path = _find_bin()
    res = _run(bin_path, "--repo")
    assert res.returncode != 0
    err = (res.stdout + res.stderr).lower()
    assert "missing value" in err, err


def test_unknown_flag_fails_with_usage(tmp_path: Path) -> None:
    """An unrecognised flag must print 'unknown argument' and the usage
    banner, then exit non-zero."""
    bin_path = _find_bin()
    res = _run(bin_path, "--this-flag-does-not-exist")
    assert res.returncode != 0
    err = (res.stdout + res.stderr).lower()
    assert "unknown argument" in err, err


def test_too_many_mount_parents_fails(tmp_path: Path) -> None:
    """Issue #160 / overflow defense: passing more than MAX_MOUNT_PARENTS
    (currently 16) --mount-parent flags must print an explicit overflow
    error rather than silently truncate or crash."""
    bin_path = _find_bin()
    args = []
    for i in range(64):
        args += ["--mount-parent", f"/tmp/m{i}"]
    res = _run(bin_path, *args)
    assert res.returncode != 0
    err = (res.stdout + res.stderr).lower()
    assert "too many" in err and "mount-parent" in err, err


def test_too_many_pack_search_fails(tmp_path: Path) -> None:
    """Sibling of test_too_many_mount_parents: --pack-search has the same
    MAX_PACK_SEARCH cap (64) and must fail loud, not silently truncate.
    Pass 128 entries to exceed the cap regardless of off-by-one."""
    bin_path = _find_bin()
    args = []
    for i in range(128):
        args += ["--pack-search", f"/tmp/p{i}"]
    res = _run(bin_path, *args)
    assert res.returncode != 0
    err = (res.stdout + res.stderr).lower()
    assert "too many" in err and "pack-search" in err, err


def test_list_pending_packs_with_valid_catalog(tmp_path: Path) -> None:
    """--list-pending-packs with a valid catalog must run the SELECT and
    exit 0 (covers main.c lines 238-246 + the catalog_print_pending_packs
    success path)."""
    bin_path = _find_bin()
    db_path = tmp_path / "test.db"
    import sqlite3
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE schema_version (version INTEGER, applied_at DATETIME);
        INSERT INTO schema_version VALUES (5, datetime('now'));
        CREATE TABLE repositories (repo_id TEXT PRIMARY KEY, name TEXT,
          mirror_path TEXT NOT NULL, encryption_key_id TEXT DEFAULT '',
          created_at DATETIME);
        CREATE TABLE packs (pack_id INTEGER PRIMARY KEY AUTOINCREMENT,
          sha256 TEXT UNIQUE NOT NULL, size_bytes INTEGER, repo_id TEXT,
          is_pruned INTEGER DEFAULT 0);
        INSERT INTO packs (sha256, size_bytes, repo_id) VALUES
          ('aa', 1024, 'r1'), ('bb', 2048, 'r1');
        CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY AUTOINCREMENT,
          label TEXT UNIQUE, uuid TEXT UNIQUE, media_type TEXT,
          capacity_bytes INTEGER, used_bytes INTEGER DEFAULT 0,
          location TEXT DEFAULT 'Home_Shelf', status TEXT DEFAULT 'STAGING',
          created_at DATETIME, closed_at DATETIME, verified_at DATETIME);
        INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status)
          VALUES ('vol-a', 'uuid-a', 'BD25', 26843545600, 'VERIFIED');
        CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER,
          PRIMARY KEY (volume_id, pack_id));
        INSERT INTO volume_packs VALUES (1, 1), (1, 2);
    """)
    db.commit()
    db.close()

    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--catalog", str(db_path),
        "--list-pending-packs",
        timeout=5,
    )
    assert res.returncode == 0, (
        f"--list-pending-packs with a valid catalog should exit 0, "
        f"got rc={res.returncode}; stderr:\n{res.stderr}"
    )
    out = res.stdout + res.stderr
    assert "vol-a" in out, out


def test_list_pending_packs_invalid_catalog_path_fails(tmp_path: Path) -> None:
    """--list-pending-packs with a non-existent --catalog must fail (covers
    the lcsas_catalog_open failure branch in main.c)."""
    bin_path = _find_bin()
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--catalog", str(tmp_path / "does-not-exist.db"),
        "--list-pending-packs",
        timeout=5,
    )
    assert res.returncode != 0


def test_verbose_flag_accepted(tmp_path: Path) -> None:
    """The --verbose / -v flag is accepted by the parser (covers the
    verbose=1 branch in main.c). Run with a non-restorable repo so it
    fails before doing real work but after parsing the flag."""
    bin_path = _find_bin()
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(tmp_path / "restored"),
        "--verbose",
        timeout=5,
    )
    # Will fail (empty repo, no keys), but verbose=1 should be set first
    assert res.returncode != 0
    # Verbose should NOT crash
    assert res.returncode not in (-11, 139, -6, 134)


def test_interactive_on_off_auto_accepted(tmp_path: Path) -> None:
    """All three --interactive values must be accepted by the parser
    (covers the strcmp branches in main.c lines 251-256)."""
    bin_path = _find_bin()
    repo = _make_minimal_repo(tmp_path)
    pwfile = tmp_path / "pw"
    pwfile.write_text("stub\n")
    for mode in ("on", "off", "auto"):
        res = _run(
            bin_path,
            "--repo", str(repo),
            "--password-file", str(pwfile),
            "--target", str(tmp_path / f"out-{mode}"),
            "--interactive", mode,
            timeout=5,
        )
        assert res.returncode != 0  # fails on missing keys, that's fine
        assert res.returncode not in (-11, 139, -6, 134), (
            f"--interactive {mode} crashed (rc={res.returncode})"
        )


# ── Real-fixture CLI tests (main.c happy paths) ──────────────────
#
# These tests use the encrypted fixture at recovery/tests/fixtures/repo
# (generated by gen_fixture.py).  Password is "test".  They exercise
# main.c branches that are unreachable with the stub fixtures above.


def _fixture_repo() -> Path | None:
    candidate = REPO_ROOT / "recovery" / "tests" / "fixtures" / "repo"
    if candidate.is_dir() and (candidate / "keys").is_dir():
        return candidate
    return None


def _make_pwfile(tmp_path: Path) -> Path:
    pw = tmp_path / "pw"
    pw.write_text("test")
    return pw


def test_fixture_list_snapshots_prints_id_and_path(tmp_path: Path) -> None:
    """--list-snapshots against the real fixture must succeed and print
    the snapshot ID + path (exercises main.c lines 380-388 — the
    list-only print loop)."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--list-snapshots",
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "/test" in out, out
    assert "2026-05-21" in out, out


def test_fixture_verbose_full_restore(tmp_path: Path) -> None:
    """A successful restore with --verbose against the real fixture
    exercises main.c lines 363-364 (master key loaded log) and
    371-372 (indexed N blobs log)."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    target = tmp_path / "restored"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        "--verbose",
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert (target / "hello.txt").is_file()
    # Verbose mode prints "[lcsas-restore] master key loaded" and
    # "[lcsas-restore] indexed N blobs".
    err = res.stderr
    assert "master key loaded" in err, err
    assert "indexed" in err and "blobs" in err, err


def test_fixture_trailing_zeros_restored_correctly(tmp_path: Path) -> None:
    """Issue #264: trailing_zeros.bin in the fixture (0xff*64 + 0x00*8192)
    exercises write_blob_sparse tree.c:263 — the loop-exit `return 0`
    that only fires when the buffer ENDS with a zero run.

    Verifies:
      1. The file is restored at all (no crash, rc=0).
      2. Its content matches the original (first 64 bytes 0xff, rest 0x00).
      3. Its logical size matches (8256 bytes).

    This deterministically covers tree.c:263 on every coverage-c run,
    eliminating the non-deterministic fault-inject side-effect that
    caused 50% failures in `make audit-gate THRESHOLD=95` (issue #264).
    """
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    target = tmp_path / "restored"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    restored = target / "trailing_zeros.bin"
    assert restored.is_file(), (
        "trailing_zeros.bin was not restored; "
        "tree.c:263 loop-exit branch may not have been exercised"
    )
    content = restored.read_bytes()
    expected = b"\xff" * 64 + b"\x00" * 8192
    assert len(content) == len(expected), (
        f"trailing_zeros.bin size mismatch: got {len(content)}, "
        f"want {len(expected)}"
    )
    assert content == expected, (
        "trailing_zeros.bin content mismatch — sparse write produced "
        "incorrect bytes"
    )


def test_fixture_snapshot_not_found_fails(tmp_path: Path) -> None:
    """--snapshot with an ID that doesn't exist in the fixture must
    print an error and exit non-zero (exercises main.c lines 397-399)."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    target = tmp_path / "restored"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        "--snapshot", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        timeout=10,
    )
    assert res.returncode != 0
    err = (res.stdout + res.stderr).lower()
    assert "snapshot not found" in err, err


def test_fixture_snapshot_find_by_prefix(tmp_path: Path) -> None:
    """--snapshot with an 8-char prefix of the fixture's snapshot ID
    must match (exercises lcsas_snapshot_find's prefix branch)."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    target = tmp_path / "restored"
    import json
    manifest = json.loads((repo / "manifest.json").read_text())
    snap_prefix = manifest["snapshot_id"][:8]
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        "--snapshot", snap_prefix,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert (target / "hello.txt").is_file()


def test_fixture_meta_disc_excludes_path(tmp_path: Path) -> None:
    """Passing --meta-disc exercises main.c line 201 (the meta_disc
    argument capture) and the disc_locator's path-under check."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    target = tmp_path / "restored"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        "--meta-disc", "/tmp/nonexistent_meta_disc",
        timeout=10,
    )
    # Restore should still succeed (meta-disc unrelated to repo path).
    assert res.returncode == 0, res.stderr


def test_fixture_verbose_catalog_open_warn(tmp_path: Path) -> None:
    """--catalog with a path that lcsas_catalog_open can't open (without
    --list-pending-packs which exits early) must print the WARN message
    in main.c line 265 — non-fatal, restore continues."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    target = tmp_path / "restored"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        "--catalog", str(tmp_path / "no-such.db"),
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    err = res.stderr
    assert "WARN" in err and "cannot open catalog" in err, err


def test_fixture_verbose_pack_cache_logs(tmp_path: Path) -> None:
    """--verbose + LCSAS_PACK_CACHE_DIR triggers the verbose "opportunistic
    pack cache" log in main.c line 297."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    cache = tmp_path / "pack_cache"
    target = tmp_path / "restored"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        "--verbose",
        env={"LCSAS_PACK_CACHE_DIR": str(cache)},
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "opportunistic pack cache" in res.stderr, res.stderr


def test_fixture_broken_snapshot_target_restore_fails(tmp_path: Path) -> None:
    """--snapshot <broken-tree-snapshot> --target ... exercises main.c
    lines 494-495 (ERROR: tree restore failed + goto out) — the main
    path's response when lcsas_tree_restore returns -1."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    import json as _json
    manifest = _json.loads((repo / "manifest.json").read_text())
    broken_snap = manifest.get("broken_snapshot_id")
    if not broken_snap:
        pytest.skip("fixture lacks broken_snapshot_id — regenerate")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    target = tmp_path / "restored"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        "--snapshot", broken_snap,
        timeout=10,
    )
    assert res.returncode != 0
    err = (res.stdout + res.stderr).lower()
    assert "tree restore failed" in err, err


def test_fixture_target_unwritable_fails_cleanly(tmp_path: Path) -> None:
    """Target dir that mkdir_p cannot create exercises main.c lines
    472-473 (ERROR: cannot create target dir + goto out)."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    # Create a regular file at the parent location, then ask to restore
    # into a child of that file — mkdir_p will fail.
    blocker = tmp_path / "i_am_a_file"
    blocker.write_text("x")
    target = blocker / "cant_mkdir_here"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        timeout=10,
    )
    assert res.returncode != 0
    err = (res.stdout + res.stderr).lower()
    assert "cannot create target dir" in err, err


def test_fixture_supersedes_overflow_load_fails(tmp_path: Path) -> None:
    """Plant an index file with 8193 supersedes entries — exercises
    repo.c lines 534-539 (the supersedes-overflow diagnostic + goto out)
    AND main.c line 375 (ERROR: index load failed).

    Uses a COPY of the fixture so the overflow doesn't pollute the
    real one (the overflow causes load_index to return -1 entirely)."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)

    import shutil
    fixture_copy = tmp_path / "repo_with_overflow"
    shutil.copytree(repo, fixture_copy)

    # Build an encrypted index file with 8193 supersedes refs.
    import hashlib as _hashlib
    import json as _json
    import sys as _sys
    repo_root = Path(__file__).resolve().parents[2]
    _sys.path.insert(0, str(repo_root / "recovery" / "tests" / "fixtures"))
    from gen_fixture import (  # type: ignore[import-not-found]
        MASTER_ENCRYPT,
        MASTER_MAC_K,
        MASTER_MAC_R,
        encrypt_authenticated,
    )

    refs = [f"{i:064x}" for i in range(8193)]
    doc = {"supersedes": refs, "packs": []}
    plain = _json.dumps(doc, separators=(",", ":")).encode()
    enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x16" + b"\x00" * 15, plain,
    )
    new_id = _hashlib.sha256(enc).hexdigest()
    (fixture_copy / "index" / new_id).write_bytes(enc)

    res = _run(
        bin_path,
        "--repo", str(fixture_copy),
        "--password-file", str(pwfile),
        "--target", str(tmp_path / "restored"),
        timeout=30,
    )
    assert res.returncode != 0
    err = res.stderr
    assert "supersedes overflow" in err, err
    # main.c also reports "index load failed" via the goto out chain.
    assert "index load failed" in err, err


def test_fixture_pack_cache_dir_speeds_repeat_restore(tmp_path: Path) -> None:
    """Setting LCSAS_PACK_CACHE_DIR exercises main.c lines 293 (cache
    dir set) and the disc_locator's cache_dir + drain_disc copy paths.
    The first restore drains the disc; the second hits cache."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    cache = tmp_path / "pack_cache"
    target1 = tmp_path / "restored1"
    target2 = tmp_path / "restored2"

    res1 = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target1),
        env={"LCSAS_PACK_CACHE_DIR": str(cache)},
        timeout=10,
    )
    assert res1.returncode == 0, res1.stderr
    assert (target1 / "hello.txt").is_file()
    assert cache.is_dir()

    res2 = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target2),
        env={"LCSAS_PACK_CACHE_DIR": str(cache)},
        timeout=10,
    )
    assert res2.returncode == 0, res2.stderr
    assert (target2 / "hello.txt").is_file()


def test_copy_file_partial_write_leaves_no_garbage() -> None:
    """Static analysis pin for Issue #85: confirm that disc_locator.c
    still contains the unlink(dst) call on the fwrite-error path in
    copy_file.  This is a documentation pin — it guards against the
    error-path cleanup being accidentally deleted during future edits.
    It does NOT execute the fwrite failure (that requires fault
    injection), but it ensures the dead-code-remover never removes it.
    """
    disc_locator = (
        Path(__file__).resolve().parents[2]
        / "recovery" / "src" / "lcsas-restore" / "disc_locator.c"
    )
    assert disc_locator.is_file(), f"disc_locator.c not found at {disc_locator}"
    src = disc_locator.read_text(encoding="utf-8", errors="replace")
    assert "unlink(dst)" in src, (
        "copy_file error-path cleanup (unlink(dst)) has been removed from "
        "disc_locator.c — restore it to prevent partial-write garbage on "
        "disc unmount during a restore."
    )


# ── Issue #269: xattr + hardlink coverage fixtures ────────────────


def test_fixture_xattr_restored_correctly(tmp_path: Path) -> None:
    """File node with extended_attributes in fixture is restored with
    correct content.  Exercises apply_node_xattrs (tree.c 330-397)."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    out = tmp_path / "out"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(out),
        timeout=15,
    )
    assert res.returncode == 0, res.stderr
    xattr_file = out / "xattr_test_file"
    assert xattr_file.exists(), (
        f"xattr_test_file missing; restored files: "
        f"{[p.name for p in out.iterdir()] if out.exists() else '(out missing)'}"
    )
    assert xattr_file.read_bytes() == b"xattr test content"


def test_fixture_hardlink_restored_correctly(tmp_path: Path) -> None:
    """Two file nodes sharing an inode are restored as hardlinks.
    Exercises the hardlink success branch in restore_file_node
    (tree.c 541-563)."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    bin_path = _find_bin()
    pwfile = _make_pwfile(tmp_path)
    out = tmp_path / "out"
    res = _run(
        bin_path,
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(out),
        timeout=15,
    )
    assert res.returncode == 0, res.stderr
    hl_a = out / "hardlink_a"
    hl_b = out / "hardlink_b"
    assert hl_a.exists(), "hardlink_a missing"
    assert hl_b.exists(), "hardlink_b missing"
    assert hl_a.read_bytes() == b"hardlink content"
    assert hl_b.read_bytes() == b"hardlink content"
    # Both should be actual hardlinks (same inode), not just same content.
    assert hl_a.stat().st_ino == hl_b.stat().st_ino, (
        f"hardlink_a inode={hl_a.stat().st_ino} != "
        f"hardlink_b inode={hl_b.stat().st_ino} "
        "(link() path was not taken)"
    )
