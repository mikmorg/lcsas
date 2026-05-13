"""Tests for CLI command handlers: repo add/list/remove, status."""

from __future__ import annotations

from lcsas.cli.main import main


def _get_repo_id_by_name(db_path: str, name: str) -> str:
    """Helper: look up the UUID for a repo registered by ``name``."""
    from lcsas.db.connection import get_connection
    from lcsas.db.repos import list_repos

    conn = get_connection(db_path)
    try:
        for repo in list_repos(conn):
            if repo.name == name:
                return repo.repo_id
    finally:
        conn.close()
    raise AssertionError(f"repo '{name}' not registered")


class TestCmdRepoAdd:
    def test_repo_add_registers(self, tmp_path, capsys):
        """repo add creates DB and registers repo."""
        db = tmp_path / "test.db"
        # First init
        assert main(["init", "--db-path", str(db)]) == 0

        result = main(["--db", str(db), "repo", "add", "family", str(tmp_path / "mirror")])
        assert result == 0
        out = capsys.readouterr().out
        assert "Registered repository 'family'" in out

    def test_repo_add_without_init(self, tmp_path, capsys):
        """repo add auto-initializes DB."""
        db = tmp_path / "new.db"
        result = main(["--db", str(db), "repo", "add", "work", str(tmp_path)])
        assert result == 0
        out = capsys.readouterr().out
        assert "Registered repository 'work'" in out


class TestCmdRepoList:
    def test_repo_list_empty(self, tmp_path, capsys):
        """Empty repo list prints message."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        result = main(["--db", str(db), "repo", "list"])
        assert result == 0
        out = capsys.readouterr().out
        assert "No repositories registered" in out

    def test_repo_list_populated(self, tmp_path, capsys):
        """Lists registered repos with IDs."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "family", "/mnt/mirror/family"])
        main(["--db", str(db), "repo", "add", "work", "/mnt/mirror/work"])
        capsys.readouterr()  # clear

        result = main(["--db", str(db), "repo", "list"])
        assert result == 0
        out = capsys.readouterr().out
        assert "family" in out
        assert "work" in out


class TestCmdRepoRemove:
    def test_repo_remove_happy_path(self, tmp_path, capsys):
        """repo remove deletes the row and logs a success message."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "family", str(tmp_path / "mirror")])
        repo_id = _get_repo_id_by_name(str(db), "family")
        capsys.readouterr()  # clear

        result = main(["--db", str(db), "repo", "remove", repo_id])
        assert result == 0
        out = capsys.readouterr().out
        assert "Removed repository 'family'" in out

        # Row is gone from the catalog.
        from lcsas.db.connection import get_connection
        from lcsas.db.repos import list_repos

        conn = get_connection(str(db))
        try:
            assert [r.name for r in list_repos(conn)] == []
        finally:
            conn.close()

    def test_repo_remove_unknown_repo_errors(self, tmp_path, capsys):
        """repo remove with an unknown id exits non-zero with an error."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        capsys.readouterr()  # clear

        result = main(["--db", str(db), "repo", "remove", "no-such-name"])
        assert result != 0
        out = capsys.readouterr().out
        assert "no-such-name" in out
        assert "not found" in out

    def test_repo_remove_rejects_active_packs_without_force(
        self, tmp_path, capsys
    ):
        """repo remove refuses while active packs sit on active volumes."""
        from lcsas.db.connection import get_connection
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import link_pack_to_volume
        from lcsas.db.volumes import create_volume
        from lcsas.utils.labels import generate_uuid

        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "family", str(tmp_path / "mirror")])
        repo_id = _get_repo_id_by_name(str(db), "family")

        # Add an active pack on a non-deprecated volume.
        conn = get_connection(str(db))
        try:
            vol = create_volume(
                conn,
                label="V1",
                uuid=generate_uuid(),
                media_type="BD25",
                capacity_bytes=25_000_000_000,
                status="BURNED",
            )
            pack = register_pack(
                conn, sha256="active_pack_1", size_bytes=4096, repo_id=repo_id
            )
            link_pack_to_volume(conn, vol.volume_id, pack.pack_id)
        finally:
            conn.close()
        capsys.readouterr()  # clear

        result = main(["--db", str(db), "repo", "remove", repo_id])
        assert result != 0
        out = capsys.readouterr().out
        assert "family" in out
        assert "active volumes" in out
        assert "--force" in out

        # Repo and pack are still in the catalog.
        conn = get_connection(str(db))
        try:
            from lcsas.db.packs import list_packs
            from lcsas.db.repos import list_repos

            assert "family" in [r.name for r in list_repos(conn)]
            assert len(list_packs(conn, repo_id=repo_id, include_pruned=True)) == 1
        finally:
            conn.close()

    def test_repo_remove_with_force_purges_packs(
        self, tmp_path, capsys, monkeypatch
    ):
        """repo remove --force marks packs pruned and deletes the repo."""
        from lcsas.db.connection import get_connection
        from lcsas.db.packs import list_packs, register_pack
        from lcsas.db.repos import list_repos
        from lcsas.db.volume_packs import link_pack_to_volume
        from lcsas.db.volumes import create_volume
        from lcsas.utils.labels import generate_uuid

        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "family", str(tmp_path / "mirror")])
        repo_id = _get_repo_id_by_name(str(db), "family")

        conn = get_connection(str(db))
        try:
            vol = create_volume(
                conn,
                label="V1",
                uuid=generate_uuid(),
                media_type="BD25",
                capacity_bytes=25_000_000_000,
                status="BURNED",
            )
            pack = register_pack(
                conn, sha256="force_pack_1", size_bytes=4096, repo_id=repo_id
            )
            link_pack_to_volume(conn, vol.volume_id, pack.pack_id)
        finally:
            conn.close()

        # --force prompts via input(); auto-confirm.
        monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")
        capsys.readouterr()  # clear

        result = main(["--db", str(db), "repo", "remove", repo_id, "--force"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Removed repository 'family'" in out

        # Repo gone; packs gone; volume_packs links gone.
        conn = get_connection(str(db))
        try:
            assert [r.name for r in list_repos(conn)] == []
            assert list_packs(conn, repo_id=repo_id, include_pruned=True) == []
            row = conn.execute(
                "SELECT COUNT(*) FROM volume_packs WHERE pack_id = ?",
                (pack.pack_id,),
            ).fetchone()
            assert row[0] == 0
        finally:
            conn.close()


class TestCmdStatus:
    def test_status_empty_db(self, tmp_path, capsys):
        """Status on empty DB shows all zeros."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        result = main(["--db", str(db), "status"])
        assert result == 0
        out = capsys.readouterr().out
        assert "0 total" in out
        assert "Volumes: 0" in out

    def test_status_with_data(self, tmp_path, capsys):
        """Status with repos shows counts."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "fam", "/mnt/fam"])
        capsys.readouterr()

        result = main(["--db", str(db), "status"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Packs:" in out


class TestCmdDispatchEdges:
    def test_burn_requires_session(self, capsys):
        """`lcsas burn` without `--session` fails fast via argparse.

        Regression test for #60: the legacy stage+burn handler was
        removed, so `--session` is now required and argparse should
        reject the bare `burn` invocation with a non-zero exit code and
        a message referencing `--session`.
        """
        import pytest

        with pytest.raises(SystemExit) as excinfo:
            main(["burn"])
        assert excinfo.value.code != 0
        err = capsys.readouterr().err
        assert "--session" in err

    def test_verify_not_implemented(self, capsys):
        result = main(["verify", "SOME_LABEL"])
        assert result == 1

    def test_status_auto_creates_db_at_unknown_path(self, tmp_path, capsys):
        """`status` against a fresh DB path auto-creates the file and schema.

        Regression test for the auto-init path: ``cmd_status`` defensively
        calls ``create_all()`` and ``get_connection`` creates missing parent
        directories, so an unused path should succeed (not error out).
        """
        import sqlite3

        db = tmp_path / "fresh-subdir" / "archive.db"
        assert not db.exists()
        assert not db.parent.exists()

        result = main(["--db", str(db), "status"])
        assert result == 0
        assert db.exists(), "status should have created the DB file"

        # Verify the schema was applied (a few core tables should exist).
        conn = sqlite3.connect(str(db))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        for expected in ("repositories", "packs", "volumes"):
            assert expected in tables, f"expected table '{expected}' in {tables}"


# ---------------------------------------------------------------------------
# cmd_catalog_validate
# ---------------------------------------------------------------------------


def _build_fake_disc(
    disc_path,
    catalog_hashes,
    disc_file_hashes,
    *,
    volume_label="VOL_001",
    repo_name="family",
):
    """Build a minimal fake mounted-disc directory at *disc_path*.

    Writes a holographic catalog (``catalog.db``) that registers *repo_name*
    and one volume whose ``volume_packs`` rows reference every SHA in
    *catalog_hashes*, plus ``data/<hash>`` files for every SHA in
    *disc_file_hashes*.  The two sets need not match — that mismatch is
    exactly what ``cmd_catalog_validate`` is meant to surface.
    """
    import sqlite3

    from lcsas.db.packs import register_pack
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume
    from lcsas.utils.labels import generate_uuid

    disc_path.mkdir(parents=True, exist_ok=True)
    catalog_db = disc_path / "catalog.db"

    conn = sqlite3.connect(str(catalog_db))
    conn.row_factory = sqlite3.Row
    try:
        create_all(conn)
        register_repo(conn, repo_name, repo_name, f"/mnt/mirror/{repo_name}", "")
        if catalog_hashes:
            packs = [register_pack(conn, sha, 1024, repo_name) for sha in catalog_hashes]
            vol = create_volume(
                conn,
                volume_label,
                generate_uuid(),
                "TEST_TINY",
                1_000_000,
                "Home_Shelf",
                "VERIFIED",
            )
            bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in packs])
        conn.commit()
    finally:
        conn.close()

    data_dir = disc_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for sha in disc_file_hashes:
        (data_dir / sha).write_bytes(b"x")


class TestCmdCatalogValidate:
    def test_catalog_validate_happy_path(self, tmp_path, capsys):
        """Disc with data files matching the embedded catalog exits 0."""
        disc = tmp_path / "disc"
        hashes = ["aa" * 32, "bb" * 32]
        _build_fake_disc(disc, catalog_hashes=hashes, disc_file_hashes=hashes)

        result = main(["catalog", "validate", str(disc)])
        assert result == 0
        out = capsys.readouterr().out
        assert "Catalog validation PASSED" in out
        assert "Catalog packs  : 2" in out
        assert "Disc packs     : 2" in out

    def test_catalog_validate_orphan_packs(self, tmp_path, capsys):
        """Disc with pack files NOT in the catalog is reported as orphaned."""
        disc = tmp_path / "disc"
        catalog_hashes = ["aa" * 32]
        # Disc has an extra file the catalog doesn't reference.
        disc_file_hashes = ["aa" * 32, "cc" * 32]
        _build_fake_disc(
            disc,
            catalog_hashes=catalog_hashes,
            disc_file_hashes=disc_file_hashes,
        )

        result = main(["catalog", "validate", str(disc)])
        assert result != 0
        out = capsys.readouterr().out
        assert "ORPHAN" in out
        assert "cc" * 32 in out
        assert "Catalog validation FAILED" in out

    def test_catalog_validate_missing_packs(self, tmp_path, capsys):
        """Catalog references packs that are absent on disc -> reported missing."""
        disc = tmp_path / "disc"
        catalog_hashes = ["aa" * 32, "bb" * 32]
        # Only one of the two cataloged packs is on the disc.
        disc_file_hashes = ["aa" * 32]
        _build_fake_disc(
            disc,
            catalog_hashes=catalog_hashes,
            disc_file_hashes=disc_file_hashes,
        )

        result = main(["catalog", "validate", str(disc)])
        assert result != 0
        out = capsys.readouterr().out
        assert "MISSING" in out
        assert "bb" * 32 in out
        assert "Catalog validation FAILED" in out

    def test_catalog_validate_unknown_path_errors(self, tmp_path, capsys):
        """`catalog validate /nonexistent` exits non-zero with a clear error."""
        missing = tmp_path / "no-such-disc"
        assert not missing.exists()

        result = main(["catalog", "validate", str(missing)])
        assert result != 0
        out = capsys.readouterr().out
        assert "does not exist" in out or "not a directory" in out
        assert str(missing) in out


# ---------------------------------------------------------------------------
# cmd_catalog_rebuild
# ---------------------------------------------------------------------------


def _build_fake_disc_for_rebuild(
    disc_path,
    *,
    repo_id,
    repo_name,
    volume_label,
    volume_uuid,
    pack_hashes,
):
    """Build a minimal fake mounted-disc directory holding a holographic catalog.

    Writes ``<disc_path>/catalog.db`` containing a single repository, a single
    volume, and a row for each SHA in *pack_hashes* linked to that volume.
    No on-disc ``data/`` files are required for catalog rebuild — rebuild
    only consults ``catalog.db``.
    """
    import sqlite3

    from lcsas.db.packs import register_pack
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume

    disc_path.mkdir(parents=True, exist_ok=True)
    catalog_db = disc_path / "catalog.db"

    conn = sqlite3.connect(str(catalog_db))
    conn.row_factory = sqlite3.Row
    try:
        create_all(conn)
        register_repo(conn, repo_id, repo_name, f"/mnt/mirror/{repo_name}", "")
        packs = [register_pack(conn, sha, 1024, repo_id) for sha in pack_hashes]
        vol = create_volume(
            conn,
            volume_label,
            volume_uuid,
            "TEST_TINY",
            1_000_000,
            "Home_Shelf",
            "VERIFIED",
        )
        bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in packs])
        conn.commit()
    finally:
        conn.close()


def _master_catalog_counts(db_path):
    """Return a dict of row counts for the key catalog tables."""
    from lcsas.db.connection import get_connection

    conn = get_connection(db_path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "repositories",
                "volumes",
                "packs",
                "volume_packs",
            )
        }
    finally:
        conn.close()


class TestCmdCatalogRebuild:
    def test_catalog_rebuild_happy_path(self, tmp_path, capsys):
        """rebuild from two disc dirs produces a master catalog with the union."""
        disc1 = tmp_path / "disc1"
        disc2 = tmp_path / "disc2"

        _build_fake_disc_for_rebuild(
            disc1,
            repo_id="family",
            repo_name="family",
            volume_label="VOL_001",
            volume_uuid="uuid-volume-1",
            pack_hashes=["aa" * 32, "bb" * 32],
        )
        _build_fake_disc_for_rebuild(
            disc2,
            repo_id="work",
            repo_name="work",
            volume_label="VOL_002",
            volume_uuid="uuid-volume-2",
            pack_hashes=["cc" * 32, "dd" * 32],
        )

        out_db = tmp_path / "master.db"
        result = main([
            "catalog", "rebuild",
            "--output", str(out_db),
            str(disc1), str(disc2),
        ])
        assert result == 0

        out = capsys.readouterr().out
        assert "Catalog rebuild complete" in out
        assert str(out_db) in out
        assert "Discs processed  : 2" in out

        counts = _master_catalog_counts(str(out_db))
        assert counts["repositories"] == 2  # family + work
        assert counts["volumes"] == 2       # VOL_001 + VOL_002
        assert counts["packs"] == 4         # 2 + 2 distinct shas
        assert counts["volume_packs"] == 4  # 2 links per volume

    def test_catalog_rebuild_idempotent(self, tmp_path, capsys):
        """Running rebuild twice over the same discs is a no-op on the 2nd pass."""
        disc1 = tmp_path / "disc1"
        disc2 = tmp_path / "disc2"
        _build_fake_disc_for_rebuild(
            disc1,
            repo_id="family",
            repo_name="family",
            volume_label="VOL_001",
            volume_uuid="uuid-volume-1",
            pack_hashes=["aa" * 32, "bb" * 32],
        )
        _build_fake_disc_for_rebuild(
            disc2,
            repo_id="work",
            repo_name="work",
            volume_label="VOL_002",
            volume_uuid="uuid-volume-2",
            pack_hashes=["cc" * 32, "dd" * 32],
        )
        out_db = tmp_path / "master.db"

        # First pass
        first = main([
            "catalog", "rebuild",
            "--output", str(out_db),
            str(disc1), str(disc2),
        ])
        assert first == 0
        first_counts = _master_catalog_counts(str(out_db))
        capsys.readouterr()  # clear

        # Second pass over the same inputs — natural-key conflicts hit
        # INSERT OR IGNORE, so the master catalog should be unchanged.
        second = main([
            "catalog", "rebuild",
            "--output", str(out_db),
            str(disc1), str(disc2),
        ])
        assert second == 0
        second_counts = _master_catalog_counts(str(out_db))

        assert first_counts == second_counts
        # Counts come from the happy-path expectation.
        assert second_counts == {
            "repositories": 2,
            "volumes": 2,
            "packs": 4,
            "volume_packs": 4,
        }

        out = capsys.readouterr().out
        # On the second pass nothing new should be merged.
        assert "Repositories     : 0 new" in out
        assert "Volumes          : 0 new" in out
        assert "Packs            : 0 new" in out

    def test_catalog_rebuild_unknown_dir_errors(self, tmp_path, capsys):
        """A non-existent disc dir trips the sanity check and exits non-zero."""
        missing = tmp_path / "no-such-disc"
        assert not missing.exists()
        out_db = tmp_path / "master.db"

        result = main([
            "catalog", "rebuild",
            "--output", str(out_db),
            str(missing),
        ])
        assert result != 0

        out = capsys.readouterr().out
        assert "Not a directory" in out
        assert str(missing) in out
        # The handler bails BEFORE creating the master DB.
        assert not out_db.exists()

    def test_catalog_rebuild_partial_failure(self, tmp_path, capsys):
        """One valid disc + one disc missing catalog.db -> non-zero, but the
        valid disc is still merged into the master catalog."""
        good = tmp_path / "good_disc"
        bad = tmp_path / "bad_disc"

        _build_fake_disc_for_rebuild(
            good,
            repo_id="family",
            repo_name="family",
            volume_label="VOL_001",
            volume_uuid="uuid-volume-1",
            pack_hashes=["aa" * 32, "bb" * 32],
        )
        # `bad` exists as a directory (so it survives the is_dir() sanity
        # check) but contains no catalog.db — rebuild_catalog records an
        # error for it and continues.
        bad.mkdir()

        out_db = tmp_path / "master.db"
        result = main([
            "catalog", "rebuild",
            "--output", str(out_db),
            str(good), str(bad),
        ])
        # rebuild_catalog appends to result.errors -> handler returns 1.
        assert result != 0

        out = capsys.readouterr().out
        # The good disc was still processed.
        assert "Discs processed  : 1" in out
        assert "Discs skipped    : 1" in out
        # The error mentioning the bad disc is reported.
        assert "No catalog.db" in out
        assert str(bad) in out

        # The good disc's contents made it into the master catalog.
        counts = _master_catalog_counts(str(out_db))
        assert counts["repositories"] == 1
        assert counts["volumes"] == 1
        assert counts["packs"] == 2
        assert counts["volume_packs"] == 2


# ---------------------------------------------------------------------------
# cmd_recovery
# ---------------------------------------------------------------------------


class _FakeRecoveryBuilder:
    """Stand-in for :class:`lcsas.recovery.RecoveryBuilder`.

    Records each method invocation so tests can assert which dispatch
    branch ``cmd_recovery`` took, without actually invoking ``make`` or
    a C compiler.
    """

    # Class-level call log so the inner ``from lcsas.recovery import
    # RecoveryBuilder`` resolves to the same patched object across the
    # test and ``cmd_recovery``.
    calls: list[tuple[str, tuple, dict]] = []

    def __init__(self, recovery_dir):
        self.recovery_dir = recovery_dir
        type(self).calls.append(("__init__", (recovery_dir,), {}))

    @classmethod
    def reset(cls):
        cls.calls = []

    def build_host(self, verbose: bool = False):
        from lcsas.recovery.build import RecoveryArtifacts

        type(self).calls.append(("build_host", (), {"verbose": verbose}))
        return RecoveryArtifacts(
            arch="x86_64",
            lcsas_restore=self.recovery_dir / "build" / "lcsas-restore",
            lcsas_iso9660=None,
            lcsas_init=None,
        )

    def cross_build(self, arch: str, cc=None, verbose: bool = False):
        from lcsas.recovery.build import RecoveryArtifacts

        type(self).calls.append(
            ("cross_build", (arch,), {"cc": cc, "verbose": verbose})
        )
        return RecoveryArtifacts(
            arch=arch,
            lcsas_restore=self.recovery_dir / "bin" / arch / "lcsas-restore",
            lcsas_iso9660=None,
            lcsas_init=None,
        )

    def run_tests(self, verbose: bool = False) -> bool:
        type(self).calls.append(("run_tests", (), {"verbose": verbose}))
        return True

    def write_manifest(self, manifest_path=None):
        from pathlib import Path

        type(self).calls.append(
            ("write_manifest", (), {"manifest_path": manifest_path})
        )
        # Write a tiny non-empty manifest so the handler can count lines.
        target = (
            Path(manifest_path)
            if manifest_path is not None
            else self.recovery_dir / "MANIFEST.sha256"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("deadbeef  README\n")
        return target


def _patch_recovery_builder(monkeypatch):
    """Install ``_FakeRecoveryBuilder`` in place of the real builder.

    ``cmd_recovery`` does ``from lcsas.recovery import RecoveryBuilder``
    inside the function body, so we need to patch the attribute on the
    ``lcsas.recovery`` package itself (not on ``lcsas.cli.main``).
    """
    import lcsas.recovery as recovery_pkg

    _FakeRecoveryBuilder.reset()
    monkeypatch.setattr(recovery_pkg, "RecoveryBuilder", _FakeRecoveryBuilder)
    return _FakeRecoveryBuilder


class TestCmdRecovery:
    def test_recovery_build_invokes_builder(self, monkeypatch, capsys):
        """`recovery build --arch x86_64` dispatches to ``cross_build``.

        (``--arch host`` would route to ``build_host`` instead; the
        issue's illustrative ``build_target`` name maps onto the real
        builder's ``cross_build`` for non-host architectures.)
        """
        fake = _patch_recovery_builder(monkeypatch)

        result = main(["recovery", "build", "--arch", "x86_64"])
        assert result == 0

        method_calls = [c for c in fake.calls if c[0] != "__init__"]
        assert len(method_calls) == 1
        name, args, kwargs = method_calls[0]
        assert name == "cross_build"
        assert args == ("x86_64",)
        assert kwargs == {"cc": None, "verbose": False}

    def test_recovery_build_host_invokes_build_host(self, monkeypatch, capsys):
        """`recovery build` (default ``--arch host``) calls ``build_host``."""
        fake = _patch_recovery_builder(monkeypatch)

        result = main(["recovery", "build"])
        assert result == 0

        method_calls = [c for c in fake.calls if c[0] != "__init__"]
        assert len(method_calls) == 1
        name, _args, kwargs = method_calls[0]
        assert name == "build_host"
        assert kwargs == {"verbose": False}

    def test_recovery_test_invokes_test(self, monkeypatch, capsys):
        """`recovery test` calls ``RecoveryBuilder.run_tests``."""
        fake = _patch_recovery_builder(monkeypatch)

        result = main(["recovery", "test"])
        assert result == 0

        method_calls = [c for c in fake.calls if c[0] != "__init__"]
        assert len(method_calls) == 1
        name, _args, kwargs = method_calls[0]
        assert name == "run_tests"
        assert kwargs == {"verbose": False}

        out = capsys.readouterr().out
        assert "recovery tests: OK" in out

    def test_recovery_manifest_invokes_manifest(
        self, monkeypatch, tmp_path, capsys
    ):
        """`recovery manifest` calls ``RecoveryBuilder.write_manifest``."""
        fake = _patch_recovery_builder(monkeypatch)

        out_path = tmp_path / "MANIFEST.sha256"
        result = main(["recovery", "manifest", "-o", str(out_path)])
        assert result == 0

        method_calls = [c for c in fake.calls if c[0] != "__init__"]
        assert len(method_calls) == 1
        name, _args, kwargs = method_calls[0]
        assert name == "write_manifest"
        # cmd_recovery passes the argparse ``Path`` value through.
        assert kwargs["manifest_path"] == out_path
        assert out_path.is_file()

    def test_recovery_verify_invokes_verify(self, monkeypatch, capsys):
        """`recovery verify` shells out to ``make ... repro-check``.

        The dispatcher invokes ``subprocess.run(["make", "-C", <dir>,
        "repro-check"])`` directly (rather than calling a builder
        method), so we stub ``subprocess.run`` at the cli.main level to
        avoid spawning make/cc and assert the right command was issued.
        """
        _patch_recovery_builder(monkeypatch)

        recorded: dict = {}

        class _CompletedStub:
            returncode = 0

        def _fake_run(cmd, *args, **kwargs):
            recorded["cmd"] = list(cmd)
            recorded["args"] = args
            recorded["kwargs"] = kwargs
            return _CompletedStub()

        monkeypatch.setattr("lcsas.cli.main.subprocess.run", _fake_run)

        result = main(["recovery", "verify"])
        assert result == 0
        assert recorded["cmd"][:2] == ["make", "-C"]
        assert recorded["cmd"][-1] == "repro-check"

    def test_recovery_missing_tree_errors(
        self, monkeypatch, tmp_path, caplog
    ):
        """If ``recovery/`` is absent, the handler exits non-zero with an
        error.

        ``cmd_recovery`` derives the recovery tree from
        ``Path(__file__).resolve().parents[3]`` of the ``cli.main``
        module; patching ``__file__`` to point at a tmp tree without a
        ``recovery/`` subdir exercises the error branch without touching
        the real repo layout.
        """
        import logging

        import lcsas.cli.main as cli_main

        fake_root = tmp_path / "fake_project"
        fake_module_path = fake_root / "src" / "lcsas" / "cli" / "main.py"
        fake_module_path.parent.mkdir(parents=True)
        fake_module_path.write_text("# stub\n")
        # No recovery/ directory under fake_root.

        monkeypatch.setattr(cli_main, "__file__", str(fake_module_path))
        # Also patch RecoveryBuilder so we know it was *not* constructed.
        fake = _patch_recovery_builder(monkeypatch)

        with caplog.at_level(logging.ERROR, logger="lcsas"):
            result = main(["recovery", "build"])

        assert result == 1
        # We never got far enough to construct the builder.
        assert fake.calls == []
        # Handler logged a clear error referencing the missing tree.
        combined = " ".join(rec.getMessage() for rec in caplog.records)
        assert "recovery/" in combined or "recovery" in combined
        assert str(fake_root / "recovery") in combined

    def test_recovery_unknown_subcommand_errors(self, monkeypatch, capsys):
        """argparse rejects unknown ``recovery`` subcommands cleanly."""
        import pytest

        _patch_recovery_builder(monkeypatch)

        with pytest.raises(SystemExit) as excinfo:
            main(["recovery", "frobnicate"])
        assert excinfo.value.code != 0
        err = capsys.readouterr().err
        assert "frobnicate" in err or "invalid choice" in err


def _write_session_config(tmp_path) -> tuple[str, str]:
    """Create a minimal TOML config + initialized DB and return (cfg, db) paths."""
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    db = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(f"""
[paths]
mirror_base = "{mirror}"
staging = "{staging}"
database = "{db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
""")
    # Initialize the catalog DB so list_sessions has tables to query.
    main(["init", "--db-path", str(db)])
    return str(cfg), str(db)


class TestCmdSessionList:
    def test_session_list_empty_db(self, tmp_path, capsys):
        """Fresh DB with no sessions: handler returns 0 and prints a friendly
        'no sessions' message."""
        cfg, _db = _write_session_config(tmp_path)
        capsys.readouterr()  # clear init output

        result = main(["--config", cfg, "session", "list"])
        assert result == 0
        out = capsys.readouterr().out
        assert "No sessions found" in out

    def test_session_list_shows_sessions(self, tmp_path, capsys):
        """Two sessions in different statuses both appear with their IDs and statuses."""
        from lcsas.db.connection import get_connection
        from lcsas.db.sessions import create_session, update_session_status

        cfg, db = _write_session_config(tmp_path)
        conn = get_connection(db)
        try:
            s1 = create_session(conn, media_type="TEST_TINY",
                                staging_dir="/tmp/s1", session_id="sess-staged-001")
            s2 = create_session(conn, media_type="TEST_TINY",
                                staging_dir="/tmp/s2", session_id="sess-complete-001")
            # s1 stays at the default STAGED status; advance s2 to COMPLETE
            # (schema CHECK constrains status to STAGED/PARTIAL/COMPLETE/CLEANED,
            # so 'COMPLETE' is the closest valid analogue of the issue's "BURNED").
            update_session_status(conn, s2.session_id, "COMPLETE")
        finally:
            conn.close()
        capsys.readouterr()  # clear

        result = main(["--config", cfg, "session", "list"])
        assert result == 0
        out = capsys.readouterr().out
        # Both session IDs are present in the listing.
        assert s1.session_id in out
        assert s2.session_id in out
        # Both statuses are visible.
        assert "STAGED" in out
        assert "COMPLETE" in out

    def test_session_list_status_filter(self, tmp_path, capsys):
        """--status STAGED shows only the STAGED session."""
        from lcsas.db.connection import get_connection
        from lcsas.db.sessions import create_session, update_session_status

        cfg, db = _write_session_config(tmp_path)
        conn = get_connection(db)
        try:
            staged = create_session(conn, media_type="TEST_TINY",
                                    staging_dir="/tmp/staged",
                                    session_id="sess-staged-only")
            other = create_session(conn, media_type="TEST_TINY",
                                   staging_dir="/tmp/other",
                                   session_id="sess-complete-only")
            update_session_status(conn, other.session_id, "COMPLETE")
        finally:
            conn.close()
        capsys.readouterr()  # clear

        result = main(["--config", cfg, "session", "list", "--status", "STAGED"])
        assert result == 0
        out = capsys.readouterr().out
        assert staged.session_id in out
        assert other.session_id not in out

    def test_session_list_invalid_status_errors(self, tmp_path, capsys):
        """Passing a bogus --status value: argparse has no `choices=`, so the
        handler accepts the filter, the DB returns no rows, and the handler
        prints a 'No sessions found ...' notice (with the bad status echoed)
        and exits 0. This documents the current handler contract."""
        from lcsas.db.connection import get_connection
        from lcsas.db.sessions import create_session

        cfg, db = _write_session_config(tmp_path)
        # Seed a real session so the empty result is unambiguously caused by
        # the filter, not by an empty table.
        conn = get_connection(db)
        try:
            create_session(conn, media_type="TEST_TINY",
                           staging_dir="/tmp/real", session_id="sess-real-001")
        finally:
            conn.close()
        capsys.readouterr()  # clear

        result = main(["--config", cfg, "session", "list", "--status", "BOGUS"])
        assert result == 0
        out = capsys.readouterr().out
        assert "No sessions found" in out
        assert "BOGUS" in out
        # The real session must NOT be listed because the filter excluded it.
        assert "sess-real-001" not in out
