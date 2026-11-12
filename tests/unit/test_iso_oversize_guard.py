"""Guard against ≥4 GiB files at the ISO mastering choke point (FMT-04).

ISO 9660 Level 3 stores a file larger than 4 GiB − 2 KiB as multiple
extents, which Windows' native CDFS mount silently truncates.
``create_iso`` must reject such a file by name *before* spawning any
xorriso process.

The oversize file is created sparse (``os.truncate``) so the test uses no
real disk space; pytest tmp lives under /var/tmp/pytest-lcsas.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from lcsas.config.media import ISO_MAX_FILE_BYTES
from lcsas.iso.xorriso import (
    OversizeFileError,
    SubprocessXorrisoRunner,
)


def _make_sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        os.truncate(fh.fileno(), size)


class TestCreateIsoOversizeGuard:
    def test_rejects_oversize_file_before_spawn(self, tmp_path):
        source = tmp_path / "source"
        offender = source / "data" / "ab" / "cd" / "bighash"
        _make_sparse(offender, ISO_MAX_FILE_BYTES + 1)
        runner = SubprocessXorrisoRunner()

        with patch("lcsas.iso.xorriso.subprocess.Popen") as mock_popen:
            with pytest.raises(OversizeFileError) as exc:
                runner.create_iso(source, tmp_path / "out.iso", "VOL")
            mock_popen.assert_not_called()

        assert "bighash" in str(exc.value)

    def test_exact_limit_passes_guard(self, tmp_path):
        """A file of exactly ISO_MAX_FILE_BYTES is single-extent and allowed."""
        source = tmp_path / "source"
        ok_file = source / "data" / "ok"
        _make_sparse(ok_file, ISO_MAX_FILE_BYTES)
        runner = SubprocessXorrisoRunner()

        captured: list[str] = []

        def _popen_factory(cmd, **kwargs):
            captured.extend(cmd)
            idx = cmd.index("-o")
            Path(cmd[idx + 1]).write_bytes(b"ISO")
            from unittest.mock import MagicMock
            proc = MagicMock()
            proc.communicate.return_value = ("", "")
            proc.returncode = 0
            return proc

        with patch("lcsas.iso.xorriso.subprocess.Popen", side_effect=_popen_factory):
            runner.create_iso(source, tmp_path / "out.iso", "VOL")
        assert "-as" in captured  # xorriso was actually invoked

