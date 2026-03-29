"""Unit tests for SubprocessRunnerBase (lcsas.utils.subprocess)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lcsas.utils.subprocess import SubprocessRunnerBase


class TestCheckBinary:
    def test_raises_when_binary_not_on_path(self):
        runner = SubprocessRunnerBase("__nonexistent_binary_xyz__")
        with pytest.raises(RuntimeError, match="not found on PATH"):
            runner.check_binary()

    def test_passes_when_binary_exists(self):
        runner = SubprocessRunnerBase("python3")
        runner.check_binary()  # Should not raise

    def test_error_message_contains_binary_name(self):
        runner = SubprocessRunnerBase("my_missing_tool")
        with pytest.raises(RuntimeError, match="my_missing_tool"):
            runner.check_binary()


class TestEnv:
    def test_env_is_none_when_no_tmpdir(self):
        runner = SubprocessRunnerBase("tool")
        assert runner._env() is None

    def test_env_sets_tmpdir(self, tmp_path: Path):
        runner = SubprocessRunnerBase("tool", tmpdir=tmp_path)
        env = runner._env()
        assert env is not None
        assert env["TMPDIR"] == str(tmp_path)

    def test_env_inherits_existing_environ(self, tmp_path: Path):
        runner = SubprocessRunnerBase("tool", tmpdir=tmp_path)
        with patch.dict(os.environ, {"CUSTOM_VAR": "hello"}):
            env = runner._env()
        assert env is not None
        assert env.get("CUSTOM_VAR") == "hello"


class TestLogStderr:
    def test_logs_each_stderr_line(self, caplog):
        import logging

        exc = subprocess.CalledProcessError(1, "mytool", stderr="line one\nline two")
        with caplog.at_level(logging.ERROR, logger="lcsas.utils.subprocess"):
            SubprocessRunnerBase._log_stderr("mytool", exc)
        messages = [r.message for r in caplog.records]
        assert any("line one" in m for m in messages)
        assert any("line two" in m for m in messages)

    def test_no_log_when_stderr_empty(self, caplog):
        import logging

        exc = subprocess.CalledProcessError(1, "mytool", stderr="")
        with caplog.at_level(logging.ERROR, logger="lcsas.utils.subprocess"):
            SubprocessRunnerBase._log_stderr("mytool", exc)
        assert caplog.records == []

    def test_no_log_when_stderr_none(self, caplog):
        import logging

        exc = subprocess.CalledProcessError(1, "mytool", stderr=None)
        with caplog.at_level(logging.ERROR, logger="lcsas.utils.subprocess"):
            SubprocessRunnerBase._log_stderr("mytool", exc)
        assert caplog.records == []


class TestInit:
    def test_binary_stored(self):
        runner = SubprocessRunnerBase("xorriso")
        assert runner._binary == "xorriso"

    def test_tmpdir_stored(self, tmp_path: Path):
        runner = SubprocessRunnerBase("xorriso", tmpdir=tmp_path)
        assert runner._tmpdir == tmp_path

    def test_tmpdir_defaults_to_none(self):
        runner = SubprocessRunnerBase("xorriso")
        assert runner._tmpdir is None
