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

The fix is in the Python reader's disc search (``DiscSearch``, which
``PurePythonRestorer`` reads packs through): when ``catalog_path is
None``, the disc-swap prompt's catalog lookup walks the search roots
looking for a holographic ``catalog.db``.  On first hit the path is
cached for the session; until then, every prompt cycle re-tries
discovery -- giving the operator a chance to mount a disc and have the
NEXT prompt frame pick up the catalog.

This module pins three behaviours of the auto-discovery code path:

  * **Catalog at the disc root** -- a synthetic mount-root containing
    ``catalog.db`` triggers auto-discovery on the first prompt fire,
    surfacing the tier-1 ``It is on any disc labelled:`` shape WITHOUT any
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

We ask the search for a pack that is on no reachable disc, so the prompt
fires (answered 'q'), and read it via ``capsys``: its catalog lines, and
the "auto-discovered catalog at" line it prints each time it discovers a
catalog -- no rustic / xorriso / cdemu, so this runs under ``make
test-unit``.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from lcsas.restore._disc_search import DiscSearch, RestoreAbortedError

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


def _make_search(
    tmp_path: Path,
    *,
    pack_search_paths: list[Path],
    catalog_path: Path | None = None,
) -> DiscSearch:
    """Build the disc search a restorer would use, with auto-discovery
    enabled unless *catalog_path* is given.

    The repository stays synthetic -- we only exercise the swap-prompt
    rendering.  The reader answers 'q' at the prompt.
    """
    repo_data = tmp_path / "repo" / "data"
    repo_data.mkdir(parents=True, exist_ok=True)
    return DiscSearch(
        pack_search_paths,
        catalog_path=catalog_path,  # None: auto-discovery
        interactive=True,
        repo_data=repo_data,
        log=lambda _msg: None,
        prompt_in=lambda: "q\n",
    )


def _show_prompt(search: DiscSearch, capsys) -> str:
    """Ask *search* for ``_PACK_HASH`` (on no reachable disc): the disc
    prompt fires and the 'q' answer stops it.  Returns its stderr."""
    capsys.readouterr()
    with pytest.raises(RestoreAbortedError):
        search.read_pack_range(_PACK_HASH, 0, 1)
    return capsys.readouterr().err


def _discovered(err: str) -> list[str]:
    """The catalogs a prompt says it auto-discovered.  The line is
    printed only when a catalog is discovered: while one is cached, or
    an explicit ``--catalog`` is in use, there is none."""
    tag = "[lcsas-restore] auto-discovered catalog at "
    return [line[len(tag):] for line in err.splitlines() if line.startswith(tag)]


def test_auto_discover_catalog_at_mount_root(tmp_path: Path, capsys) -> None:
    """When ``--catalog`` was NOT supplied but a mount root carries
    ``catalog.db``, the disc-swap prompt must auto-discover it and emit
    the tier-1 ``It is on any disc labelled:`` block.

    This is the central #253 scenario: ``restore.sh``'s startup scan
    found no catalog (no data disc yet mounted), so it execs tier-3
    without ``--catalog`` -- but tier-3 still has ``--mount-point``
    pointing at the eventual mount root, and after the operator
    inserts a disc the catalog materialises there.
    """
    mount_root = tmp_path / "mnt"
    mount_root.mkdir()
    _make_catalog(mount_root / "catalog.db")

    search = _make_search(tmp_path, pack_search_paths=[mount_root])
    stderr = _show_prompt(search, capsys)

    assert "It is on any disc labelled:" in stderr, (
        "auto-discovered catalog did not surface the tier-1 disc-label "
        f"block; stderr was:\n{stderr}"
    )
    assert _VOLUME_LABEL in stderr, (
        f"expected volume label {_VOLUME_LABEL!r} in framed prompt after "
        f"auto-discovery; stderr was:\n{stderr}"
    )
    assert "no disc catalog yet" not in stderr, (
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

    search = _make_search(tmp_path, pack_search_paths=[data_dir])
    stderr = _show_prompt(search, capsys)

    assert "It is on any disc labelled:" in stderr, (
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
    + retry-on-miss semantic together: prompt 1 finds no catalog (not
    yet present), prompt 2 discovers it and caches it, and prompt 3
    uses the cached one without discovering again.
    """
    mount_root = tmp_path / "mnt"
    mount_root.mkdir()

    search = _make_search(tmp_path, pack_search_paths=[mount_root])

    # Prompt 1: no catalog yet.  Legacy fallback line expected.
    first = _show_prompt(search, capsys)
    assert "(no disc catalog yet: insert your newest data disc)" in first, (
        "expected legacy fallback line on FIRST prompt (catalog not yet "
        f"reachable); stderr was:\n{first}"
    )
    # Sanity: discovery genuinely failed, so nothing was discovered.
    assert _discovered(first) == [], (
        f"a catalog was discovered despite none present: {first}"
    )

    # Operator now "inserts" a data disc carrying the catalog.
    _make_catalog(mount_root / "catalog.db")

    # Prompt 2: discovery must succeed THIS time.
    second = _show_prompt(search, capsys)
    assert "It is on any disc labelled:" in second, (
        "auto-discovery did not pick up the newly-mounted catalog on the "
        f"second prompt; stderr was:\n{second}"
    )
    assert _VOLUME_LABEL in second
    assert _discovered(second) == [str(mount_root / "catalog.db")], second
    # Cached for future prompts: the next one uses it without
    # discovering it again.
    third = _show_prompt(search, capsys)
    assert _VOLUME_LABEL in third
    assert _discovered(third) == [], (
        f"catalog not cached after successful discovery: {third}"
    )


def test_explicit_catalog_path_skips_auto_discovery(
    tmp_path: Path, capsys
) -> None:
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

    search = _make_search(
        tmp_path, pack_search_paths=[mount_root], catalog_path=explicit,
    )
    # Trigger lookups -- the explicit catalog must stay the one in use:
    # nothing is ever auto-discovered in its place.
    for _ in range(2):
        stderr = _show_prompt(search, capsys)
        assert "It is on any disc labelled:" in stderr, stderr
        assert _VOLUME_LABEL in stderr
        assert _discovered(stderr) == [], (
            f"explicit catalog_path was overwritten by auto-discovery: "
            f"{stderr} (expected {explicit})"
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

    search = _make_search(tmp_path, pack_search_paths=[mount_root])
    stderr = _show_prompt(search, capsys)
    assert _discovered(stderr) == []
    assert "(no disc catalog yet: insert your newest data disc)" in stderr


def test_discover_catalog_probes_recovery_subpath(
    tmp_path: Path, capsys
) -> None:
    """The meta-disc's local-recovery layout drops catalog.db at
    ``<root>/recovery/catalog.db`` (see restore.sh's ramdir copy at
    line 154-159).  Auto-discovery must probe that path too so a
    bundled-recovery-tree caller without an explicit ``--catalog``
    still gets the volume hint.
    """
    mount_root = tmp_path / "meta"
    (mount_root / "recovery").mkdir(parents=True)
    _make_catalog(mount_root / "recovery" / "catalog.db")

    search = _make_search(tmp_path, pack_search_paths=[mount_root])
    stderr = _show_prompt(search, capsys)
    discovered = _discovered(stderr)
    assert discovered == [str(mount_root / "recovery" / "catalog.db")], (
        f"recovery-subpath catalog not discovered; got {discovered}"
    )
    assert _VOLUME_LABEL in stderr


def test_auto_discovered_corrupt_catalog_is_evicted_for_retry(
    tmp_path: Path, capsys
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

    search = _make_search(
        tmp_path, pack_search_paths=[bad_root, good_root]
    )
    # Prompt 1: discovery finds the corrupt one first, opens it,
    # sqlite3.Error fires, cache evicted.  Since #635 the lookup also
    # reads every catalog reachable now, so the good one answers at once.
    first = _show_prompt(search, capsys)
    assert _discovered(first) == [str(bad_root / "catalog.db")], first
    assert "It is on any disc labelled:" in first, (
        f"expected the reachable good catalog to answer, got {first}"
    )
    assert _VOLUME_LABEL in first
    # Evicted: the next prompt discovers again (a cached path would be
    # used without discovery), and finds the corrupt one first again.
    again = _show_prompt(search, capsys)
    assert _discovered(again) == [str(bad_root / "catalog.db")], (
        f"auto-discovered corrupt catalog was NOT evicted from cache: {again}"
    )

    # Operator effectively ejects the bad disc; here we just delete the
    # file so the NEXT discovery probe finds the good one.
    (bad_root / "catalog.db").unlink()

    # Prompt 2: discovery picks up the good catalog, returns labels.
    second = _show_prompt(search, capsys)
    assert _discovered(second) == [str(good_root / "catalog.db")], (
        f"expected catalog hit on retry after evicting corrupt entry: {second}"
    )
    assert "It is on any disc labelled:" in second
    assert _VOLUME_LABEL in second
    # ... and keeps it: the next prompt does not discover again.
    assert _discovered(_show_prompt(search, capsys)) == []


def test_explicit_corrupt_catalog_stays_cached(tmp_path: Path, capsys) -> None:
    """An explicit ``--catalog`` pointing at a corrupt file must NOT
    be auto-evicted -- the operator asked for THAT catalog and a
    silent substitution would surprise them.

    Behaviour pinned: the explicit file stays the catalog in use across
    lookups (nothing is auto-discovered in its place).  Since #635 the
    lookup also reads every disc catalog reachable now (it adds labels,
    it does not replace the explicit catalog), so the healthy one still
    names the disc.
    """
    explicit = tmp_path / "explicit.db"
    explicit.write_bytes(b"not a sqlite database")

    # Also drop a healthy auto-discoverable catalog -- if eviction
    # fired, discovery would find this and the prompt would say so.
    good_root = tmp_path / "good"
    good_root.mkdir()
    _make_catalog(good_root / "catalog.db")

    search = _make_search(
        tmp_path, pack_search_paths=[good_root], catalog_path=explicit,
    )
    first = _show_prompt(search, capsys)
    assert "It is on any disc labelled:" in first
    assert _VOLUME_LABEL in first
    assert _discovered(first) == [], (
        "explicit catalog was auto-evicted -- operator's --catalog choice "
        f"got silently dropped: {first}"
    )
    # Second lookup: the explicit path is still the one in use.
    second = _show_prompt(search, capsys)
    assert _VOLUME_LABEL in second
    assert _discovered(second) == []
