"""Hardening test: tier-3 catalog auto-discovery (#253).

Issue #253 surfaced after PR #251 (#248) landed catalog-aware disc-swap
prompts in tier-3.  The fix in #251 only fires when ``restore.sh`` can
locate ``catalog.db`` at script-start time -- but the script's
catalog-pick scan runs ONCE, BEFORE the operator has inserted any data
disc.  The meta-disc deliberately ships no catalog (would be stale at
burn time), so ``$CATALOG_ARG`` stays empty for the entire blind run
and tier-3's prompt falls back to ``(no catalog available)`` even
though every inserted data disc carries the holographic catalog at
``<root>/catalog.db``.

The fix is in ``PurePythonRestorer``: when ``catalog_path is None``, the
disc-swap prompt's catalog lookup walks ``self._pack_search_paths``
looking for a holographic ``catalog.db``.  On first hit the path is
cached for the session; until then, every prompt cycle re-tries
discovery -- giving the operator a chance to mount a disc and have the
NEXT prompt frame pick up the catalog.

This module pins three behaviours of the auto-discovery code path:

  * **Catalog at the disc root** -- a synthetic mount-root containing
    ``catalog.db`` triggers auto-discovery on the first prompt fire,
    surfacing the tier-1 ``It lives on volume(s):`` shape WITHOUT any
    ``--catalog`` flag.

  * **Catalog discoverable via parent traversal** -- when a caller
    passes the disc's ``data/`` subdir as a search path (legacy
    ``--pack-search /mnt/data`` shape), the catalog at
    ``/mnt/catalog.db`` -- one level up -- is still found.

  * **Catalog appears mid-restore** -- a prompt fires BEFORE the disc
    is inserted (no catalog reachable, legacy line printed); a second
    prompt fires AFTER the disc is inserted, and THAT prompt resolves
    the volume label.  This is the variant-blind scenario #253 was
    filed against.

We test via ``capsys`` on ``_print_swap_prompt`` and direct probes of
``_lookup_volume_labels`` -- no rustic / xorriso / cdemu, so this runs
under ``make test-unit``.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

from lcsas.restore.restic_fallback import PurePythonRestorer

# Reuse the synthetic pack hash + volume label conventions from the
# sibling #248 test so audit trails line up across the two fixtures.
_PACK_HASH = (
    "ba2288e55901265a0123456789abcdef"
    "0123456789abcdef0123456789abcdef"
)
_VOLUME_LABEL = "LCSAS_TEST_TINY_2026_0003"


def _make_catalog(path: Path) -> None:
    """Synthesise a minimal LCSAS holographic ``catalog.db`` at *path*.

    Mirrors the schema columns the tier-3 lookup touches: ``packs``,
    ``volumes``, ``volume_packs``.  Stamps schema v5 to match what
    burned discs ship today.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        textwrap.dedent("""
            CREATE TABLE schema_version (
                version INTEGER, applied_at DATETIME
            );
            INSERT INTO schema_version VALUES (5, datetime('now'));
            CREATE TABLE volumes (
                volume_id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT UNIQUE NOT NULL,
                uuid TEXT UNIQUE NOT NULL,
                media_type TEXT NOT NULL,
                capacity_bytes INTEGER NOT NULL,
                used_bytes INTEGER NOT NULL DEFAULT 0,
                location TEXT NOT NULL DEFAULT 'Home_Shelf',
                status TEXT NOT NULL DEFAULT 'STAGING',
                created_at DATETIME,
                closed_at DATETIME,
                verified_at DATETIME
            );
            CREATE TABLE packs (
                pack_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT UNIQUE NOT NULL,
                size_bytes INTEGER NOT NULL,
                repo_id TEXT NOT NULL,
                is_pruned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE volume_packs (
                volume_id INTEGER NOT NULL,
                pack_id INTEGER NOT NULL,
                PRIMARY KEY (volume_id, pack_id)
            );
        """)
    )
    conn.execute(
        "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
        "VALUES (?, ?, 'TEST_TINY', 1024, 'VERIFIED')",
        (_VOLUME_LABEL, f"uuid-{_VOLUME_LABEL}"),
    )
    conn.execute(
        "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
        (_PACK_HASH, 4096, "repo1"),
    )
    conn.execute(
        "INSERT INTO volume_packs (volume_id, pack_id) VALUES (1, 1)"
    )
    conn.commit()
    conn.close()


def _make_restorer(
    tmp_path: Path,
    *,
    pack_search_paths: list[Path],
) -> PurePythonRestorer:
    """Build a PurePythonRestorer with auto-discovery enabled.

    ``catalog_path`` is left unset so the discovery code path fires.
    The repo/password stay synthetic -- we only exercise the
    swap-prompt rendering.
    """
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True, exist_ok=True)
    return PurePythonRestorer(
        repo_path=repo,
        password=b"unused",
        pack_search_paths=pack_search_paths,
        interactive=True,
        catalog_path=None,  # auto-discovery, explicit for clarity
    )


def test_auto_discover_catalog_at_mount_root(tmp_path: Path, capsys) -> None:
    """When ``--catalog`` was NOT supplied but a mount root carries
    ``catalog.db``, the disc-swap prompt must auto-discover it and emit
    the tier-1 ``It lives on volume(s):`` block.

    This is the central #253 scenario: ``restore.sh``'s startup scan
    found no catalog (no data disc yet mounted), so it execs tier-3
    without ``--catalog`` -- but tier-3 still has ``--mount-point``
    pointing at the eventual mount root, and after the operator
    inserts a disc the catalog materialises there.
    """
    mount_root = tmp_path / "mnt"
    mount_root.mkdir()
    _make_catalog(mount_root / "catalog.db")

    restorer = _make_restorer(tmp_path, pack_search_paths=[mount_root])
    restorer._print_swap_prompt(_PACK_HASH)

    stderr = capsys.readouterr().err
    assert "It lives on volume(s):" in stderr, (
        "auto-discovered catalog did not surface the tier-1 disc-label "
        f"block; stderr was:\n{stderr}"
    )
    assert _VOLUME_LABEL in stderr, (
        f"expected volume label {_VOLUME_LABEL!r} in framed prompt after "
        f"auto-discovery; stderr was:\n{stderr}"
    )
    assert "no catalog available" not in stderr, (
        "legacy fallback line leaked when auto-discovery should have "
        f"succeeded; stderr was:\n{stderr}"
    )


def test_auto_discover_catalog_via_parent_traversal(
    tmp_path: Path, capsys
) -> None:
    """Legacy ``--pack-search /mnt/data`` callers pass the ``data/``
    subdir as the search root.  Auto-discovery must still find a
    catalog one level up at ``/mnt/catalog.db``.

    Without parent traversal, the only discs that surface a catalog
    via auto-discovery would be those where the disc root itself was
    passed -- and a fair number of real-world callers / tests pass the
    legacy ``data/`` subdir for historical reasons.
    """
    mount_root = tmp_path / "mnt"
    data_dir = mount_root / "data"
    data_dir.mkdir(parents=True)
    _make_catalog(mount_root / "catalog.db")

    restorer = _make_restorer(tmp_path, pack_search_paths=[data_dir])
    restorer._print_swap_prompt(_PACK_HASH)

    stderr = capsys.readouterr().err
    assert "It lives on volume(s):" in stderr, (
        "parent-traversal discovery failed -- catalog at "
        f"<root>/../catalog.db was not located.  Stderr:\n{stderr}"
    )
    assert _VOLUME_LABEL in stderr


def test_auto_discover_picks_up_catalog_appearing_mid_restore(
    tmp_path: Path, capsys
) -> None:
    """The variant-blind scenario from #253: the FIRST swap prompt
    fires BEFORE any data disc is mounted (no catalog reachable, legacy
    line printed).  A SECOND prompt fires AFTER the operator inserts a
    disc -- and THAT prompt must resolve the volume label via the
    catalog that now lives on the mounted disc.

    This is the only test in the suite that exercises the cache-on-hit
    + retry-on-miss semantic together: prompt 1 returns ``None`` from
    ``_lookup_volume_labels`` (catalog not yet present), prompt 2
    discovers it, caches it, and returns labels.
    """
    mount_root = tmp_path / "mnt"
    mount_root.mkdir()

    restorer = _make_restorer(tmp_path, pack_search_paths=[mount_root])

    # Prompt 1: no catalog yet.  Legacy fallback line expected.
    restorer._print_swap_prompt(_PACK_HASH)
    first = capsys.readouterr().err
    assert "(tier-3 standalone restorer: no catalog available)" in first, (
        "expected legacy fallback line on FIRST prompt (catalog not yet "
        f"reachable); stderr was:\n{first}"
    )
    # Sanity: discovery genuinely failed, so the path stays None.
    assert restorer.catalog_path is None, (
        f"catalog_path mutated despite no catalog present: {restorer.catalog_path}"
    )

    # Operator now "inserts" a data disc carrying the catalog.
    _make_catalog(mount_root / "catalog.db")

    # Prompt 2: discovery must succeed THIS time.
    restorer._print_swap_prompt(_PACK_HASH)
    second = capsys.readouterr().err
    assert "It lives on volume(s):" in second, (
        "auto-discovery did not pick up the newly-mounted catalog on the "
        f"second prompt; stderr was:\n{second}"
    )
    assert _VOLUME_LABEL in second
    # Cached for future prompts.
    assert restorer.catalog_path == mount_root / "catalog.db", (
        f"catalog_path not cached after successful discovery: "
        f"{restorer.catalog_path}"
    )


def test_explicit_catalog_path_skips_auto_discovery(tmp_path: Path) -> None:
    """When ``catalog_path`` IS supplied explicitly, auto-discovery
    must NOT override it -- the override has higher priority and
    callers (including ``restore.sh``'s startup-time pick) rely on
    that.

    Pins the precedence rule: explicit ``--catalog`` always wins, even
    if a discoverable catalog lives in a search path.
    """
    explicit = tmp_path / "explicit-catalog.db"
    _make_catalog(explicit)

    # ALSO put a catalog where auto-discovery would normally find it,
    # to make sure the explicit one is the one queried.
    mount_root = tmp_path / "mnt"
    mount_root.mkdir()
    # Empty file -- if discovery picked it up, the sqlite3.Error branch
    # would hide the failure as None / legacy line.  Use a real catalog
    # so we can assert the EXPLICIT path is the active one.
    _make_catalog(mount_root / "catalog.db")

    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    restorer = PurePythonRestorer(
        repo_path=repo,
        password=b"unused",
        pack_search_paths=[mount_root],
        interactive=True,
        catalog_path=explicit,
    )
    assert restorer.catalog_path == explicit
    # Trigger a lookup -- catalog_path should NOT be reassigned.
    result = restorer._lookup_volume_labels(_PACK_HASH)
    assert result is not None
    labels, _status = result
    assert labels == [_VOLUME_LABEL]
    assert restorer.catalog_path == explicit, (
        f"explicit catalog_path was overwritten by auto-discovery: "
        f"{restorer.catalog_path} (expected {explicit})"
    )


def test_auto_discover_returns_none_when_no_catalog_anywhere(
    tmp_path: Path, capsys
) -> None:
    """Pure negative case: no catalog in any search path → legacy
    fallback line + no exception.

    This is what restore.sh's tier-3 invocation sees when neither the
    operator nor any inserted disc surfaces a catalog (e.g. the
    operator deleted catalog.db, or all data discs are damaged).
    Restoration must continue with the hash-only prompt.
    """
    mount_root = tmp_path / "mnt"
    mount_root.mkdir()
    # No catalog placed anywhere.

    restorer = _make_restorer(tmp_path, pack_search_paths=[mount_root])
    result = restorer._lookup_volume_labels(_PACK_HASH)
    assert result is None
    assert restorer.catalog_path is None

    restorer._print_swap_prompt(_PACK_HASH)
    stderr = capsys.readouterr().err
    assert "(tier-3 standalone restorer: no catalog available)" in stderr


def test_discover_catalog_probes_recovery_subpath(tmp_path: Path) -> None:
    """The meta-disc's local-recovery layout drops catalog.db at
    ``<root>/recovery/catalog.db`` (see restore.sh's ramdir copy at
    line 154-159).  Auto-discovery must probe that path too so a
    bundled-recovery-tree caller without an explicit ``--catalog``
    still gets the volume hint.
    """
    mount_root = tmp_path / "meta"
    (mount_root / "recovery").mkdir(parents=True)
    _make_catalog(mount_root / "recovery" / "catalog.db")

    restorer = _make_restorer(tmp_path, pack_search_paths=[mount_root])
    discovered = restorer._discover_catalog()
    assert discovered == mount_root / "recovery" / "catalog.db", (
        f"recovery-subpath catalog not discovered; got {discovered}"
    )


def test_auto_discovered_corrupt_catalog_is_evicted_for_retry(
    tmp_path: Path,
) -> None:
    """If auto-discovery picks up a corrupt ``catalog.db`` (e.g. a
    truncated file on a half-mounted disc), the cache must be evicted
    so the NEXT prompt cycle can re-scan and possibly find a healthier
    catalog on another mount root.

    Without eviction, the bad path would stick for the rest of the
    session even if a later-inserted disc carries a perfect catalog --
    every subsequent prompt would print the legacy line and waste the
    operator's time.

    This pins the corrupt-cache-eviction contract specifically for
    AUTO-DISCOVERED paths.  An explicit ``--catalog`` override stays
    cached (see ``test_explicit_corrupt_catalog_stays_cached``).
    """
    # Two mount roots: first carries a corrupt catalog, second carries
    # a real one.  The discovery probe walks the search-paths list in
    # order, so the corrupt one gets picked up first.
    bad_root = tmp_path / "bad"
    good_root = tmp_path / "good"
    bad_root.mkdir()
    good_root.mkdir()
    (bad_root / "catalog.db").write_bytes(b"not a sqlite database")
    _make_catalog(good_root / "catalog.db")

    restorer = _make_restorer(
        tmp_path, pack_search_paths=[bad_root, good_root]
    )
    # Prompt 1: discovery finds the corrupt one first, opens it,
    # sqlite3.Error fires, cache evicted, returns None (legacy line).
    result1 = restorer._lookup_volume_labels(_PACK_HASH)
    assert result1 is None, (
        f"expected None on corrupt-catalog hit, got {result1}"
    )
    assert restorer.catalog_path is None, (
        "auto-discovered corrupt catalog was NOT evicted from cache: "
        f"{restorer.catalog_path}"
    )

    # Operator effectively ejects the bad disc; here we just delete the
    # file so the NEXT discovery probe finds the good one.
    (bad_root / "catalog.db").unlink()

    # Prompt 2: discovery picks up the good catalog, returns labels.
    result2 = restorer._lookup_volume_labels(_PACK_HASH)
    assert result2 is not None, (
        "expected catalog hit on retry after evicting corrupt entry; "
        f"catalog_path is {restorer.catalog_path}"
    )
    labels, _status = result2
    assert labels == [_VOLUME_LABEL]
    assert restorer.catalog_path == good_root / "catalog.db"


def test_explicit_corrupt_catalog_stays_cached(tmp_path: Path) -> None:
    """An explicit ``--catalog`` pointing at a corrupt file must NOT
    be auto-evicted -- the operator asked for THAT catalog and a
    silent substitution would surprise them.

    Behaviour pinned: each lookup against the corrupt explicit catalog
    returns None (legacy line) but ``self.catalog_path`` stays set,
    so the second lookup makes the same query against the same path
    (and gets the same None).  No discovery runs.
    """
    explicit = tmp_path / "explicit.db"
    explicit.write_bytes(b"not a sqlite database")

    # Also drop a healthy auto-discoverable catalog -- if eviction
    # fired, discovery would find this and the test would see labels.
    good_root = tmp_path / "good"
    good_root.mkdir()
    _make_catalog(good_root / "catalog.db")

    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    restorer = PurePythonRestorer(
        repo_path=repo,
        password=b"unused",
        pack_search_paths=[good_root],
        interactive=True,
        catalog_path=explicit,
    )
    assert restorer._lookup_volume_labels(_PACK_HASH) is None
    assert restorer.catalog_path == explicit, (
        "explicit catalog was auto-evicted -- operator's --catalog choice "
        f"got silently dropped: {restorer.catalog_path}"
    )
    # Confirm: second lookup uses the same (still corrupt) explicit
    # path, never falls through to discovery.
    assert restorer._lookup_volume_labels(_PACK_HASH) is None
    assert restorer.catalog_path == explicit
