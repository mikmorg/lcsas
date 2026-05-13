"""Tests for the 'lcsas scan' CLI command."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from lcsas.cli.main import build_parser, main
from lcsas.db.connection import get_connection
from lcsas.db.packs import list_packs
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all


def _make_mirror(tmp_path: Path, repo_name: str, pack_hashes: list[str]) -> Path:
    """Create a fake Rustic mirror with pack files in two-level layout."""
    mirror = tmp_path / "mirror" / repo_name
    data_dir = mirror / "data"
    for sha in pack_hashes:
        prefix_dir = data_dir / sha[:2]
        prefix_dir.mkdir(parents=True, exist_ok=True)
        pack_file = prefix_dir / sha
        pack_file.write_bytes(os.urandom(1024))
    # Create minimal repo structure
    (mirror / "config").write_text("{}")
    (mirror / "keys").mkdir(exist_ok=True)
    (mirror / "index").mkdir(exist_ok=True)
    (mirror / "snapshots").mkdir(exist_ok=True)
    return mirror


def _write_config(tmp_path: Path, db_path: Path, repos: dict[str, Path]) -> Path:
    """Write a minimal TOML config file."""
    config_path = tmp_path / "config.toml"
    repo_blocks = ""
    for name, mirror_path in repos.items():
        repo_blocks += f'\n[repos.{name}]\nmirror_path = "{mirror_path}"\npassword_file = ""\n'

    config_path.write_text(
        f'[paths]\nmirror_base = "{tmp_path / "mirror"}"\n'
        f'staging = "{tmp_path / "staging"}"\n'
        f'database = "{db_path}"\n'
        f"\n[defaults]\n"
        f'media_type = "TEST_TINY"\n'
        f'ecc_redundancy_pct = 0\n'
        f'location = "Home_Shelf"\n'
        f'optical_device = "/dev/null"\n'
        f'label_prefix = "TEST"\n'
        f"metadata_reserve_mb = 0\n"
        f"{repo_blocks}"
    )
    return config_path


class TestScanParser:
    def test_scan_parser_exists(self):
        """The scan subcommand is recognized by argparse."""
        parser = build_parser()
        args = parser.parse_args(["scan"])
        assert args.command == "scan"
        assert args.repo is None

    def test_scan_parser_with_repo_filter(self):
        """--repo accepts one or more repository names."""
        parser = build_parser()
        args = parser.parse_args(["scan", "--repo", "family", "personal"])
        assert args.repo == ["family", "personal"]

    def test_scan_parser_help(self, capsys):
        """Scan command appears in help output."""
        parser = build_parser()
        parser.print_help()
        out = capsys.readouterr().out
        assert "scan" in out.lower()


@pytest.mark.skipif(
    not shutil.which("rustic"), reason="rustic binary not installed"
)
class TestCmdScan:
    def test_scan_discovers_new_packs(self, tmp_path, capsys):
        """Scan finds packs on disk and registers them in the catalog."""
        db_path = tmp_path / "archive.db"
        hashes = ["aa" * 32, "bb" * 32, "cc" * 32]
        mirror = _make_mirror(tmp_path, "family", hashes)
        config_path = _write_config(tmp_path, db_path, {"family": mirror})

        # Init DB + register repo
        main(["init", "--db-path", str(db_path)])
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "family", "family", str(mirror), "")
        conn.close()

        result = main(["--config", str(config_path), "--db", str(db_path), "scan"])
        assert result == 0

        out = capsys.readouterr().out
        assert "family:" in out
        assert "Newly registered: 3" in out
        assert "Unarchived:" in out

        # Verify packs are actually in the DB
        conn = get_connection(db_path)
        create_all(conn)
        packs = list_packs(conn)
        conn.close()
        assert len(packs) == 3

    def test_scan_idempotent(self, tmp_path, capsys):
        """Running scan twice registers packs only once."""
        db_path = tmp_path / "archive.db"
        hashes = ["dd" * 32, "ee" * 32]
        mirror = _make_mirror(tmp_path, "work", hashes)
        config_path = _write_config(tmp_path, db_path, {"work": mirror})

        main(["init", "--db-path", str(db_path)])
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "work", "work", str(mirror), "")
        conn.close()

        # First scan
        main(["--config", str(config_path), "--db", str(db_path), "scan"])
        capsys.readouterr()

        # Second scan — should register 0 new
        result = main(["--config", str(config_path), "--db", str(db_path), "scan"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Newly registered: 0" in out

    def test_scan_repo_filter(self, tmp_path, capsys):
        """--repo filters to specific repositories."""
        db_path = tmp_path / "archive.db"
        mirror_a = _make_mirror(tmp_path, "alpha", ["a1" * 32])
        mirror_b = _make_mirror(tmp_path, "beta", ["b1" * 32])
        config_path = _write_config(
            tmp_path, db_path, {"alpha": mirror_a, "beta": mirror_b}
        )

        main(["init", "--db-path", str(db_path)])
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "alpha", "alpha", str(mirror_a), "")
        register_repo(conn, "beta", "beta", str(mirror_b), "")
        conn.close()

        result = main([
            "--config", str(config_path), "--db", str(db_path),
            "scan", "--repo", "alpha",
        ])
        assert result == 0
        out = capsys.readouterr().out
        assert "alpha:" in out
        assert "beta:" not in out

        # Only alpha's pack should be registered
        conn = get_connection(db_path)
        create_all(conn)
        packs = list_packs(conn)
        conn.close()
        assert len(packs) == 1

    def test_scan_empty_mirror(self, tmp_path, capsys):
        """Scanning a mirror with no packs reports zero."""
        db_path = tmp_path / "archive.db"
        mirror = tmp_path / "mirror" / "empty"
        mirror.mkdir(parents=True)
        (mirror / "data").mkdir()
        (mirror / "config").write_text("{}")
        (mirror / "keys").mkdir()
        (mirror / "index").mkdir()
        (mirror / "snapshots").mkdir()
        config_path = _write_config(tmp_path, db_path, {"empty": mirror})

        main(["init", "--db-path", str(db_path)])
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "empty", "empty", str(mirror), "")
        conn.close()

        result = main(["--config", str(config_path), "--db", str(db_path), "scan"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Packs on disk:  0" in out
        assert "Newly registered: 0" in out

    def test_scan_prints_total_summary(self, tmp_path, capsys):
        """Scan prints a total summary line across all repos."""
        db_path = tmp_path / "archive.db"
        mirror = _make_mirror(tmp_path, "repo1", ["ff" * 32])
        config_path = _write_config(tmp_path, db_path, {"repo1": mirror})

        main(["init", "--db-path", str(db_path)])
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "repo1", "repo1", str(mirror), "")
        conn.close()

        result = main(["--config", str(config_path), "--db", str(db_path), "scan"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Total scanned:" in out
        assert "New packs registered: 1" in out
        assert "1 total" in out
