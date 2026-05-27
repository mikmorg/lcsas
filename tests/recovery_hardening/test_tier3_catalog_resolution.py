"""Hardening test: tier-3 catalog-aware disc-swap prompt (#248).

Tier-1's ``lcsas-restore`` resolves the missing pack hash to a volume
label via the holographic SQLite catalog burned onto every disc and
prints an ``It lives on volume(s):`` block (see
``recovery/src/lcsas-restore/disc_locator.c::print_prompt``).  Until
issue #248 the tier-3 standalone restorer printed only the pack hash
with the legacy ``(no catalog available)`` line, leaving operators (and
the blind-restore test agent's pattern matcher) without the disc-label
hint.

This module pins the three branches the framed prompt must drive when
``--catalog`` is wired through restore.sh:

  * **Happy path** -- catalog contains a ``volume_packs`` row for the
    missing pack: the framed prompt names the volume label in the SAME
    shape tier-1 emits.

  * **No catalog supplied** -- backwards-compat: the legacy
    ``(no catalog available)`` line still appears.

  * **Catalog present but pack absent** -- mirrors tier-1's behaviour
    when ``lcsas_catalog_find_pack`` returns no match
    (``disc_locator.c:739``): the prompt prints
    ``(catalog has no record of this pack hash)``.

We test by driving ``PurePythonRestorer._print_swap_prompt`` directly
through ``capsys`` -- no rustic / xorriso required, so this runs
under ``make test-unit`` as well as the recovery-hardening suite.

A fourth test exercises the ``--catalog`` flag on the generated
``standalone_restorer.py`` CLI to make sure the plumbing through
``standalone_builder.py`` actually surfaces the flag (cheap argparse
``--help`` probe).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

from lcsas.restore.restic_fallback import PurePythonRestorer
from lcsas.restore.standalone_builder import build_standalone

# A 64-char lowercase hex sha256-ish string used as a synthetic pack
# hash throughout these tests.  Format must match what restic emits
# (and what _print_swap_prompt slices to 16 chars).
_PACK_HASH_PRESENT = (
    "ba2288e55901265a0123456789abcdef"
    "0123456789abcdef0123456789abcdef"
)
_PACK_HASH_ABSENT = (
    "deadbeefcafef00d0123456789abcdef"
    "0123456789abcdef0123456789abcdef"
)
_VOLUME_LABEL = "LCSAS_TEST_TINY_2026_0003"


def _make_catalog(
    path: Path,
    *,
    pack_hash: str,
    volume_label: str,
    map_to_volume: bool = True,
) -> None:
    """Synthesise a minimal LCSAS holographic ``catalog.db``.

    Only the columns the tier-3 lookup query touches are populated
    (packs.sha256, packs.pack_id, volumes.label, volumes.volume_id,
    volume_packs).  Schema version is stamped at v5 -- matches what
    burned discs carry today; the lookup query is identical on v6.

    Args:
        path: Where to write the new sqlite3 file.
        pack_hash: Hex sha256 to register in ``packs``.
        volume_label: Label to register in ``volumes``.
        map_to_volume: When True (default), insert a ``volume_packs``
            row linking the pack to the volume.  When False, the pack
            row is present but no volume mapping exists -- pins the
            ``(catalog has the pack, but no current volume mapping)``
            branch in ``disc_locator.c::print_prompt``.
    """
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
        (volume_label, f"uuid-{volume_label}"),
    )
    conn.execute(
        "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
        (pack_hash, 4096, "repo1"),
    )
    if map_to_volume:
        conn.execute(
            "INSERT INTO volume_packs (volume_id, pack_id) VALUES (1, 1)"
        )
    conn.commit()
    conn.close()


def _make_restorer(
    tmp_path: Path,
    *,
    catalog_path: Path | None,
) -> PurePythonRestorer:
    """Build a PurePythonRestorer whose constructor short-circuits to
    the disc-swap prompt path without touching a real restic repo.

    We never call .restore() -- this is a unit test of the prompt
    rendering only.  The repo / password stay synthetic.
    """
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    return PurePythonRestorer(
        repo_path=repo,
        password=b"unused",
        pack_search_paths=[repo / "data"],
        interactive=True,
        catalog_path=catalog_path,
    )


def test_prompt_includes_volume_label_when_catalog_supplied(
    tmp_path: Path, capsys
) -> None:
    """Happy path: with a catalog that maps the pack to a volume, the
    framed prompt prints an ``It lives on volume(s):`` block in the
    same shape tier-1 emits.

    The shape-match is what lets the blind-restore test agent's pattern
    matcher (and habitual operators) treat tier-1 and tier-3 prompts
    identically.
    """
    catalog = tmp_path / "catalog.db"
    _make_catalog(
        catalog,
        pack_hash=_PACK_HASH_PRESENT,
        volume_label=_VOLUME_LABEL,
    )
    restorer = _make_restorer(tmp_path, catalog_path=catalog)
    restorer._print_swap_prompt(_PACK_HASH_PRESENT)

    captured = capsys.readouterr().err
    assert "It lives on volume(s):" in captured, (
        "framed prompt is missing the tier-1 'It lives on volume(s):' line; "
        f"stderr was:\n{captured}"
    )
    assert _VOLUME_LABEL in captured, (
        f"expected volume label {_VOLUME_LABEL!r} in framed prompt; "
        f"stderr was:\n{captured}"
    )
    # Legacy line MUST NOT appear when the catalog answered -- otherwise
    # the agent could see contradictory hints.
    assert "no catalog available" not in captured, (
        "legacy 'no catalog available' line leaked when catalog was "
        f"present; stderr was:\n{captured}"
    )


def test_prompt_falls_back_when_no_catalog_supplied(
    tmp_path: Path, capsys
) -> None:
    """Backwards-compat: with ``catalog_path=None`` the legacy
    ``(no catalog available)`` line still appears.

    Tests / fixtures / older meta-discs that ship without a catalog
    must still get the existing tier-3 prompt -- the catalog hint is
    an enrichment, not a requirement.
    """
    restorer = _make_restorer(tmp_path, catalog_path=None)
    restorer._print_swap_prompt(_PACK_HASH_PRESENT)

    captured = capsys.readouterr().err
    assert "(tier-3 standalone restorer: no catalog available)" in captured, (
        f"expected legacy fallback line in framed prompt; stderr was:\n{captured}"
    )
    assert "It lives on volume(s):" not in captured, (
        f"unexpected catalog-success line in no-catalog prompt; stderr was:\n{captured}"
    )


def test_prompt_reports_unknown_pack_when_catalog_lacks_record(
    tmp_path: Path, capsys
) -> None:
    """Mirrors tier-1: when the catalog is supplied but the pack hash
    isn't in ``packs`` at all (``lcsas_catalog_find_pack`` returns no
    match in ``disc_locator.c:738``), the framed prompt must print
    ``(catalog has no record of this pack hash)``.

    This is the third branch operators and the test agent's pattern
    matchers need to disambiguate ("the catalog can't help you here,
    don't waste time looking") from ("here's the disc to insert").
    """
    catalog = tmp_path / "catalog.db"
    # Populate the catalog with a DIFFERENT pack so the lookup will miss.
    _make_catalog(
        catalog,
        pack_hash=_PACK_HASH_PRESENT,
        volume_label=_VOLUME_LABEL,
    )
    restorer = _make_restorer(tmp_path, catalog_path=catalog)
    restorer._print_swap_prompt(_PACK_HASH_ABSENT)

    captured = capsys.readouterr().err
    assert "(catalog has no record of this pack hash)" in captured, (
        "expected 'catalog has no record of this pack hash' branch in "
        f"framed prompt; stderr was:\n{captured}"
    )
    assert "It lives on volume(s):" not in captured, (
        "unexpected volume-label line for a pack absent from the catalog; "
        f"stderr was:\n{captured}"
    )
    assert "no catalog available" not in captured, (
        "legacy 'no catalog available' line leaked when catalog was "
        f"reachable but didn't have the pack; stderr was:\n{captured}"
    )


def test_prompt_handles_corrupt_catalog_gracefully(
    tmp_path: Path, capsys
) -> None:
    """A non-SQLite (or otherwise unreadable) catalog must NOT crash the
    framed prompt mid-restore.  Restoration must continue, falling back
    to the legacy 'no catalog available' line.

    This pins the try/except around the sqlite3.connect call -- a
    corrupt catalog on a half-mounted disc would otherwise raise
    DatabaseError inside the disc-swap prompt loop and abort the
    restore.
    """
    catalog = tmp_path / "catalog.db"
    catalog.write_bytes(b"not a sqlite database")
    restorer = _make_restorer(tmp_path, catalog_path=catalog)
    restorer._print_swap_prompt(_PACK_HASH_PRESENT)

    captured = capsys.readouterr().err
    assert "(tier-3 standalone restorer: no catalog available)" in captured, (
        "corrupt catalog must degrade to the legacy line; "
        f"stderr was:\n{captured}"
    )


def test_lookup_volume_labels_returns_no_volume_mapping_state(
    tmp_path: Path,
) -> None:
    """Direct probe of ``_lookup_volume_labels``: a catalog that has
    the pack hash but no ``volume_packs`` row must return the
    ``_CATALOG_HIT_NO_VOLS`` sentinel.

    This is the rare-but-real case where a pack was registered in the
    catalog (e.g. mid-burn) but never bound to a volume copy.  The
    framed prompt translates this to
    ``(catalog has the pack, but no current volume mapping)``.
    """
    catalog = tmp_path / "catalog.db"
    _make_catalog(
        catalog,
        pack_hash=_PACK_HASH_PRESENT,
        volume_label=_VOLUME_LABEL,
        map_to_volume=False,
    )
    restorer = _make_restorer(tmp_path, catalog_path=catalog)
    result = restorer._lookup_volume_labels(_PACK_HASH_PRESENT)
    assert result is not None
    labels, status = result
    assert labels == []
    assert status == PurePythonRestorer._CATALOG_HIT_NO_VOLS


def test_standalone_restorer_exposes_catalog_flag(tmp_path: Path) -> None:
    """The generated ``standalone_restorer.py`` must expose ``--catalog``
    so ``restore.sh`` can plumb the catalog path into tier-3.

    Cheap argparse probe -- builds the script, runs ``--help`` against
    it under the same Python interpreter the test runs under, and
    looks for the flag in the help output.  No real repo or rustic
    dependency.
    """
    script = tmp_path / "standalone_restorer.py"
    script.write_text(build_standalone())
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"standalone_restorer.py --help failed; "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "--catalog" in result.stdout, (
        f"--catalog flag missing from standalone_restorer.py --help; "
        f"stdout:\n{result.stdout}"
    )


def test_restore_sh_threads_catalog_arg_to_tier3(tmp_path: Path) -> None:
    """End-to-end plumbing: when a catalog.db is reachable, restore.sh
    must hand the tier-3 invocation a ``--catalog <path>`` flag.

    Mirrors the existing test_tier3_invocation.py pattern -- stubs
    python3 to capture argv, drives restore.sh, asserts on the captured
    args.  Without this test, the restic_fallback / standalone_builder
    changes can ship without restore.sh ever supplying the flag.
    """
    import os

    repo_root = Path(__file__).resolve().parents[2]
    restore_sh = repo_root / "recovery" / "scripts" / "restore.sh"
    host_target = "x86_64-unknown-linux-musl"

    recovery = tmp_path / "recovery"
    recovery.mkdir()
    (recovery / "bin" / host_target).mkdir(parents=True)
    # Minimal restic-shaped repo so restore.sh's preflight passes.
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    # Place a fake standalone_restorer.py next to recovery/ so the
    # tier-3 search finds it.
    (recovery.parent / "standalone_restorer.py").write_text(
        "# placeholder for tier-3\n"
    )
    # Drop a synthetic catalog.db at the location restore.sh's
    # local-recovery-tree probe scans first ("$RECOVERY/catalog.db").
    catalog = recovery / "catalog.db"
    _make_catalog(
        catalog,
        pack_hash=_PACK_HASH_PRESENT,
        volume_label=_VOLUME_LABEL,
    )

    # python3 stub that captures argv and exits 0.
    pybin_dir = tmp_path / "stubbin"
    pybin_dir.mkdir()
    argv_log = pybin_dir / "argv.log"
    stub = pybin_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        : > {argv_log}
        for a in "$@"; do
            printf '%s\\n' "$a" >> {argv_log}
        done
        exit 0
    """))
    stub.chmod(0o755)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "PATH": f"{pybin_dir}:" + os.environ.get("PATH", ""),
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
    }
    result = subprocess.run(
        ["sh", str(restore_sh), str(recovery), str(target), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )
    assert result.returncode == 0, (
        f"restore.sh exited {result.returncode}; stderr:\n{result.stderr}"
    )
    assert argv_log.is_file(), "tier 3 was not reached"
    args = argv_log.read_text().splitlines()
    assert "--catalog" in args, (
        "restore.sh's tier-3 dispatch did not pass --catalog; "
        f"argv: {args}\nstderr:\n{result.stderr}"
    )
    assert args[args.index("--catalog") + 1] == str(catalog), (
        f"--catalog points to the wrong path; argv: {args}"
    )
