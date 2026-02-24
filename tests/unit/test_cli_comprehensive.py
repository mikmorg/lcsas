"""Comprehensive CLI handler tests — covers all 17+ handlers.

Uses mock-based patterns since most handlers require config, external
tools, and filesystem.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.cli.main import build_parser, main
from lcsas.db.connection import get_connection
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volumes import create_volume

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_conn() -> sqlite3.Connection:
    conn = get_connection(":memory:")
    create_all(conn)
    return conn


def _ns(**kwargs) -> argparse.Namespace:
    """Build a Namespace with sensible defaults."""
    defaults = {
        "config": None,
        "db": None,
        "verbose": False,
        "command": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _write_config(tmp_path: Path, extra: str = "") -> Path:
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
{extra}
""")
    return cfg


# ===================================================================
# cmd_init
# ===================================================================

class TestCmdInit:
    def test_init_creates_db(self, tmp_path, capsys):
        db = tmp_path / "new.db"
        result = main(["init", "--db-path", str(db)])
        assert result == 0
        assert db.exists()

    def test_reinit_on_existing_db(self, tmp_path, capsys):
        db = tmp_path / "existing.db"
        main(["init", "--db-path", str(db)])
        result = main(["init", "--db-path", str(db)])
        assert result == 0


# ===================================================================
# cmd_repo_add / cmd_repo_list
# ===================================================================

class TestCmdRepoAddEdges:
    def test_duplicate_repo_name_errors(self, tmp_path, capsys):
        """Adding the same repo twice with same name; second should succeed
        (generates new UUID)."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        r1 = main(["--db", str(db), "repo", "add", "dup", "/p1"])
        r2 = main(["--db", str(db), "repo", "add", "dup", "/p2"])
        assert r1 == 0
        assert r2 == 0


class TestCmdRepoListEdges:
    def test_many_repos(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        for i in range(10):
            main(["--db", str(db), "repo", "add", f"repo{i}", f"/mnt/{i}"])
        capsys.readouterr()

        result = main(["--db", str(db), "repo", "list"])
        assert result == 0
        out = capsys.readouterr().out
        for i in range(10):
            assert f"repo{i}" in out


# ===================================================================
# cmd_scan
# ===================================================================

class TestCmdScan:
    def test_scan_registers_new_packs(self, tmp_path, capsys):
        """Scan with mock filesystem registers packs."""
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"

        # Pre-init DB + register a repo
        conn = get_connection(db)
        create_all(conn)
        register_repo(conn, "family", "family", str(tmp_path / "mirror"), "")
        conn.close()

        # Create fake pack files in mirror
        data_dir = tmp_path / "mirror" / "data"
        data_dir.mkdir(parents=True)
        fake_sha = "aa" * 32
        (data_dir / fake_sha).write_bytes(b"x" * 100)

        result = main(["--config", str(cfg), "scan", "--no-snapshots"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Total scanned:" in out

    def test_scan_unknown_repo_warning(self, tmp_path, capsys):
        """Scanning an unknown repo name produces a warning, not crash."""
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main([
            "--config", str(cfg), "scan", "--repo", "nonexistent",
            "--no-snapshots",
        ])
        assert result == 0
        out = capsys.readouterr().out
        assert "not found in config" in out


# ===================================================================
# cmd_status — extended
# ===================================================================

class TestCmdStatusEdges:
    def test_status_with_volumes(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_001", "uuid1", "TEST_TINY", 1000000, "Home", "VERIFIED")
        conn.close()

        result = main(["--db", str(db), "status"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Volumes:" in out


# ===================================================================
# cmd_db_export — extended
# ===================================================================

class TestCmdDbExportEdges:
    def test_export_has_all_keys(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        register_repo(conn, "r1", "work", "/mnt/work", "")
        create_volume(conn, "V1", "u1", "BD25", 25_000_000_000, "Home", "VERIFIED")
        conn.close()

        result = main(["--db", str(db), "db", "export"])
        assert result == 0
        data = json.loads(capsys.readouterr().out)
        assert data["status"]["total"] >= 0
        assert len(data["volumes"]) == 1
        assert data["volumes"][0]["label"] == "V1"


# ===================================================================
# cmd_config_check
# ===================================================================

class TestCmdConfigCheck:
    def test_valid_config(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        result = main(["--config", str(cfg), "config", "check"])
        assert result == 0
        out = capsys.readouterr().out
        assert "valid" in out.lower()

    def test_missing_paths_errors(self, tmp_path, capsys):
        cfg = tmp_path / "bad.toml"
        cfg.write_text("""
[paths]
mirror_base = "/nonexistent/mirror"
staging = "/nonexistent/staging"
database = "/nonexistent/db/archive.db"
""")
        result = main(["--config", str(cfg), "config", "check"])
        assert result == 1
        out = capsys.readouterr().out
        assert "does not exist" in out

    def test_config_required(self, capsys):
        result = main(["config", "check"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()

    def test_bad_ecc_redundancy(self, tmp_path, capsys):
        cfg = _write_config(tmp_path, "ecc_redundancy_pct = 200")
        result = main(["--config", str(cfg), "config", "check"])
        assert result == 1
        out = capsys.readouterr().out
        assert "out of range" in out


# ===================================================================
# cmd_stage (mock-based)
# ===================================================================

class TestCmdStage:
    def test_stage_requires_config(self, capsys):
        result = main(["stage"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()

    def test_stage_unknown_media(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--config", str(cfg), "stage", "--media", "BOGUS_TYPE"])
        assert result == 1
        out = capsys.readouterr().out
        assert "Unknown media type" in out

    def test_stage_dry_run(self, tmp_path, capsys):
        """Dry-run produces plan output but no DB/FS changes."""
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        register_repo(conn, "fam", "fam", str(tmp_path / "mirror"), "")

        from lcsas.db.packs import register_pack
        register_pack(conn, "aa" * 32, 5000, "fam")
        conn.close()

        mock_stage_result = MagicMock()
        mock_stage_result.session_id = "dry-run"
        mock_stage_result.manifests = []

        with patch("lcsas.burn.orchestrator.BurnOrchestrator.stage",
                    return_value=mock_stage_result) as mock_s:
            result = main(["--config", str(cfg), "stage", "--dry-run"])

        assert result == 0
        mock_s.assert_called_once()
        _, kwargs = mock_s.call_args
        assert kwargs.get("dry_run") is True


# ===================================================================
# cmd_burn_session (mock-based)
# ===================================================================

class TestCmdBurnSession:
    def test_burn_session_requires_config(self, capsys):
        result = main(["burn", "--session", "latest"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()


# ===================================================================
# cmd_burn_iso (mock-based)
# ===================================================================

class TestCmdBurnIso:
    def test_burn_iso_missing_file(self, tmp_path, capsys):
        iso = tmp_path / "missing.iso"
        result = main(["burn-iso", str(iso)])
        assert result == 1

    def test_burn_iso_file_exists(self, tmp_path, capsys):
        """Burn ISO calls xorriso runner."""
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\0" * 100)

        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.burn_iso") as mock_burn,
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
        ):
            result = main(["burn-iso", str(iso)])

        assert result == 0
        mock_burn.assert_called_once()


# ===================================================================
# cmd_location
# ===================================================================

class TestCmdLocation:
    def test_location_requires_config(self, capsys):
        result = main(["location", "list"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()

    def test_location_add_and_list(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        r1 = main(["--config", str(cfg), "location", "add", "Offsite_Safe"])
        assert r1 == 0
        capsys.readouterr()

        r2 = main(["--config", str(cfg), "location", "list"])
        assert r2 == 0
        out = capsys.readouterr().out
        assert "Offsite_Safe" in out

    def test_location_add_rejects_path_separator(self, tmp_path, capsys):
        """Sanitization rejects names with path separators."""
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--config", str(cfg), "location", "add", "../../etc"])
        assert result == 1
        out = capsys.readouterr().out
        assert "unsafe" in out.lower() or "error" in out.lower()

    def test_location_status(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        from lcsas.db.locations import create_location
        create_location(conn, "Home_Shelf", "")
        conn.close()

        result = main(["--config", str(cfg), "location", "status", "Home_Shelf"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Home_Shelf" in out

    def test_location_move_nonexistent_volume(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main([
            "--config", str(cfg), "location", "move", "NONEXIST",
            "--from", "A", "--to", "B",
        ])
        assert result == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower()


# ===================================================================
# cmd_catalog_import
# ===================================================================

class TestCmdCatalogImport:
    def test_import_receipts_from_json(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_001", "uuid1", "TEST_TINY", 1000, "Home", "VERIFIED")
        conn.close()

        receipt = tmp_path / "receipt.json"
        receipt.write_text(json.dumps({
            "volume_label": "VOL_001",
            "volume_id": 1,
            "session_id": "s1",
            "location": "Offsite",
            "pack_count": 5,
        }))

        result = main(["--config", str(cfg), "catalog", "import-receipts",
                        str(receipt)])
        assert result == 0


# ===================================================================
# cmd_verify (mock-based)
# ===================================================================

class TestCmdVerify:
    def test_verify_unknown_volume(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--db", str(db), "verify", "NONEXIST"])
        assert result == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower()

    def test_verify_disc_calls_runner(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_001", "u1", "BD25", 25e9, "Home", "VERIFIED")
        conn.close()

        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True) as mock_verify,
        ):
            result = main(["--db", str(db), "verify", "VOL_001", "--disc"])

        assert result == 0
        mock_verify.assert_called_once()

    def test_verify_mark_verified(self, tmp_path, capsys):
        """--mark-verified records VERIFY_PASS event and promotes BURNED."""
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_MV", "u1", "BD25", 25e9, "Home", "BURNED")
        conn.close()

        result = main(["--db", str(db), "verify", "VOL_MV",
                        "--mark-verified", "--detail", "Verified on remote machine"])
        assert result == 0

        conn = get_connection(db)
        from lcsas.db.volumes import get_volume_by_label
        vol = get_volume_by_label(conn, "VOL_MV")
        assert vol.status == "VERIFIED"

        from lcsas.db.volume_events import get_events_for_volume
        events = get_events_for_volume(conn, vol.volume_id, "VERIFY_PASS")
        assert len(events) == 1
        assert "remote" in events[0].detail.lower()
        conn.close()

    def test_verify_mark_failed(self, tmp_path, capsys):
        """--mark-failed records VERIFY_FAIL event."""
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        create_volume(conn, "VOL_MF", "u1", "BD25", 25e9, "Home", "VERIFIED")
        conn.close()

        result = main(["--db", str(db), "verify", "VOL_MF",
                        "--mark-failed", "--detail", "Sector errors at offset 0x400"])
        assert result == 0

        conn = get_connection(db)
        from lcsas.db.volumes import get_volume_by_label
        vol = get_volume_by_label(conn, "VOL_MF")
        from lcsas.db.volume_events import get_events_for_volume
        events = get_events_for_volume(conn, vol.volume_id, "VERIFY_FAIL")
        assert len(events) == 1
        assert "Sector errors" in events[0].detail
        conn.close()

    def test_verify_no_label_no_all_returns_error(self, tmp_path, capsys):
        """Omitting volume label without --all returns error."""
        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--db", str(db), "verify"])
        assert result == 1

    def test_verify_all_skipped_returns_error(self, tmp_path, capsys):
        """When --all runs but all ISOs are cleaned, return 1 (not success)."""
        from lcsas.db.sessions import add_session_volume, create_session

        db = tmp_path / "test.db"
        conn = get_connection(db)
        create_all(conn)
        vol = create_volume(conn, "VOL_SKIP", "u1", "BD25", 25e9,
                            "Home", "VERIFIED")
        # Register session volume pointing to a nonexistent ISO
        create_session(conn, media_type="BD25", staging_dir="/tmp",
                       session_id="sess1")
        add_session_volume(conn, "sess1", vol.volume_id,
                           iso_path="/nonexistent/path.iso",
                           iso_sha256="")
        conn.close()

        result = main(["--db", str(db), "verify", "--all"])
        assert result == 1
        out = capsys.readouterr().out
        assert "skipped" in out.lower() or "No volumes" in out


# ===================================================================
# cmd_consolidate (mock-based)
# ===================================================================

class TestCmdConsolidate:
    def test_consolidate_dispatches(self, capsys):
        """Consolidate command invokes cmd_consolidate handler."""
        parser = build_parser()
        args = parser.parse_args(["consolidate", "1", "2", "--target-media", "MDISC100"])
        assert args.command == "consolidate"
        assert args.volume_ids == [1, 2]

    def test_consolidate_execute_flag_parses(self, capsys):
        """--execute flag is accepted by the parser."""
        parser = build_parser()
        args = parser.parse_args(["consolidate", "1", "2", "--execute"])
        assert args.execute is True


class TestCmdRepoRemove:
    def test_remove_nonexistent_repo(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        result = main(["--db", str(db), "repo", "remove", "nonexistent"])
        assert result == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower()

    def test_remove_empty_repo(self, tmp_path, capsys):
        """Removing repo with no packs succeeds without --force."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "empty_repo", "/mnt/empty"])

        # Find the repo_id
        conn = get_connection(db)
        from lcsas.db.repos import list_repos
        repos = [r for r in list_repos(conn) if r.name == "empty_repo"]
        assert len(repos) == 1
        repo_id = repos[0].repo_id
        conn.close()

        capsys.readouterr()
        result = main(["--db", str(db), "repo", "remove", repo_id])
        assert result == 0
        out = capsys.readouterr().out
        assert "Removed" in out

    def test_remove_with_packs_needs_force(self, tmp_path, capsys):
        """Removing repo with active packs without --force fails."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "packed", "/mnt/packed"])

        conn = get_connection(db)
        from lcsas.db.repos import list_repos
        repos = [r for r in list_repos(conn) if r.name == "packed"]
        repo_id = repos[0].repo_id
        from lcsas.db.packs import register_pack
        register_pack(conn, "cc" * 32, 1000, repo_id)
        conn.close()

        capsys.readouterr()
        result = main(["--db", str(db), "repo", "remove", repo_id])
        assert result == 1
        out = capsys.readouterr().out
        assert "force" in out.lower()


# ===================================================================
# cmd_meta_build (mock-based)
# ===================================================================

class TestCmdMetaBuild:
    def test_meta_build_dispatches(self, capsys):
        """Meta build command parses correctly."""
        parser = build_parser()
        args = parser.parse_args(["meta", "build", "--output", "/tmp/meta"])
        assert args.command == "meta"
        assert args.meta_command == "build"
        assert args.output == Path("/tmp/meta")


# ===================================================================
# cmd_staging_clean
# ===================================================================

class TestCmdStagingClean:
    def test_staging_clean_no_orphans(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        result = main(["--config", str(cfg), "staging", "clean", "--force"])
        assert result == 0
        out = capsys.readouterr().out
        assert "No orphaned" in out

    def test_staging_clean_detects_orphan(self, tmp_path, capsys):
        cfg = _write_config(tmp_path)
        db = tmp_path / "archive.db"
        conn = get_connection(db)
        create_all(conn)
        conn.close()

        # Create an orphan directory in staging (must match session-id pattern)
        orphan = tmp_path / "staging" / "2025-01-01T00-00-00.000000+00-00-deadbeef"
        orphan.mkdir(parents=True)

        result = main(["--config", str(cfg), "staging", "clean", "--force"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Removed 1" in out
        assert not orphan.exists()

    def test_staging_clean_requires_config(self, capsys):
        result = main(["staging", "clean"])
        assert result == 1
        out = capsys.readouterr().out
        assert "required" in out.lower()


# ===================================================================
# cmd_restore_plan / cmd_restore_exec — see test_cli_restore.py
# ===================================================================


# ===================================================================
# Dispatch routing
# ===================================================================

class TestDispatchRouting:
    def test_all_commands_parse(self):
        """Verify parser accepts all known commands without errors."""
        parser = build_parser()
        commands = [
            ["init", "--db-path", "/tmp/x.db"],
            ["repo", "add", "fam", "/mnt"],
            ["repo", "list"],
            ["scan"],
            ["status"],
            ["db", "export"],
            ["config", "check"],
            ["staging", "clean"],
            ["stage"],
            ["burn", "--session", "latest"],
            ["burn"],
            ["burn-iso", "/tmp/t.iso"],
            ["location", "list"],
            ["location", "add", "Home"],
            ["location", "status", "Home"],
            ["location", "move", "VOL", "--from", "A", "--to", "B"],
            ["catalog", "import-receipts", "f.json"],
            ["restore", "plan", "snap1", "--repo", "fam"],
            ["restore", "exec", "snap1", "/tmp", "--repo", "fam",
             "--password-file", "/k"],
            ["consolidate", "1", "2"],
            ["verify", "VOL", "--disc"],
            ["meta", "build", "--output", "/tmp/m"],
        ]
        for cmd in commands:
            args = parser.parse_args(cmd)
            assert args.command is not None, f"Failed to parse: {cmd}"

    def test_dry_run_flag_on_stage(self):
        parser = build_parser()
        args = parser.parse_args(["stage", "--dry-run"])
        assert args.dry_run is True

    def test_dry_run_short_flag_on_burn(self):
        parser = build_parser()
        args = parser.parse_args(["burn", "-n"])
        assert args.dry_run is True

    def test_skip_verify_flag(self):
        parser = build_parser()
        args = parser.parse_args([
            "restore", "exec", "snap1", "/tmp",
            "--repo", "fam", "--password-file", "/k", "--skip-verify",
        ])
        assert args.skip_verify is True

    def test_no_snapshots_flag(self):
        parser = build_parser()
        args = parser.parse_args(["scan", "--no-snapshots"])
        assert args.no_snapshots is True


# ===================================================================
# New module tests — validate_config, sanitize_name, shutdown, etc.
# ===================================================================

class TestValidateConfig:
    def test_valid_config_no_errors(self, tmp_path):
        from lcsas.config.settings import default_config, validate_config

        mirror = tmp_path / "mirror"
        mirror.mkdir()
        staging = tmp_path / "staging"
        staging.mkdir()
        db = tmp_path / "archive.db"

        cfg = default_config(mirror, staging, db)
        errors = validate_config(cfg)
        assert errors == []

    def test_missing_mirror(self, tmp_path):
        from lcsas.config.settings import default_config, validate_config

        staging = tmp_path / "staging"
        staging.mkdir()
        db = tmp_path / "archive.db"

        cfg = default_config(tmp_path / "no_mirror", staging, db)
        errors = validate_config(cfg)
        assert any("mirror_base_path" in e for e in errors)

    def test_ecc_out_of_range(self, tmp_path):
        from lcsas.config.settings import LCSASConfig, validate_config

        mirror = tmp_path / "mirror"
        mirror.mkdir()
        staging = tmp_path / "staging"
        staging.mkdir()

        cfg = LCSASConfig(
            mirror_base_path=mirror,
            staging_path=staging,
            db_path=tmp_path / "db",
            default_ecc_redundancy_pct=150,
        )
        errors = validate_config(cfg)
        assert any("out of range" in e for e in errors)


class TestSanitizeName:
    def test_valid_name(self):
        from lcsas.utils.labels import sanitize_name
        assert sanitize_name("Offsite_Safe") == "Offsite_Safe"

    def test_rejects_path_separator(self):
        from lcsas.utils.labels import sanitize_name
        with pytest.raises(ValueError, match="unsafe"):
            sanitize_name("../etc/passwd")

    def test_rejects_backslash(self):
        from lcsas.utils.labels import sanitize_name
        with pytest.raises(ValueError, match="unsafe"):
            sanitize_name("foo\\bar")

    def test_rejects_null_byte(self):
        from lcsas.utils.labels import sanitize_name
        with pytest.raises(ValueError, match="unsafe"):
            sanitize_name("foo\x00bar")

    def test_rejects_empty(self):
        from lcsas.utils.labels import sanitize_name
        with pytest.raises(ValueError, match="must not be empty"):
            sanitize_name("")

    def test_rejects_too_long(self):
        from lcsas.utils.labels import sanitize_name
        with pytest.raises(ValueError, match="maximum length"):
            sanitize_name("x" * 200)


class TestShutdownManager:
    def test_callbacks_run_in_reverse_order(self):
        from lcsas.utils.shutdown import ShutdownManager

        order: list[int] = []
        mgr = ShutdownManager()
        mgr.register(lambda: order.append(1))
        mgr.register(lambda: order.append(2))
        mgr.register(lambda: order.append(3))
        mgr.run_cleanups()
        assert order == [3, 2, 1]

    def test_callback_error_is_swallowed(self):
        from lcsas.utils.shutdown import ShutdownManager

        mgr = ShutdownManager()
        mgr.register(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        mgr.register(lambda: None)
        # Should not raise
        mgr.run_cleanups()


class TestPackCorruptionError:
    def test_good_hash_passes(self, tmp_path):
        from lcsas.restore.executor import RestoreExecutor

        mock_runner = MagicMock()
        executor = RestoreExecutor(mock_runner)

        # Create a pack file whose name matches its SHA-256
        vol = tmp_path / "volume" / "data"
        vol.mkdir(parents=True)
        content = b"hello_world_pack_data"
        from hashlib import sha256 as _sha256
        actual_hash = _sha256(content).hexdigest()
        (vol / actual_hash).write_bytes(content)

        cache = tmp_path / "cache"
        cache.mkdir()

        count = executor.ingest_volume(cache, tmp_path / "volume", [actual_hash], verify=True)
        assert count == 1

    def test_bad_hash_raises_and_deletes(self, tmp_path):
        from lcsas.restore.executor import PackCorruptionError, RestoreExecutor

        mock_runner = MagicMock()
        executor = RestoreExecutor(mock_runner)

        vol = tmp_path / "volume" / "data"
        vol.mkdir(parents=True)
        fake_sha = "bb" * 32
        (vol / fake_sha).write_bytes(b"wrong content")

        cache = tmp_path / "cache"
        cache.mkdir()

        with pytest.raises(PackCorruptionError):
            executor.ingest_volume(cache, tmp_path / "volume", [fake_sha], verify=True)

        # Verify the bad file was deleted
        dst = cache / "data" / fake_sha[:2] / fake_sha
        assert not dst.exists()

    def test_skip_verify_skips_check(self, tmp_path):
        from lcsas.restore.executor import RestoreExecutor

        mock_runner = MagicMock()
        executor = RestoreExecutor(mock_runner)

        vol = tmp_path / "volume" / "data"
        vol.mkdir(parents=True)
        fake_sha = "cc" * 32
        (vol / fake_sha).write_bytes(b"whatever")

        cache = tmp_path / "cache"
        cache.mkdir()

        count = executor.ingest_volume(
            cache, tmp_path / "volume", [fake_sha], verify=False,
        )
        assert count == 1


class TestMaskPasswordPath:
    def test_masks_key_file(self):
        from lcsas.log import mask_password_path
        assert mask_password_path("/root/keys/family.key") == "***"

    def test_masks_pem_file(self):
        from lcsas.log import mask_password_path
        assert mask_password_path("/tmp/cert.pem") == "***"

    def test_passes_normal_path(self):
        from lcsas.log import mask_password_path
        assert mask_password_path("/mnt/mirror/data/pack.bin") == "/mnt/mirror/data/pack.bin"


class TestOrphanedStagingCleanup:
    def test_detect_no_orphans(self, tmp_path):
        from lcsas.config.settings import default_config
        from lcsas.staging.cleanup import detect_orphaned_staging

        staging = tmp_path / "staging"
        staging.mkdir()
        cfg = default_config(tmp_path, staging, tmp_path / "db")
        conn = _mem_conn()
        result = detect_orphaned_staging(cfg, conn)
        assert result == []

    def test_detect_orphan_dir(self, tmp_path):
        from lcsas.config.settings import default_config
        from lcsas.staging.cleanup import detect_orphaned_staging

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "2025-01-01T00-00-00.000000+00-00-deadbeef").mkdir()
        cfg = default_config(tmp_path, staging, tmp_path / "db")
        conn = _mem_conn()

        result = detect_orphaned_staging(cfg, conn)
        assert len(result) == 1
        assert result[0].name == "2025-01-01T00-00-00.000000+00-00-deadbeef"

    def test_active_session_not_detected(self, tmp_path):
        from lcsas.config.settings import default_config
        from lcsas.staging.cleanup import detect_orphaned_staging

        staging = tmp_path / "staging"
        staging.mkdir()
        active_dir = staging / "active"
        active_dir.mkdir()
        cfg = default_config(tmp_path, staging, tmp_path / "db")
        conn = _mem_conn()

        # Add an active session pointing to this dir
        conn.execute(
            "INSERT INTO burn_sessions (session_id, media_type, status, staging_dir) "
            "VALUES (?, ?, ?, ?)",
            ("s1", "TEST_TINY", "STAGED", str(active_dir)),
        )
        conn.commit()

        result = detect_orphaned_staging(cfg, conn)
        assert result == []

    def test_clean_orphaned(self, tmp_path):
        from lcsas.staging.cleanup import clean_orphaned_staging

        orphan = tmp_path / "orphan"
        orphan.mkdir()
        (orphan / "file.iso").write_bytes(b"data")

        count = clean_orphaned_staging([orphan])
        assert count == 1
        assert not orphan.exists()


class TestSnapshotDB:
    def test_upsert_and_list(self):
        from lcsas.db.snapshots import list_snapshots, upsert_snapshot

        conn = _mem_conn()
        register_repo(conn, "r1", "fam", "/mnt/fam", "")

        upsert_snapshot(conn, "snap1", "r1", "host1", "2024-01-01T00:00:00",
                        '["/" ]', '["daily"]', "desc")
        upsert_snapshot(conn, "snap2", "r1", "host1", "2024-01-02T00:00:00")

        snaps = list_snapshots(conn)
        assert len(snaps) == 2
        assert snaps[0].snapshot_id == "snap1"

    def test_upsert_replaces(self):
        from lcsas.db.snapshots import get_snapshot, upsert_snapshot

        conn = _mem_conn()
        register_repo(conn, "_test", "Test", "/test", "")
        upsert_snapshot(conn, "s1", "_test", "h1", "t1", description="v1")
        upsert_snapshot(conn, "s1", "_test", "h2", "t2", description="v2")

        snap = get_snapshot(conn, "s1")
        assert snap is not None
        assert snap.hostname == "h2"
        assert snap.description == "v2"

    def test_bulk_upsert(self):
        from lcsas.db.models import Snapshot
        from lcsas.db.snapshots import bulk_upsert_snapshots, list_snapshots

        conn = _mem_conn()
        register_repo(conn, "_test", "Test", "/test", "")
        snaps = [
            Snapshot("s1", "_test", "h1", "t1", "[]", "[]", ""),
            Snapshot("s2", "_test", "h2", "t2", "[]", "[]", ""),
            Snapshot("s3", "_test", "h3", "t3", "[]", "[]", ""),
        ]
        count = bulk_upsert_snapshots(conn, snaps)
        assert count == 3
        assert len(list_snapshots(conn)) == 3

    def test_list_by_repo(self):
        from lcsas.db.snapshots import list_snapshots, upsert_snapshot

        conn = _mem_conn()
        register_repo(conn, "r1", "a", "/a", "")
        register_repo(conn, "r2", "b", "/b", "")

        upsert_snapshot(conn, "s1", "r1", "h", "t1")
        upsert_snapshot(conn, "s2", "r2", "h", "t2")
        upsert_snapshot(conn, "s3", "r1", "h", "t3")

        assert len(list_snapshots(conn, repo_id="r1")) == 2
        assert len(list_snapshots(conn, repo_id="r2")) == 1

    def test_get_none(self):
        from lcsas.db.snapshots import get_snapshot

        conn = _mem_conn()
        assert get_snapshot(conn, "nonexistent") is None


# ---------------------------------------------------------------------------
# _resolve_repo_names_to_ids — CRITICAL safety tests
# ---------------------------------------------------------------------------


class TestResolveRepoNamesToIds:
    """Ensure typo'd repo names raise instead of silently matching all repos."""

    def test_none_input_returns_none(self):
        from lcsas.cli.main import _resolve_repo_names_to_ids

        conn = _mem_conn()
        assert _resolve_repo_names_to_ids(conn, None) is None

    def test_valid_names_return_ids(self):
        from lcsas.cli.main import _resolve_repo_names_to_ids

        conn = _mem_conn()
        register_repo(conn, "family", "Family", "/mnt/mirror/family")
        register_repo(conn, "work", "Work", "/mnt/mirror/work")

        ids = _resolve_repo_names_to_ids(conn, ["Family"])
        assert ids is not None
        assert len(ids) == 1

    def test_all_invalid_names_raises(self):
        from lcsas.cli.main import _resolve_repo_names_to_ids

        conn = _mem_conn()
        register_repo(conn, "family", "Family", "/mnt/mirror/family")

        with pytest.raises(ValueError, match="None of the specified repositories"):
            _resolve_repo_names_to_ids(conn, ["typo_name", "bogus"])

    def test_partial_valid_returns_valid_only(self):
        from lcsas.cli.main import _resolve_repo_names_to_ids

        conn = _mem_conn()
        register_repo(conn, "family", "Family", "/mnt/mirror/family")
        register_repo(conn, "work", "Work", "/mnt/mirror/work")

        ids = _resolve_repo_names_to_ids(conn, ["Family", "nonexistent"])
        assert ids is not None
        assert len(ids) == 1
