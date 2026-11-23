"""Tests for the 'lcsas restore from-disc' CLI command.

Since #948 the command is a front for the Python reader (tier 3):
these tests check what it hands the reader and how it reads the
disc catalog (read-only).  The restore itself, from real discs, is
in tests/integration/test_owner_restore_commands.py.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from lcsas.cli.main import build_parser, cmd_restore_from_disc
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_disc_args(**kwargs) -> argparse.Namespace:
    """Build a Namespace mimicking parsed 'restore from-disc' args."""
    defaults = {
        "command": "restore",
        "restore_command": "from-disc",
        "disc": Path("/mnt/disc1"),
        "restore_folder": Path("/tmp/restored"),
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


def _write_disc_metadata(meta_dir: Path) -> None:
    """Create the index/snapshots/keys subtree of a repository on a disc.

    Contents are irrelevant when the Python reader is faked; a snapshot
    file must exist (the command refuses a disc with none).
    """
    for sub in ("index", "snapshots", "keys"):
        (meta_dir / sub).mkdir(parents=True, exist_ok=True)
        (meta_dir / sub / ".keep").write_bytes(b"")
    (meta_dir / "config").write_bytes(b"repo-config")


def _password(tmp_path: Path) -> Path:
    pw = tmp_path / "secret.key"
    pw.write_text("password\n")
    return pw


def _disc(tmp_path: Path, repo_name: str = "family") -> Path:
    """A disc tree: catalog.db, data/, metadata/<repo>/ with a snapshot."""
    disc = tmp_path / "disc"
    (disc / "data").mkdir(parents=True)
    _write_catalog(disc / "catalog.db", repo_name=repo_name)
    _write_disc_metadata(disc / "metadata" / repo_name)
    return disc


def _run_reader(
    args: argparse.Namespace,
    *,
    tty: bool = False,
    side_effect: BaseException | None = None,
    failures: int = 0,
) -> tuple[int, MagicMock, MagicMock]:
    """Run the command with the Python reader replaced.

    Returns (exit code, the reader class mock, the reader instance).
    The reader itself is tested against real repositories and real
    discs in tests/integration/test_owner_restore_commands.py.
    """
    restorer = MagicMock()
    restorer.failures = failures
    restorer.restore.side_effect = side_effect
    with (
        patch("lcsas.restore.restic_fallback.PurePythonRestorer",
              return_value=restorer) as reader_cls,
        patch("sys.stdin") as stdin,
    ):
        stdin.isatty.return_value = tty
        rc = cmd_restore_from_disc(args)
    return rc, reader_cls, restorer


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestFromDiscParser:
    def test_from_disc_subcommand_registered(self):
        """Parser accepts 'restore from-disc' without error."""
        parser = build_parser()
        args = parser.parse_args([
            "restore", "from-disc",
            "/mnt/disc1", "/tmp/out",
            "--password-file", "/home/user/secret.key",
        ])
        assert args.command == "restore"
        assert args.restore_command == "from-disc"
        assert args.disc == Path("/mnt/disc1")
        assert args.restore_folder == Path("/tmp/out")
        assert args.password_file == Path("/home/user/secret.key")

    def test_from_disc_snapshot_default(self):
        """--snapshot defaults to 'latest'."""
        parser = build_parser()
        args = parser.parse_args([
            "restore", "from-disc",
            "/mnt/disc1", "/tmp/out",
            "--password-file", "/home/user/secret.key",
        ])
        assert args.snapshot == "latest"

    def test_from_disc_all_optional_flags(self):
        """All optional flags are accepted."""
        parser = build_parser()
        args = parser.parse_args([
            "restore", "from-disc",
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
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
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
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
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
        args = _make_disc_args(disc=disc, repo=None,
                               password_file=_password(tmp_path))
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_unknown_repo_name_returns_1(self, tmp_path):
        """--repo with a name not in the catalog returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        _write_catalog(catalog, repo_name="family")
        args = _make_disc_args(disc=disc, repo="nonexistent",
                               password_file=_password(tmp_path))
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_missing_metadata_dir_returns_1(self, tmp_path):
        """Missing metadata/<repo_name>/ on disc returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        _write_catalog(catalog, repo_name="family")
        # No metadata/ directory created
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_metadata_found_by_repo_id_not_name(self, tmp_path):
        """#743: discs hold metadata/<repo_id>/; a UUID id must be found."""
        disc = tmp_path / "disc"
        disc.mkdir()
        repo_id = "3f2a9c1e-7b4d-4e8a-9c2f-1a2b3c4d5e6f"
        conn = sqlite3.connect(str(disc / "catalog.db"))
        conn.row_factory = sqlite3.Row
        create_all(conn)
        register_repo(conn, repo_id, "family", "/mnt/mirror/family", "")
        conn.commit()
        conn.close()
        meta = disc / "metadata" / repo_id
        _write_disc_metadata(meta)

        args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
        rc, reader_cls, _ = _run_reader(args)
        assert rc == 0
        assert reader_cls.call_args.kwargs["repo_path"] == meta

    def test_no_snapshot_on_the_disc_returns_1(self, tmp_path, caplog):
        """A disc that carries no snapshot of the set says so in words."""
        disc = _disc(tmp_path)
        for f in (disc / "metadata" / "family" / "snapshots").iterdir():
            f.unlink()
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
        rc, reader_cls, _ = _run_reader(args)
        assert rc == 1
        reader_cls.assert_not_called()
        assert "carries no snapshot" in caplog.text

    def test_volume_dir_not_a_directory_returns_1(self, tmp_path):
        disc = _disc(tmp_path)
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path),
                               volume_dir=tmp_path / "absent")
        rc, reader_cls, _ = _run_reader(args)
        assert rc == 1
        reader_cls.assert_not_called()


# ---------------------------------------------------------------------------
# The disc catalog is read-only (#948)
# ---------------------------------------------------------------------------


class TestFromDiscReadOnlyCatalog:
    def test_read_only_disc_is_not_written(self, tmp_path):
        """An extracted disc has 0444 files: the catalog is read where it
        is, nothing is written beside it (no -wal/-shm, no copy)."""
        disc = _disc(tmp_path)
        before = sorted(p.name for p in disc.iterdir())
        (disc / "catalog.db").chmod(0o444)
        disc.chmod(0o555)
        try:
            args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
            rc, _, _ = _run_reader(args)
            after = sorted(p.name for p in disc.iterdir())
        finally:
            disc.chmod(0o755)
        assert rc == 0
        assert after == before

    def test_newer_catalog_schema_is_refused_in_words(self, tmp_path, caplog):
        from lcsas.db.schema import CURRENT_SCHEMA_VERSION

        disc = _disc(tmp_path)
        conn = sqlite3.connect(str(disc / "catalog.db"))
        conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                     (CURRENT_SCHEMA_VERSION + 1,))
        conn.commit()
        conn.close()
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
        rc, reader_cls, _ = _run_reader(args)
        assert rc == 1
        reader_cls.assert_not_called()
        assert f"schema v{CURRENT_SCHEMA_VERSION + 1}" in caplog.text
        assert "newer LCSAS" in caplog.text

    def test_damaged_catalog_is_refused_in_words(self, tmp_path, caplog):
        disc = _disc(tmp_path)
        (disc / "catalog.db").write_bytes(b"not a database" * 100)
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
        rc, reader_cls, _ = _run_reader(args)
        assert rc == 1
        reader_cls.assert_not_called()
        assert "another disc" in caplog.text
        assert "password" not in caplog.text.lower()


# ---------------------------------------------------------------------------
# The job goes to the Python reader's own disc loop (#948)
# ---------------------------------------------------------------------------


class TestFromDiscHandsTheReader:
    def test_no_rustic_is_used(self, tmp_path):
        """from-disc plans nothing with rustic: the tree packs of a
        multi-disc archive are on discs not read yet."""
        disc = _disc(tmp_path)
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner") as runner_cls,
            patch("lcsas.utils.subprocess.check_binary_version") as check,
        ):
            rc, _, _ = _run_reader(args)
        assert rc == 0
        runner_cls.assert_not_called()
        check.assert_not_called()

    def test_disc_alone_is_the_search_path(self, tmp_path):
        disc = _disc(tmp_path)
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
        _, reader_cls, restorer = _run_reader(args, tty=False)
        kw = reader_cls.call_args.kwargs
        assert kw["pack_search_paths"] == [disc]
        assert kw["catalog_path"] == disc / "catalog.db"
        assert kw["interactive"] is False  # nobody at the terminal
        assert kw["pack_cache_dir"] is None
        assert restorer.restore.call_args.kwargs["snapshot_id"] is None

    def test_volume_dir_is_searched_too_without_prompts(self, tmp_path):
        disc = _disc(tmp_path)
        vols = tmp_path / "vols"
        vols.mkdir()
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path),
                               volume_dir=vols)
        _, reader_cls, _ = _run_reader(args, tty=True)
        kw = reader_cls.call_args.kwargs
        assert kw["pack_search_paths"] == [disc, vols]
        assert kw["interactive"] is False

    def test_at_the_terminal_the_reader_asks_for_discs(self, tmp_path):
        disc = _disc(tmp_path)
        args = _make_disc_args(disc=disc, password_file=_password(tmp_path))
        _, reader_cls, _ = _run_reader(args, tty=True)
        assert reader_cls.call_args.kwargs["interactive"] is True

    def test_custom_catalog_and_cache_and_snapshot(self, tmp_path):
        """--catalog names the discs, --cache-dir is the reader's pack
        cache, --snapshot is passed on."""
        disc = _disc(tmp_path)
        custom = tmp_path / "custom_catalog.db"
        _write_catalog(custom, repo_name="family")
        (disc / "catalog.db").unlink()
        cache = tmp_path / "cache"
        args = _make_disc_args(
            disc=disc, password_file=_password(tmp_path), catalog=custom,
            cache_dir=cache, snapshot="abc123",
        )
        rc, reader_cls, restorer = _run_reader(args)
        assert rc == 0
        kw = reader_cls.call_args.kwargs
        assert kw["catalog_path"] == custom
        assert kw["pack_cache_dir"] == cache
        assert restorer.restore.call_args.kwargs["snapshot_id"] == "abc123"

    def test_single_repo_auto_selected(self, tmp_path):
        """Single repo in catalog is auto-selected without --repo flag."""
        disc = _disc(tmp_path)
        args = _make_disc_args(disc=disc, repo=None,
                               password_file=_password(tmp_path))
        rc, reader_cls, _ = _run_reader(args)
        assert rc == 0
        assert reader_cls.call_args.kwargs["repo_path"] == (
            disc / "metadata" / "family")

    def test_prompt_answers_reach_the_reader(self, tmp_path):
        """The prompt callable answers the reader's disc prompt; the end
        of input is an empty line, which stops the reader."""
        from lcsas.restore.orchestrator import restore_from_disc

        disc = _disc(tmp_path)
        answers = iter(["", "q"])

        def prompt(_text: str) -> str:
            try:
                return next(answers)
            except StopIteration:
                raise EOFError from None

        with (
            patch("lcsas.restore.restic_fallback.PurePythonRestorer")
            as reader_cls,
        ):
            reader_cls.return_value.failures = 0
            restore_from_disc(
                disc, tmp_path / "out", _password(tmp_path),
                prompt=prompt, stdin_is_tty=lambda: True,
            )
        read_line = reader_cls.call_args.kwargs["prompt_in"]
        assert [read_line(), read_line(), read_line()] == ["\n", "q\n", ""]


# ---------------------------------------------------------------------------
# Exit codes: those of the bundled python_reader.py
# ---------------------------------------------------------------------------


class TestFromDiscPythonReader:
    """'restore from-disc' runs the Python reader.  Its exit codes match
    the bundled python_reader.py (#658, #740, #805, #875): 75 when the
    reader typed q (or input ended) at the disc prompt, 77 when a key
    file rejected the password, 4 when some items were not restored, 1
    for a restore that could not start."""

    @staticmethod
    def _run(tmp_path, restore_side_effect, failures=0):
        disc = _disc(tmp_path)
        args = _make_disc_args(
            disc=disc, restore_folder=tmp_path / "out",
            cache_dir=tmp_path / "cache", password_file=_password(tmp_path),
        )
        rc, _, _ = _run_reader(args, side_effect=restore_side_effect,
                               failures=failures)
        return rc

    def test_quit_at_disc_prompt_exits_75(self, tmp_path, caplog):
        from lcsas.restore.restic_fallback import (
            EXIT_READER_QUIT,
            RestoreAbortedError,
        )

        rc = self._run(tmp_path, RestoreAbortedError(
            "stopped at the disc prompt.  Files restored so far are kept.",
            exit_code=EXIT_READER_QUIT,
        ))
        assert rc == 75
        assert "stopped at the disc prompt" in caplog.text

    def test_full_disk_exits_1(self, tmp_path, caplog):
        from lcsas.restore.restic_fallback import RestoreAbortedError

        rc = self._run(tmp_path, RestoreAbortedError("disk full",
                                                     exit_code=1))
        assert rc == 1
        assert "Python reader restore stopped: disk full" in caplog.text

    def test_setup_failure_exits_1(self, tmp_path, caplog):
        rc = self._run(tmp_path, ValueError("Snapshot not found: 'abc'"))
        assert rc == 1
        assert "Python reader restore failed: Snapshot not found" in caplog.text
        assert "wrong password" not in caplog.text

    def test_rejected_password_exits_77(self, tmp_path, caplog):
        from lcsas.restore.restic_fallback import NoKeyUnlockedError

        rc = self._run(tmp_path, NoKeyUnlockedError(
            "no key file accepted the password", [tmp_path / "k"], [],
        ))
        assert rc == 77
        assert "wrong password" in caplog.text

    def test_some_items_not_restored_exits_4(self, tmp_path, caplog):
        rc = self._run(tmp_path, None, failures=2)
        assert rc == 4
        assert "RESTORE_FAILURES.txt" in caplog.text
