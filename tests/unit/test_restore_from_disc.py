"""Tests for the 'lcsas restore standalone' CLI command."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from lcsas.cli.main import build_parser, cmd_restore_from_disc
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume
from lcsas.restore.executor import IngestionResult
from lcsas.rustic.types import RestorePlan
from lcsas.utils.labels import generate_uuid

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_disc_args(**kwargs) -> argparse.Namespace:
    """Build a Namespace mimicking parsed 'restore standalone' args."""
    defaults = {
        "command": "restore",
        "restore_command": "standalone",
        "disc": Path("/mnt/disc1"),
        "target_path": Path("/tmp/restored"),
        "password_file": Path("/home/user/secret.key"),
        "repo": None,
        "snapshot": "latest",
        "volume_dir": None,
        "catalog": None,
        "cache_dir": None,
        "skip_verify": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _write_catalog(path: Path, repo_name: str = "family") -> None:
    """Write a minimal catalog.db to *path* with one repository."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    create_all(conn)
    register_repo(conn, repo_name, repo_name, f"/mnt/mirror/{repo_name}", "")
    conn.commit()
    conn.close()


def _write_catalog_with_packs(
    path: Path,
    repo_name: str = "family",
    pack_hashes: list[str] | None = None,
    volume_label: str = "VOL_001",
) -> list[str]:
    """Write a catalog.db with packs assigned to a volume."""
    if pack_hashes is None:
        pack_hashes = ["aa" * 32, "bb" * 32]
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    create_all(conn)
    register_repo(conn, repo_name, repo_name, f"/mnt/mirror/{repo_name}", "")
    packs = [register_pack(conn, sha, 1024, repo_name) for sha in pack_hashes]
    vol = create_volume(
        conn, volume_label, generate_uuid(), "TEST_TINY",
        1_000_000, "Home_Shelf", "VERIFIED",
    )
    bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in packs])
    conn.commit()
    conn.close()
    return pack_hashes


def _write_multivolume_catalog(
    path: Path,
    repo_name: str,
    layout: dict[str, list[str]],
) -> None:
    """Write a catalog.db spreading packs across multiple volumes.

    ``layout`` maps ``{volume_label: [pack_sha256, ...]}``.  Each pack is
    registered once and linked to exactly its one volume (single-copy
    layout — no alternates), reproducing the RST-01 multi-disc scenario.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    create_all(conn)
    register_repo(conn, repo_name, repo_name, f"/mnt/mirror/{repo_name}", "")
    for label, hashes in layout.items():
        packs = [register_pack(conn, sha, 1024, repo_name) for sha in hashes]
        vol = create_volume(
            conn, label, generate_uuid(), "TEST_TINY",
            1_000_000, "Home_Shelf", "VERIFIED",
        )
        bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in packs])
    conn.commit()
    conn.close()


def _write_multicopy_catalog(
    path: Path,
    repo_name: str,
    layout: dict[str, list[str]],
) -> None:
    """Write a catalog where a pack listed in two volumes gets an alternate.

    ``layout`` maps ``{volume_label: [sha256, ...]}``.  Each pack is
    registered once; volumes that share a pack become primary/alternate in
    the V2 pick list.  [RST-07]
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    create_all(conn)
    register_repo(conn, repo_name, repo_name, f"/mnt/mirror/{repo_name}", "")
    pack_ids: dict[str, int] = {}
    for hashes in layout.values():
        for sha in hashes:
            if sha not in pack_ids:
                pack_ids[sha] = register_pack(conn, sha, 1024, repo_name).pack_id
    for label, hashes in layout.items():
        vol = create_volume(
            conn, label, generate_uuid(), "TEST_TINY",
            1_000_000, "Home_Shelf", "VERIFIED",
        )
        bulk_link_packs(conn, vol.volume_id, [pack_ids[s] for s in hashes])
    conn.commit()
    conn.close()


def _place_pack(vol_data_dir: Path, sha256: str) -> None:
    """Write a placeholder pack file at the two-level layout location."""
    dst = vol_data_dir / sha256[:2] / sha256
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"pack-" + sha256.encode())


def _write_disc_metadata(meta_dir: Path) -> None:
    """Create the index/snapshots/keys subtree prepare_cache requires.

    A real RestoreExecutor.prepare_cache copies these three dirs from the
    disc into the cache; they only need to exist (contents are irrelevant
    when the rustic runner is faked).
    """
    for sub in ("index", "snapshots", "keys"):
        (meta_dir / sub).mkdir(parents=True, exist_ok=True)
        (meta_dir / sub / ".keep").write_bytes(b"")
    (meta_dir / "config").write_bytes(b"repo-config")


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestFromDiscParser:
    def test_from_disc_subcommand_registered(self):
        """Parser accepts 'restore standalone' without error."""
        parser = build_parser()
        args = parser.parse_args([
            "restore", "standalone",
            "/mnt/disc1", "/tmp/out",
            "--password-file", "/home/user/secret.key",
        ])
        assert args.command == "restore"
        assert args.restore_command == "standalone"
        assert args.disc == Path("/mnt/disc1")
        assert args.target_path == Path("/tmp/out")
        assert args.password_file == Path("/home/user/secret.key")

    def test_from_disc_snapshot_default(self):
        """--snapshot defaults to 'latest'."""
        parser = build_parser()
        args = parser.parse_args([
            "restore", "standalone",
            "/mnt/disc1", "/tmp/out",
            "--password-file", "/home/user/secret.key",
        ])
        assert args.snapshot == "latest"

    def test_from_disc_all_optional_flags(self):
        """All optional flags are accepted."""
        parser = build_parser()
        args = parser.parse_args([
            "restore", "standalone",
            "/mnt/disc1", "/tmp/out",
            "--password-file", "/home/user/secret.key",
            "--repo", "family",
            "--snapshot", "abc123def",
            "--volume-dir", "/media/vols",
            "--cache-dir", "/tmp/cache",
            "--skip-verify",
        ])
        assert args.repo == "family"
        assert args.snapshot == "abc123def"
        assert args.volume_dir == Path("/media/vols")
        assert args.cache_dir == Path("/tmp/cache")
        assert args.skip_verify is True


# ---------------------------------------------------------------------------
# Validation / early-exit tests
# ---------------------------------------------------------------------------


class TestFromDiscValidation:
    def test_disc_path_not_dir_returns_1(self, tmp_path):
        """Non-directory disc path returns exit code 1."""
        f = tmp_path / "notadir"
        f.write_text("x")
        args = _make_disc_args(disc=f)
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_no_catalog_db_returns_1(self, tmp_path):
        """Missing catalog.db returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        args = _make_disc_args(disc=disc)
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_no_repositories_returns_1(self, tmp_path):
        """A catalog with no repositories returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        # Create schema but don't insert any repos
        conn = sqlite3.connect(str(catalog))
        create_all(conn)
        conn.commit()
        conn.close()
        args = _make_disc_args(disc=disc)
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_multiple_repos_without_flag_returns_1(self, tmp_path):
        """Multiple repos without --repo returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        conn = sqlite3.connect(str(catalog))
        conn.row_factory = sqlite3.Row
        create_all(conn)
        register_repo(conn, "family", "family", "/mnt/mirror/family", "")
        register_repo(conn, "work", "work", "/mnt/mirror/work", "")
        conn.commit()
        conn.close()
        args = _make_disc_args(disc=disc, repo=None)
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_unknown_repo_name_returns_1(self, tmp_path):
        """--repo with a name not in the catalog returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        _write_catalog(catalog, repo_name="family")
        args = _make_disc_args(disc=disc, repo="nonexistent")
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_missing_metadata_dir_returns_1(self, tmp_path):
        """Missing metadata/<repo_name>/ on disc returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        _write_catalog(catalog, repo_name="family")
        # No metadata/ directory created
        args = _make_disc_args(disc=disc)
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_rustic_not_found_returns_1(self, tmp_path):
        """FileNotFoundError from restore_dry_run returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        _write_catalog(catalog, repo_name="family")
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        mock_runner = MagicMock()
        mock_runner.restore_dry_run.side_effect = FileNotFoundError("rustic not found")
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None

        args = _make_disc_args(disc=disc)
        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
        ):
            result = cmd_restore_from_disc(args)
        assert result == 1

    def test_interactive_no_tty_returns_1(self, tmp_path):
        """Non-interactive stdin returns exit code 1 in interactive mode."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        pack_hashes = _write_catalog_with_packs(catalog, repo_name="family")
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = pack_hashes
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None
        mock_executor.ingest_volume.return_value = IngestionResult(0, [])
        mock_executor.verify_cache_completeness = MagicMock(return_value=pack_hashes)

        args = _make_disc_args(disc=disc, volume_dir=None, skip_verify=True)
        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
            patch("lcsas.restore.executor.RestoreExecutor.verify_cache_completeness",
                  return_value=pack_hashes),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            result = cmd_restore_from_disc(args)
        assert result == 1


# ---------------------------------------------------------------------------
# Happy-path: batch mode with --volume-dir
# ---------------------------------------------------------------------------


class TestFromDiscBatchMode:
    def test_batch_restore_returns_0(self, tmp_path):
        """Batch restore with all packs available returns exit code 0."""
        disc = tmp_path / "disc"
        disc.mkdir()
        (disc / "data").mkdir()
        pack_hashes = _write_catalog_with_packs(
            disc / "catalog.db", repo_name="family"
        )
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        vol_dir = tmp_path / "vols"
        vol_dir.mkdir()

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = pack_hashes
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None
        mock_executor.ingest_volume.return_value = IngestionResult(len(pack_hashes), [])

        target = tmp_path / "restored"

        key_file = tmp_path / "secret.key"
        key_file.write_bytes(b"password")

        args = _make_disc_args(
            disc=disc,
            target_path=target,
            volume_dir=vol_dir,
            skip_verify=True,
            password_file=key_file,
        )
        with (
            patch("lcsas.utils.subprocess.check_binary_version", return_value="1.0.0"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
            patch("lcsas.restore.executor.RestoreExecutor.verify_cache_completeness",
                  return_value=[]),
        ):
            result = cmd_restore_from_disc(args)
        assert result == 0
        mock_executor.execute_restore.assert_called_once()

    def test_batch_multidisc_single_copy_returns_0(self, tmp_path):
        """RST-01 repro: a 2-pack/2-volume single-copy snapshot exits 0.

        Uses a REAL RestoreExecutor (only the rustic runner is faked) so
        the initial-disc ingest genuinely seeds pack B (which lives only
        on VOL_002) into all_failed, the per-volume loop ingests it, and
        the cache-prune must heal all_failed before the spurious raise.
        Before the fix this raised PackCorruptionError → exit 1.
        """
        pack_a = "aa" * 32
        pack_b = "bb" * 32

        # Initial --disc carries VOL_001's data (pack A).
        disc = tmp_path / "disc"
        (disc / "data").mkdir(parents=True)
        _write_multivolume_catalog(
            disc / "catalog.db", "family",
            {"VOL_001": [pack_a], "VOL_002": [pack_b]},
        )
        _write_disc_metadata(disc / "metadata" / "family")
        _place_pack(disc / "data", pack_a)

        # --volume-dir holds VOL_002's data (pack B) under its label dir.
        vol_dir = tmp_path / "vols"
        (vol_dir / "VOL_002").mkdir(parents=True)
        _place_pack(vol_dir / "VOL_002" / "data", pack_b)

        key_file = tmp_path / "secret.key"
        key_file.write_bytes(b"password")

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = [pack_a, pack_b]
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan

        args = _make_disc_args(
            disc=disc,
            target_path=tmp_path / "restored",
            volume_dir=vol_dir,
            skip_verify=True,
            password_file=key_file,
        )
        # Real RestoreExecutor; only the rustic runner is faked.
        with (
            patch("lcsas.utils.subprocess.check_binary_version", return_value="1.0.0"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
        ):
            result = cmd_restore_from_disc(args)
        assert result == 0
        mock_runner.restore.assert_called_once()

    def test_genuinely_missing_pack_error_names_disc_labels(self, tmp_path):
        """RST-01: a pack on no mounted volume still fails — and the raised
        PackCorruptionError names the catalog volume label (VOL_002), not
        only the SHA-256, so the operator knows which disc to re-mount.

        Real executor; pack B's data is never placed anywhere, so it stays
        unrecovered and the cache-prune cannot heal it.
        """
        from lcsas.restore.executor import PackCorruptionError

        pack_a = "aa" * 32
        pack_b = "bb" * 32

        disc = tmp_path / "disc"
        (disc / "data").mkdir(parents=True)
        _write_multivolume_catalog(
            disc / "catalog.db", "family",
            {"VOL_001": [pack_a], "VOL_002": [pack_b]},
        )
        _write_disc_metadata(disc / "metadata" / "family")
        _place_pack(disc / "data", pack_a)

        vol_dir = tmp_path / "vols"
        (vol_dir / "VOL_002" / "data").mkdir(parents=True)

        key_file = tmp_path / "secret.key"
        key_file.write_bytes(b"password")

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = [pack_a, pack_b]
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan

        args = _make_disc_args(
            disc=disc,
            target_path=tmp_path / "restored",
            volume_dir=vol_dir,
            skip_verify=True,
            password_file=key_file,
        )
        import pytest
        with (
            patch("lcsas.utils.subprocess.check_binary_version", return_value="1.0.0"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            pytest.raises(PackCorruptionError) as exc,
        ):
            cmd_restore_from_disc(args)
        assert "VOL_002" in str(exc.value)

    def test_interactive_multidisc_no_spurious_failure(self, tmp_path):
        """RST-01 interactive: supplying the second disc's mount path lets
        a 2-pack/2-volume restore complete cleanly (exit 0) with no
        spurious alternates prompt once the cache is complete.

        Real executor; only the rustic runner and input()/isatty are faked.
        """
        pack_a = "aa" * 32
        pack_b = "bb" * 32

        disc = tmp_path / "disc"
        (disc / "data").mkdir(parents=True)
        _write_multivolume_catalog(
            disc / "catalog.db", "family",
            {"VOL_001": [pack_a], "VOL_002": [pack_b]},
        )
        _write_disc_metadata(disc / "metadata" / "family")
        _place_pack(disc / "data", pack_a)

        # Second disc mounted at its own path; carries pack B.
        disc2 = tmp_path / "disc2"
        (disc2 / "data").mkdir(parents=True)
        _place_pack(disc2 / "data", pack_b)

        key_file = tmp_path / "secret.key"
        key_file.write_bytes(b"password")

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = [pack_a, pack_b]
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan

        args = _make_disc_args(
            disc=disc,
            target_path=tmp_path / "restored",
            volume_dir=None,  # interactive
            skip_verify=True,
            password_file=key_file,
        )

        prompts: list[str] = []

        def fake_input(prompt: str = "") -> str:
            prompts.append(prompt)
            return str(disc2)

        with (
            patch("lcsas.utils.subprocess.check_binary_version", return_value="1.0.0"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("builtins.input", side_effect=fake_input),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            result = cmd_restore_from_disc(args)

        assert result == 0
        mock_runner.restore.assert_called_once()
        # Exactly the VOL_002 mount prompt — no alternates prompt fired,
        # because the cache was already complete by the time the retry
        # guard ran (it never reaches _retry_from_alternates_interactive).
        assert not any("alternate" in p.lower() for p in prompts)

    def test_skip_primary_disc_triggers_alternate_prompt(self, tmp_path):
        """RST-07: skipping the primary disc routes its packs to the alternate
        retry, which prompts for the alternate disc and completes (exit 0).

        Real executor; only the rustic runner and input()/isatty are faked.
        """
        pack = "aa" * 32
        # Initial disc holds nothing extra; pack lives on VOL_A + VOL_B.
        disc = tmp_path / "disc"
        (disc / "data").mkdir(parents=True)
        _write_multicopy_catalog(
            disc / "catalog.db", "family",
            {"VOL_A": [pack], "VOL_B": [pack]},
        )
        _write_disc_metadata(disc / "metadata" / "family")

        # Alternate disc VOL_B carries the pack.
        disc_b = tmp_path / "disc_b"
        (disc_b / "data").mkdir(parents=True)
        _place_pack(disc_b / "data", pack)

        key_file = tmp_path / "secret.key"
        key_file.write_bytes(b"password")

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = [pack]
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan

        args = _make_disc_args(
            disc=disc, target_path=tmp_path / "restored",
            volume_dir=None, skip_verify=True, password_file=key_file,
        )

        prompts: list[str] = []

        def fake_input(prompt: str = "") -> str:
            prompts.append(prompt)
            # 'skip' the primary; mount VOL_B at the alternate prompt.
            return "skip" if "VOL_A" in prompt else str(disc_b)

        with (
            patch("lcsas.utils.subprocess.check_binary_version", return_value="1.0.0"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("builtins.input", side_effect=fake_input),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            result = cmd_restore_from_disc(args)

        assert result == 0
        mock_runner.restore.assert_called_once()
        assert any("VOL_B" in p for p in prompts)

    def test_skip_all_discs_error_names_labels(self, tmp_path):
        """RST-07: skipping every disc (including the alternate prompt) fails
        with a PackCorruptionError that names the primary disc *and* the
        redundant alternate, never only the bare SHA-256.
        """
        from lcsas.restore.executor import PackCorruptionError

        pack = "aa" * 32
        disc = tmp_path / "disc"
        (disc / "data").mkdir(parents=True)
        _write_multicopy_catalog(
            disc / "catalog.db", "family",
            {"VOL_A": [pack], "VOL_B": [pack]},
        )
        _write_disc_metadata(disc / "metadata" / "family")

        key_file = tmp_path / "secret.key"
        key_file.write_bytes(b"password")

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = [pack]
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan

        args = _make_disc_args(
            disc=disc, target_path=tmp_path / "restored",
            volume_dir=None, skip_verify=True, password_file=key_file,
        )

        import pytest
        with (
            patch("lcsas.utils.subprocess.check_binary_version", return_value="1.0.0"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("builtins.input", return_value="skip"),
            patch("sys.stdin") as mock_stdin,
            pytest.raises(PackCorruptionError) as exc,
        ):
            mock_stdin.isatty.return_value = True
            cmd_restore_from_disc(args)

        msg = str(exc.value)
        assert "VOL_A" in msg
        assert "VOL_B" in msg  # the redundant alternate is named
        assert pack not in msg
        mock_runner.restore.assert_not_called()

    def test_batch_missing_packs_raises(self, tmp_path):
        """Batch restore with permanently missing packs fails loud.

        Realistic IngestionResult shape (packs absent → in .failed); the
        cache-prune can't heal them, so PackCorruptionError propagates
        (main() maps it to exit 1).
        """
        from lcsas.restore.executor import PackCorruptionError

        disc = tmp_path / "disc"
        disc.mkdir()
        (disc / "data").mkdir()
        pack_hashes = _write_catalog_with_packs(
            disc / "catalog.db", repo_name="family"
        )
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        vol_dir = tmp_path / "vols"
        vol_dir.mkdir()

        key_file = tmp_path / "secret.key"
        key_file.write_bytes(b"password")

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = pack_hashes
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None
        # Realistic shape: packs genuinely absent from every volume come
        # back in IngestionResult.failed (not an empty list).  RST-01
        # de-masks this — the old empty-list mock hid the false-failure bug.
        mock_executor.ingest_volume.return_value = IngestionResult(0, pack_hashes)

        args = _make_disc_args(
            disc=disc,
            target_path=tmp_path / "restored",
            volume_dir=vol_dir,
            skip_verify=True,
            password_file=key_file,
        )
        import pytest
        # Genuinely-missing packs survive the cache-prune, so the function
        # raises PackCorruptionError (main() maps it to exit 1).  The
        # realistic IngestionResult shape exercises the raise path; the
        # old empty-list mock skipped it entirely.
        with (
            patch("lcsas.utils.subprocess.check_binary_version", return_value="1.0.0"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
            patch("lcsas.restore.executor.RestoreExecutor.verify_cache_completeness",
                  return_value=pack_hashes),
            pytest.raises(PackCorruptionError),
        ):
            cmd_restore_from_disc(args)
        mock_executor.execute_restore.assert_not_called()

    def test_single_repo_auto_selected(self, tmp_path):
        """Single repo in catalog is auto-selected without --repo flag."""
        disc = tmp_path / "disc"
        disc.mkdir()
        (disc / "data").mkdir()
        pack_hashes = _write_catalog_with_packs(
            disc / "catalog.db", repo_name="family"
        )
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        vol_dir = tmp_path / "vols"
        vol_dir.mkdir()

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = pack_hashes
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None
        mock_executor.ingest_volume.return_value = IngestionResult(len(pack_hashes), [])

        key_file = tmp_path / "secret.key"
        key_file.write_bytes(b"password")

        args = _make_disc_args(
            disc=disc,
            repo=None,  # no explicit --repo
            volume_dir=vol_dir,
            skip_verify=True,
            password_file=key_file,
        )
        with (
            patch("lcsas.utils.subprocess.check_binary_version", return_value="1.0.0"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
            patch("lcsas.restore.executor.RestoreExecutor.verify_cache_completeness",
                  return_value=[]),
        ):
            result = cmd_restore_from_disc(args)
        assert result == 0

    def test_custom_catalog_path_used(self, tmp_path):
        """--catalog overrides default catalog.db location on disc."""
        disc = tmp_path / "disc"
        disc.mkdir()
        (disc / "data").mkdir()
        custom_catalog = tmp_path / "custom_catalog.db"
        pack_hashes = _write_catalog_with_packs(custom_catalog, repo_name="family")
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        vol_dir = tmp_path / "vols"
        vol_dir.mkdir()

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = pack_hashes
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None
        mock_executor.ingest_volume.return_value = IngestionResult(len(pack_hashes), [])

        key_file = tmp_path / "secret.key"
        key_file.write_bytes(b"password")

        args = _make_disc_args(
            disc=disc,
            catalog=custom_catalog,
            volume_dir=vol_dir,
            skip_verify=True,
            password_file=key_file,
        )
        with (
            patch("lcsas.utils.subprocess.check_binary_version", return_value="1.0.0"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
            patch("lcsas.restore.executor.RestoreExecutor.verify_cache_completeness",
                  return_value=[]),
        ):
            result = cmd_restore_from_disc(args)
        assert result == 0
