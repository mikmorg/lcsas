"""Hardening test: mtime-based locator-catalog cache invalidation in restore.sh (Issue #108).

Catches: restore.sh reusing a stale .locator-catalog.db when the source catalog.db
has advanced, causing the C binary to miss newly-added pack files.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

RESTORE_SH = Path(__file__).resolve().parents[2] / "recovery" / "scripts" / "restore.sh"

# Matches detect_arch.sh emission on Linux x86_64 — the only host arch
# the hardening tests run on.
HOST_TARGET = "x86_64-unknown-linux-musl"


def _make_env(tmp_path: Path, catalog_newer: bool = True):
    """Create a minimal fake recovery tree + pack cache for mtime tests."""
    # fake recovery root with needed dirs
    rec = tmp_path / "rec"
    (rec / "bin" / HOST_TARGET).mkdir(parents=True)
    (rec / "src").mkdir()

    # Single-tenant repo so the script doesn't prompt for a choice.
    repo = rec / "metadata" / "myrepo"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    (repo / "data").mkdir()
    (repo / "snapshots").mkdir()
    (repo / "keys" / "stub_key").write_text("stub")

    # fake catalog in the "recovery root"
    cat = rec / "catalog.db"
    cat.write_text("FAKE")

    # fake pack cache with a locator-catalog.db
    cache = tmp_path / "cache"
    cache.mkdir()
    loc = cache / ".locator-catalog.db"
    loc.write_text("STALE")

    old_time = time.time() - 3600   # 1 hour ago
    new_time = time.time()           # now

    if catalog_newer:
        os.utime(loc, (old_time, old_time))
        os.utime(cat, (new_time, new_time))
    else:
        os.utime(cat, (old_time, old_time))
        os.utime(loc, (new_time, new_time))

    env = os.environ.copy()
    env["LCSAS_PACK_CACHE_DIR"] = str(cache)
    env["LCSAS_ALLOW_NO_PACK_SEARCH"] = "1"
    env["LCSAS_PASSWORD"] = "x"
    env["LCSAS_REPO"] = "myrepo"
    return rec, cache, loc, env


def test_stale_locator_cache_deleted_when_catalog_newer(tmp_path: Path) -> None:
    """When the source catalog is newer than the cached locator-catalog.db,
    restore.sh must delete the stale cache so the binary re-derives it."""
    rec, cache, loc, env = _make_env(tmp_path, catalog_newer=True)

    result = subprocess.run(
        ["sh", str(RESTORE_SH), str(rec), str(tmp_path / "out"), "latest"],
        capture_output=True, text=True, env=env, timeout=15,
    )

    assert not loc.exists(), (
        f".locator-catalog.db should have been deleted when catalog was newer; "
        f"stderr:\n{result.stderr}"
    )
    assert "catalog advanced" in result.stderr, (
        f"stderr must contain 'catalog advanced'; got:\n{result.stderr}"
    )


def test_fresh_locator_cache_not_deleted_when_catalog_older(tmp_path: Path) -> None:
    """When the cached locator-catalog.db is newer than the source catalog,
    restore.sh must leave it intact (no stale-cache eviction needed)."""
    rec, cache, loc, env = _make_env(tmp_path, catalog_newer=False)

    result = subprocess.run(
        ["sh", str(RESTORE_SH), str(rec), str(tmp_path / "out"), "latest"],
        capture_output=True, text=True, env=env, timeout=15,
    )

    assert loc.exists(), (
        f".locator-catalog.db should NOT have been deleted when locator was newer; "
        f"stderr:\n{result.stderr}"
    )
    assert "catalog advanced" not in result.stderr, (
        f"stderr must NOT contain 'catalog advanced' when locator is fresh; "
        f"got:\n{result.stderr}"
    )


def test_no_locator_cache_no_error(tmp_path: Path) -> None:
    """When no .locator-catalog.db exists in the cache dir, the invalidation
    block must be a no-op — no crash, no 'catalog advanced' message."""
    rec, cache, loc, env = _make_env(tmp_path, catalog_newer=True)

    # Remove the locator-catalog.db so there is nothing to invalidate.
    loc.unlink()
    assert not loc.exists()

    result = subprocess.run(
        ["sh", str(RESTORE_SH), str(rec), str(tmp_path / "out"), "latest"],
        capture_output=True, text=True, env=env, timeout=15,
    )

    # The script will fail at binary-exec (no real binary), but the
    # invalidation block must not crash or emit the eviction message.
    assert "catalog advanced" not in result.stderr, (
        f"stderr must NOT contain 'catalog advanced' when no locator cache exists; "
        f"got:\n{result.stderr}"
    )
    # The using-catalog line confirms the block was reached.
    assert "using catalog" in result.stderr, (
        f"script must reach the catalog block before failing; got:\n{result.stderr}"
    )
