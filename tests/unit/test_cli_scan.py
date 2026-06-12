"""Tests for the 'lcsas scan' CLI command."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from lcsas.cli.main import build_parser, main
from lcsas.db.connection import get_connection
from lcsas.db.packs import list_packs, mark_pruned, register_pack
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


class TestPruneSyncGuards:
    """BURN-09 guards — these scans pass --no-snapshots, so no rustic needed."""

    def test_partial_scan_does_not_mark_pruned(self, tmp_path, capsys, monkeypatch):
        """BURN-09: an unreadable data/XY subdir disables prune-sync.

        A transient permission/mount glitch yields a non-empty scan that
        is missing whole hash-prefix ranges — those packs must NOT be
        marked pruned.
        """
        db_path = tmp_path / "archive.db"
        hashes = ["aa" * 32, "bb" * 32]
        mirror = _make_mirror(tmp_path, "family", hashes)
        config_path = _write_config(tmp_path, db_path, {"family": mirror})

        main(["init", "--db-path", str(db_path)])
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "family", "family", str(mirror), "")
        conn.close()

        # First (complete) scan registers both packs.
        assert main(["--config", str(config_path), "--db", str(db_path),
                     "scan", "--no-snapshots"]) == 0
        capsys.readouterr()

        # Second scan: the data/bb subdir raises PermissionError.
        import lcsas.packs.scanner as scanner_mod
        real_scandir = os.scandir

        def fake_scandir(path):
            if str(path).endswith(f"{os.sep}bb"):
                raise PermissionError("Permission denied")
            return real_scandir(path)

        monkeypatch.setattr(scanner_mod.os, "scandir", fake_scandir)
        assert main(["--config", str(config_path), "--db", str(db_path),
                     "scan", "--no-snapshots"]) == 0
        monkeypatch.undo()

        out = capsys.readouterr().out
        assert "INCOMPLETE" in out
        assert "prune-sync skipped" in out

        conn = get_connection(db_path)
        active = list_packs(conn, include_pruned=False)
        conn.close()
        assert len(active) == 2  # nothing was marked pruned

    def test_mass_prune_requires_confirmation(self, tmp_path, capsys):
        """BURN-09: pruning >max(10, 20% of active) needs --yes-prune."""
        db_path = tmp_path / "archive.db"
        shas = [f"{i:064x}" for i in range(100)]
        # Mirror holds only 1 of the 100 known packs.
        mirror = _make_mirror(tmp_path, "family", [shas[0]])
        config_path = _write_config(tmp_path, db_path, {"family": mirror})

        main(["init", "--db-path", str(db_path)])
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "family", "family", str(mirror), "")
        for sha in shas:
            register_pack(conn, sha256=sha, size_bytes=1024, repo_id="family")
        conn.close()

        # Without --yes-prune: refused, nothing pruned.
        assert main(["--config", str(config_path), "--db", str(db_path),
                     "scan", "--no-snapshots"]) == 0
        out = capsys.readouterr().out
        assert "Refusing to mark 99/100" in out
        assert "--yes-prune" in out

        conn = get_connection(db_path)
        assert len(list_packs(conn, include_pruned=False)) == 100
        conn.close()

        # With --yes-prune: the mass-prune is confirmed and applied.
        assert main(["--config", str(config_path), "--db", str(db_path),
                     "scan", "--no-snapshots", "--yes-prune"]) == 0
        out = capsys.readouterr().out
        assert "Pruned packs:   99" in out

        conn = get_connection(db_path)
        active = list_packs(conn, include_pruned=False)
        conn.close()
        assert [p.sha256 for p in active] == [shas[0]]

    def test_small_prune_still_automatic(self, tmp_path, capsys):
        """BURN-09: a small prune (≤ threshold) needs no confirmation."""
        db_path = tmp_path / "archive.db"
        shas = [f"{i:064x}" for i in range(100)]
        # Mirror holds 98 of the 100 known packs (2 genuinely pruned).
        mirror = _make_mirror(tmp_path, "family", shas[:98])
        config_path = _write_config(tmp_path, db_path, {"family": mirror})

        main(["init", "--db-path", str(db_path)])
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "family", "family", str(mirror), "")
        for sha in shas:
            register_pack(conn, sha256=sha, size_bytes=1024, repo_id="family")
        conn.close()

        assert main(["--config", str(config_path), "--db", str(db_path),
                     "scan", "--no-snapshots"]) == 0
        out = capsys.readouterr().out
        assert "Pruned packs:   2" in out
        assert "Refusing" not in out

        conn = get_connection(db_path)
        assert len(list_packs(conn, include_pruned=False)) == 98
        conn.close()


class TestPackUnprune:
    """BURN-09: 'lcsas pack unprune' — the recovery tool for mis-prunes."""

    def _db_with_pruned_packs(self, tmp_path, shas: list[str]):
        db_path = tmp_path / "archive.db"
        main(["init", "--db-path", str(db_path)])
        conn = get_connection(db_path)
        create_all(conn)
        register_repo(conn, "family", "family", "/mirror/family", "")
        for sha in shas:
            p = register_pack(conn, sha256=sha, size_bytes=1024, repo_id="family")
            mark_pruned(conn, p.pack_id)
        conn.close()
        return db_path

    def test_unprune_restores_pack(self, tmp_path, capsys):
        sha = "ab" * 32
        db_path = self._db_with_pruned_packs(tmp_path, [sha])

        rc = main(["--db", str(db_path), "pack", "unprune", sha[:12]])
        assert rc == 0
        out = capsys.readouterr().out
        assert "restored to the active pool" in out

        conn = get_connection(db_path)
        active = list_packs(conn, include_pruned=False)
        conn.close()
        assert [p.sha256 for p in active] == [sha]

    def test_unprune_ambiguous_prefix_refused(self, tmp_path, capsys):
        sha_a = "ab" + "11" * 31
        sha_b = "ab" + "22" * 31
        db_path = self._db_with_pruned_packs(tmp_path, [sha_a, sha_b])

        rc = main(["--db", str(db_path), "pack", "unprune", "ab"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "ambiguous" in out

        conn = get_connection(db_path)
        active = list_packs(conn, include_pruned=False)
        conn.close()
        assert active == []  # both still pruned

    def test_unprune_no_match(self, tmp_path, capsys):
        db_path = self._db_with_pruned_packs(tmp_path, ["cd" * 32])

        rc = main(["--db", str(db_path), "pack", "unprune", "ff" * 32])
        assert rc == 1
        out = capsys.readouterr().out
        assert "No pack matches" in out
