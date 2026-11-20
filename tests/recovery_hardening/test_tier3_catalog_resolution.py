"""Hardening test: tier-3 catalog-aware disc-swap prompt (#248).

Tier-1's ``lcsas-restore`` resolves the missing pack hash to a volume
label via the holographic SQLite catalog burned onto every disc and
prints an ``It is on any disc labelled:`` block (see
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
    ``(catalog has no record of this pack ID)``.

We test by asking the Python reader's disc search (``DiscSearch``, the
search ``PurePythonRestorer`` reads packs through) for a pack that is on
no reachable disc: it prints the framed prompt to stderr, read through
``capsys``, and the reader answers 'q'.  No rustic / xorriso required,
so this runs under ``make test-unit`` as well as the recovery-hardening
suite.

A fourth test exercises the ``--catalog`` flag on the generated
``python_reader.py`` CLI to make sure the plumbing through
``standalone_builder.py`` actually surfaces the flag (cheap argparse
``--help`` probe).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lcsas.restore._disc_search import DiscSearch, RestoreAbortedError
from lcsas.restore.standalone_builder import build_standalone

# A 64-char lowercase hex sha256-ish string used as a synthetic pack
# hash throughout these tests.  Format must match what restic emits
# (and what the prompt slices to 16 chars).
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
            ``(catalog has the pack, but lists no disc for it)``
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


def _make_search(
    tmp_path: Path,
    *,
    catalog_path: Path | None,
) -> DiscSearch:
    """Build the disc search a restorer of a synthetic repository would
    use, with its own ``data/`` as the only search root.

    We never restore -- this is a unit test of the prompt rendering
    only.  The reader answers 'q' at the prompt.
    """
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    return DiscSearch(
        [repo / "data"],
        catalog_path=catalog_path,
        interactive=True,
        repo_data=repo / "data",
        log=lambda _msg: None,
        prompt_in=lambda: "q\n",
    )


def _show_prompt(search: DiscSearch, pack_id: str) -> None:
    """Ask *search* for *pack_id*: the disc prompt goes to stderr, and
    the 'q' answer stops the restore."""
    with pytest.raises(RestoreAbortedError):
        search.read_pack_range(pack_id, 0, 1)


def test_prompt_includes_volume_label_when_catalog_supplied(
    tmp_path: Path, capsys
) -> None:
    """Happy path: with a catalog that maps the pack to a volume, the
    framed prompt prints an ``It is on any disc labelled:`` block in the
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
    search = _make_search(tmp_path, catalog_path=catalog)
    _show_prompt(search, _PACK_HASH_PRESENT)

    captured = capsys.readouterr().err
    assert "It is on any disc labelled:" in captured, (
        "framed prompt is missing the tier-1 'It is on any disc labelled:' line; "
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
    search = _make_search(tmp_path, catalog_path=None)
    _show_prompt(search, _PACK_HASH_PRESENT)

    captured = capsys.readouterr().err
    assert "(tier-3 Python reader: no catalog available)" in captured, (
        f"expected legacy fallback line in framed prompt; stderr was:\n{captured}"
    )
    assert "It is on any disc labelled:" not in captured, (
        f"unexpected catalog-success line in no-catalog prompt; stderr was:\n{captured}"
    )


def test_prompt_reports_unknown_pack_when_catalog_lacks_record(
    tmp_path: Path, capsys
) -> None:
    """Mirrors tier-1: when the catalog is supplied but the pack hash
    isn't in ``packs`` at all (``lcsas_catalog_find_pack`` returns no
    match in ``disc_locator.c:738``), the framed prompt must print
    ``(catalog has no record of this pack ID)``.

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
    search = _make_search(tmp_path, catalog_path=catalog)
    _show_prompt(search, _PACK_HASH_ABSENT)

    captured = capsys.readouterr().err
    assert "(catalog has no record of this pack ID)" in captured, (
        "expected 'catalog has no record of this pack ID' branch in "
        f"framed prompt; stderr was:\n{captured}"
    )
    assert "It is on any disc labelled:" not in captured, (
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
    search = _make_search(tmp_path, catalog_path=catalog)
    _show_prompt(search, _PACK_HASH_PRESENT)

    captured = capsys.readouterr().err
    assert "(tier-3 Python reader: no catalog available)" in captured, (
        "corrupt catalog must degrade to the legacy line; "
        f"stderr was:\n{captured}"
    )


def test_prompt_reports_pack_with_no_volume_mapping(
    tmp_path: Path, capsys
) -> None:
    """A catalog that has the pack hash but no ``volume_packs`` row is
    the third catalog state (the pack is known, no disc is listed):
    no label, but not "no record" either.

    This is the rare-but-real case where a pack was registered in the
    catalog (e.g. mid-burn) but never bound to a volume copy.  The
    framed prompt translates this to
    ``(catalog has the pack, but lists no disc for it)``.
    """
    catalog = tmp_path / "catalog.db"
    _make_catalog(
        catalog,
        pack_hash=_PACK_HASH_PRESENT,
        volume_label=_VOLUME_LABEL,
        map_to_volume=False,
    )
    search = _make_search(tmp_path, catalog_path=catalog)
    _show_prompt(search, _PACK_HASH_PRESENT)

    captured = capsys.readouterr().err
    assert "(catalog has the pack, but lists no disc for it)" in captured, (
        f"expected the no-volume-mapping line; stderr was:\n{captured}"
    )
    # Not one of the other three states.
    assert "It is on any disc labelled:" not in captured
    assert "(catalog has no record of this pack ID)" not in captured
    assert "no catalog available" not in captured


def test_standalone_restorer_exposes_catalog_flag(tmp_path: Path) -> None:
    """The generated ``python_reader.py`` must expose ``--catalog``
    so ``restore.sh`` can plumb the catalog path into tier-3.

    Cheap argparse probe -- builds the script, runs ``--help`` against
    it under the same Python interpreter the test runs under, and
    looks for the flag in the help output.  No real repo or rustic
    dependency.
    """
    script = tmp_path / "python_reader.py"
    script.write_text(build_standalone())
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"python_reader.py --help failed; "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "--catalog" in result.stdout, (
        f"--catalog flag missing from python_reader.py --help; "
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
    # Place a fake python_reader.py next to recovery/ so the
    # tier-3 search finds it.
    (recovery.parent / "python_reader.py").write_text(
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
    seen = pybin_dir / "catalog.seen"
    stub = pybin_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        : > {argv_log}
        prev=
        for a in "$@"; do
            printf '%s\\n' "$a" >> {argv_log}
            [ "$prev" = --catalog ] && cp "$a" {seen}
            prev=$a
        done
        exit 0
    """))
    stub.chmod(0o755)

    target = tmp_path / "restored"
    tools = tmp_path / "tools"
    tools.mkdir()
    env = {
        **os.environ,
        "LCSAS_TOOLS_DIR": str(tools),
        "PATH": f"{pybin_dir}:" + os.environ.get("PATH", ""),
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
    }
    result = subprocess.run(
        ["sh", str(restore_sh), "--restore-folder", str(target), str(recovery)],
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
    # Since rc6 (#610) restore.sh hands every tier its own copy of the
    # catalog, in the tools folder, never a catalog.db in place: the
    # copy must be of the catalog that was reachable.
    assert args[args.index("--catalog") + 1] == str(tools / "catalog.db"), (
        f"--catalog is not the tools folder's copy; argv: {args}"
    )
    assert seen.is_file(), "tier 3 got a --catalog that was not a readable file"
    assert seen.read_bytes() == catalog.read_bytes(), (
        "the tools folder's catalog.db is not a copy of the reachable catalog"
    )
