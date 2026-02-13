"""Tests for rustic/wrapper.py — SubprocessRusticRunner."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lcsas.rustic.wrapper import SubprocessRusticRunner


@pytest.fixture
def runner():
    return SubprocessRusticRunner(rustic_binary="restic")


@pytest.fixture
def custom_runner():
    return SubprocessRusticRunner(rustic_binary="/usr/local/bin/custom_restic")


REPO = Path("/tmp/test_repo")
PW = Path("/tmp/password.txt")


class TestRun:
    """Test the internal _run method command construction."""

    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_basic_command_construction(self, mock_run, runner):
        """Verify correct command line assembly."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        runner._run(["snapshots", "--json"], REPO, PW)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["restic", "-r", str(REPO), "--password-file", str(PW), "snapshots", "--json"]

    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_custom_binary(self, mock_run, custom_runner):
        """Custom binary path is used in command."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        custom_runner._run(["init"], REPO, PW)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/custom_restic"

    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_check_raises_on_failure(self, mock_run, runner):
        """CalledProcessError propagates when check=True."""
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "restic", stderr="fatal: unable to open repo"
        )
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            runner._run(["init"], REPO, PW, check=True)
        assert "unable to open repo" in str(exc_info.value.stderr)

    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_capture_output(self, mock_run, runner):
        """verify capture_output=True and text=True are passed."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="output", stderr=""
        )
        result = runner._run(["snapshots"], REPO, PW)
        kwargs = mock_run.call_args[1]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert result.stdout == "output"


class TestInitRepo:
    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_init_basic(self, mock_run, runner):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        runner.init_repo(REPO, PW)

        cmd = mock_run.call_args[0][0]
        assert "init" in cmd
        assert "--repo-hot" not in cmd

    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_init_with_hot_repo(self, mock_run, runner):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        hot = Path("/tmp/hot_repo")
        runner.init_repo(REPO, PW, hot_repo_path=hot)

        cmd = mock_run.call_args[0][0]
        assert "--repo-hot" in cmd
        assert str(hot) in cmd


class TestBackup:
    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_backup_command_and_parsing(self, mock_run, runner):
        """Backup calls --json and parses output."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"snapshot_id":"abc123","files_new":5,"data_added":1024}',
            stderr="",
        )
        result = runner.backup([Path("/data")], REPO, PW)

        cmd = mock_run.call_args[0][0]
        assert "backup" in cmd
        assert "--json" in cmd
        assert "/data" in cmd
        assert result.snapshot_id == "abc123"
        assert result.files_new == 5


class TestSnapshots:
    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_snapshots_parse(self, mock_run, runner):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='[{"id":"snap1","time":"2026-01-01","hostname":"box","paths":["/home"],"tags":[]}]',
            stderr="",
        )
        snaps = runner.snapshots(REPO, PW)
        assert len(snaps) == 1
        assert snaps[0].snapshot_id == "snap1"


class TestRestoreDryRun:
    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_restore_dry_run_command(self, mock_run, runner):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"packs":["p1","p2"],"total_size":4096,"file_count":10}',
            stderr="",
        )
        plan = runner.restore_dry_run("snap1", REPO, PW)

        cmd = mock_run.call_args[0][0]
        assert "restore" in cmd
        assert "--dry-run" in cmd
        assert "--json" in cmd
        assert plan.required_pack_hashes == ["p1", "p2"]
        assert plan.total_size_bytes == 4096


class TestRestore:
    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_restore_command(self, mock_run, runner):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        target = Path("/restore/target")
        runner.restore("snap1", REPO, PW, target)

        cmd = mock_run.call_args[0][0]
        assert "restore" in cmd
        assert "snap1" in cmd
        assert str(target) in cmd


class TestPruneDryRun:
    @patch("lcsas.rustic.wrapper.subprocess.run")
    def test_prune_dry_run(self, mock_run, runner):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"packs_to_delete":["p1"],"space_freed":65536}',
            stderr="",
        )
        result = runner.prune_dry_run(REPO, PW)

        cmd = mock_run.call_args[0][0]
        assert "prune" in cmd
        assert "--dry-run" in cmd
        assert "--json" in cmd
        assert result.packs_to_delete == ["p1"]
        assert result.space_freed_bytes == 65536
