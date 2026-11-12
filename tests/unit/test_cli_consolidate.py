"""Tests for the ``lcsas consolidate`` command (``lcsas.cli.commands.consolidate``)."""

from __future__ import annotations

from lcsas.cli.main import build_parser, main


def _seed_db_with_volumes(db_path, num_volumes: int = 2, packs_per_volume: int = 3):
    """Helper: write a real SQLite DB on disk with VERIFIED volumes and
    linked packs.  Returns the list of volume_ids in creation order.
    """
    from lcsas.db.connection import get_connection
    from lcsas.db.packs import register_pack
    from lcsas.db.repos import register_repo
    from lcsas.db.schema import create_all
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume
    from lcsas.utils.labels import generate_uuid

    conn = get_connection(str(db_path))
    try:
        create_all(conn)
        register_repo(conn, "test", "Test Repo", "/tmp/test_mirror")
        volume_ids: list[int] = []
        for v in range(num_volumes):
            vol = create_volume(
                conn,
                label=f"SRC_VOL_{v}",
                uuid=generate_uuid(),
                media_type="BD25",
                capacity_bytes=25_000_000_000,
                status="VERIFIED",
            )
            volume_ids.append(vol.volume_id)
            pack_ids = []
            for p in range(packs_per_volume):
                pack = register_pack(
                    conn,
                    sha256=f"vol{v}_pack{p}_hash_{'0' * 50}"[:64],
                    size_bytes=10_000,
                    repo_id="test",
                )
                pack_ids.append(pack.pack_id)
            bulk_link_packs(conn, vol.volume_id, pack_ids)
        conn.commit()
        return volume_ids
    finally:
        conn.close()


def _write_consolidate_config(tmp_path, db) -> str:
    """Write a TOML config valid for cmd_consolidate --execute paths.

    Returns the path to the config file as a string.  The config uses
    the canonical schema (``[paths] mirror_base / staging / database``,
    ``[repos.NAME] mirror_path``) — the wrong key names are silently
    accepted but emit "Unknown … keys" warnings and fall back to
    defaults that don't exist on this host.
    """
    mirror_base = tmp_path / "mirror"
    mirror_base.mkdir(exist_ok=True)
    test_mirror = mirror_base / "test"
    test_mirror.mkdir(exist_ok=True)
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)

    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        "[paths]\n"
        f'mirror_base = "{mirror_base}"\n'
        f'staging = "{staging}"\n'
        f'database = "{db}"\n'
        "\n"
        "[defaults]\n"
        'media_type = "BDXL100"\n'
        'label_prefix = "TEST"\n'
        "\n"
        "[repos.test]\n"
        f'mirror_path = "{test_mirror}"\n'
    )
    return str(cfg)


class TestCmdConsolidate:
    def test_consolidate_unknown_media_type_errors(self, tmp_path, capsys, caplog):
        """`consolidate --target-media BOGUS` exits non-zero with an error
        message that lists the valid media types."""
        db = tmp_path / "test.db"
        vol_ids = _seed_db_with_volumes(db, num_volumes=2, packs_per_volume=2)
        capsys.readouterr()
        caplog.clear()

        result = main([
            "--db", str(db), "consolidate",
            *[str(v) for v in vol_ids],
            "--target-media", "BOGUS_MEDIA",
        ])
        assert result == 1
        assert "Unknown media type 'BOGUS_MEDIA'" in caplog.text
        # The error message advertises canonical media types.
        assert "BDXL100" in caplog.text
        assert "TEST_TINY" in caplog.text

    def test_consolidate_plan_only_prints_summary(self, tmp_path, capsys, caplog):
        """Default `consolidate <ids> --target-media X` is plan-only.

        Exits 0, logs the plan, and prints next-step instructions
        including the `--execute` hint.
        """
        db = tmp_path / "test.db"
        vol_ids = _seed_db_with_volumes(db, num_volumes=2, packs_per_volume=3)
        caplog.clear()
        capsys.readouterr()

        result = main([
            "--db", str(db), "consolidate",
            *[str(v) for v in vol_ids],
            "--target-media", "BDXL100",
        ])
        assert result == 0
        assert "Consolidation Plan:" in caplog.text
        assert "SRC_VOL_0" in caplog.text and "SRC_VOL_1" in caplog.text
        assert "BDXL100" in caplog.text
        assert "Active packs:" in caplog.text
        assert "Volumes needed:" in caplog.text
        # Plan-only must always advertise the --execute next step.
        assert "--execute" in caplog.text

    def test_consolidate_deprecate_marks_sources(self, tmp_path, capsys, caplog):
        """`consolidate <ids> --deprecate` flips source volumes to
        DEPRECATED.

        Mirrors the real two-phase consolidation flow: by the time
        ``--deprecate`` is called, the operator has already burned the
        target volume(s) holding replica copies of every source pack.
        The deprecation safety check (Phase 19.4) blocks DEPRECATION
        when packs would become unreplicated, so we link an external
        REPLICA volume holding the same packs before invoking the
        handler.
        """
        from lcsas.db.connection import get_connection
        from lcsas.db.queries import get_packs_for_volume
        from lcsas.db.volume_packs import bulk_link_packs
        from lcsas.db.volumes import create_volume, get_volume_by_id
        from lcsas.utils.labels import generate_uuid

        db = tmp_path / "test.db"
        vol_ids = _seed_db_with_volumes(db, num_volumes=2, packs_per_volume=1)

        # Stand up a non-source REPLICA volume holding the same packs so
        # the deprecation-safety check sees a remaining copy per pack.
        conn = get_connection(str(db))
        try:
            replica = create_volume(
                conn, label="REPLICA_VOL", uuid=generate_uuid(),
                media_type="BDXL100", capacity_bytes=100_103_356_416,
                status="VERIFIED",
            )
            replica_pack_ids = []
            for src in vol_ids:
                for pack in get_packs_for_volume(conn, src):
                    replica_pack_ids.append(pack.pack_id)
            bulk_link_packs(conn, replica.volume_id, replica_pack_ids)
            conn.commit()
        finally:
            conn.close()

        caplog.clear()
        result = main([
            "--db", str(db), "consolidate",
            *[str(v) for v in vol_ids],
            "--target-media", "BDXL100",
            "--deprecate",
        ])
        assert result == 0
        assert f"Marked {len(vol_ids)} source volume" in caplog.text

        conn = get_connection(str(db))
        try:
            for vid in vol_ids:
                assert get_volume_by_id(conn, vid).status == "DEPRECATED"
            # The replica must stay VERIFIED — only the source ids deprecate.
            assert get_volume_by_id(conn, replica.volume_id).status == "VERIFIED"
        finally:
            conn.close()

    def test_consolidate_deprecate_and_execute_mutually_exclusive(
        self, tmp_path, capsys, caplog
    ):
        """`--deprecate` and `--execute` together exit non-zero."""
        db = tmp_path / "test.db"
        vol_ids = _seed_db_with_volumes(db, num_volumes=1, packs_per_volume=1)
        caplog.clear()

        result = main([
            "--db", str(db), "consolidate",
            *[str(v) for v in vol_ids],
            "--target-media", "BDXL100",
            "--deprecate", "--execute",
        ])
        assert result == 1
        assert "mutually exclusive" in caplog.text

    def test_consolidate_execute_requires_config(self, tmp_path, capsys, caplog):
        """`--execute` without `--config` exits non-zero.

        cmd_consolidate needs the LCSASConfig to construct the
        BurnOrchestrator (staging path, ECC settings, repos).  Bare
        `--db` alone is not enough.
        """
        db = tmp_path / "test.db"
        vol_ids = _seed_db_with_volumes(db, num_volumes=1, packs_per_volume=1)
        caplog.clear()

        result = main([
            "--db", str(db), "consolidate",
            *[str(v) for v in vol_ids],
            "--target-media", "BDXL100",
            "--execute",
        ])
        assert result == 1
        assert "--config is required" in caplog.text

    def test_consolidate_execute_confirmation_declined(
        self, tmp_path, capsys, caplog, monkeypatch
    ):
        """`--execute` then anything other than 'yes' cancels (exit 0).

        The handler prompts via `input()`.  Patch builtins.input to
        return "no" and assert we get a cancellation, not staging.
        """
        db = tmp_path / "test.db"
        vol_ids = _seed_db_with_volumes(db, num_volumes=1, packs_per_volume=1)
        cfg = _write_consolidate_config(tmp_path, db)

        monkeypatch.setattr("builtins.input", lambda _prompt="": "no")
        caplog.clear()

        result = main([
            "--config", cfg, "consolidate",
            *[str(v) for v in vol_ids],
            "--target-media", "BDXL100",
            "--execute",
        ])
        assert result == 0
        assert "Consolidation canceled" in caplog.text

    def test_consolidate_execute_eof_aborts(
        self, tmp_path, capsys, caplog, monkeypatch
    ):
        """`--execute` under redirected stdin (EOF on input) errors out
        rather than silently consolidating, so scripted callers get a
        clear "use a TTY or use the non-interactive path" signal."""
        db = tmp_path / "test.db"
        vol_ids = _seed_db_with_volumes(db, num_volumes=1, packs_per_volume=1)
        cfg = _write_consolidate_config(tmp_path, db)

        def _raise_eof(_prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise_eof)
        caplog.clear()

        result = main([
            "--config", cfg, "consolidate",
            *[str(v) for v in vol_ids],
            "--target-media", "BDXL100",
            "--execute",
        ])
        assert result == 1
        assert "No terminal available" in caplog.text

    def test_consolidate_execute_success_prints_next_steps(
        self, tmp_path, capsys, caplog, monkeypatch
    ):
        """`--execute` confirmed and staged exits 0 and prints the session
        to burn and the --deprecate command (#469).

        The next-steps line used to join the int volume IDs directly, so
        a successful stage ended in "Unexpected error" and exit 1 with
        the sources already CONSOLIDATING.  Staging itself is stubbed:
        only the handler's success path is under test.
        """
        from types import SimpleNamespace

        from lcsas.db.connection import get_connection
        from lcsas.db.volumes import get_volume_by_id

        db = tmp_path / "test.db"
        vol_ids = _seed_db_with_volumes(db, num_volumes=2, packs_per_volume=1)
        cfg = _write_consolidate_config(tmp_path, db)

        staged: list[list[str]] = []

        def _fake_stage(self, media_type, pack_sha256s, for_location=None):
            staged.append(list(pack_sha256s))
            return SimpleNamespace(
                session_id="SESSION-469",
                manifests=[SimpleNamespace(volume_label="NEW_VOL_0009")],
            )

        monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")
        monkeypatch.setattr(
            "lcsas.burn.orchestrator.BurnOrchestrator.stage", _fake_stage
        )
        caplog.clear()

        result = main([
            "--config", cfg, "consolidate",
            *[str(v) for v in vol_ids],
            "--target-media", "BDXL100",
            "--execute",
        ])
        assert result == 0, caplog.text
        assert len(staged) == 1 and len(staged[0]) == 2
        assert "Unexpected error" not in caplog.text
        assert "burn session SESSION-469" in caplog.text
        conn = get_connection(str(db))
        try:
            labels = [get_volume_by_id(conn, vid).label for vid in vol_ids]
            for vid in vol_ids:
                assert get_volume_by_id(conn, vid).status == "CONSOLIDATING"
        finally:
            conn.close()
        # Labels, not catalog IDs (#741): the owner and the heir use labels.
        assert "verify the new volumes (NEW_VOL_0009)" in caplog.text
        assert (
            "lcsas consolidate --deprecate " + " ".join(labels)
            in caplog.text
        )

    def test_consolidate_plan_names_sources_by_label(
        self, tmp_path, caplog,
    ):
        """The plan's '--deprecate' hint names the volumes by label (#741)."""
        from lcsas.db.connection import get_connection
        from lcsas.db.volumes import get_volume_by_id

        db = tmp_path / "test.db"
        vol_ids = _seed_db_with_volumes(db, num_volumes=2, packs_per_volume=1)
        cfg = _write_consolidate_config(tmp_path, db)
        caplog.clear()
        assert main([
            "--config", cfg, "consolidate", *[str(v) for v in vol_ids],
            "--target-media", "BDXL100",
        ]) == 0, caplog.text
        conn = get_connection(str(db))
        try:
            labels = [get_volume_by_id(conn, vid).label for vid in vol_ids]
        finally:
            conn.close()
        assert (
            "run: lcsas consolidate --deprecate " + " ".join(labels)
            in caplog.text
        )


class TestConsolidateParser:
    def test_consolidate_execute_flag_parses(self, capsys):
        """--execute flag is accepted by the parser."""
        parser = build_parser()
        args = parser.parse_args(["consolidate", "1", "2", "--execute"])
        assert args.execute is True
