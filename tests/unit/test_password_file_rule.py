"""One password-file rule for every reader (#641).

rustic (the writer, and tier 2) takes a password file's first line: every
byte before the first LF, less one CR at its end.  Spaces are kept.  Tier 1
(lcsas-restore, see tests/recovery_hardening/test_tier1_password_file_rule.py),
tier 3 (``read_password_file`` in restic_fallback.py) and the ``lcsas key``
commands must take the same password, or a repository written with a file of
two lines opens in tier 2 only.

RUSTIC_0_11_2 is what rustic 0.11.2 did with each file when the repository
password was ``PW`` (probed 2026-11-17: ``rustic -r repo --password-file F
snapshots``).  The tier-1 test runs the same table against its fixture.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from lcsas.cli.commands.key import _read_password_file
from lcsas.config.settings import password_file_lines_warning
from lcsas.restore.restic_fallback import (
    password_from_file_bytes,
    read_password_file,
)

PW = b"test"

#: (file content, whether rustic 0.11.2 opened the repository with it).
RUSTIC_0_11_2: list[tuple[bytes, bool]] = [
    (PW, True),
    (PW + b"\n", True),
    (PW + b"\r", True),
    (PW + b"\r\n", True),
    (PW + b"\n\n", True),
    (PW + b"\nsecond line\n", True),
    (PW + b"\r\nsecond line\r\n", True),
    (PW + b"\r\r\n", False),
    (PW + b"\r\r", False),
    (b" " + PW + b"\n", False),
    (PW + b" \n", False),
    (PW + b"\rjunk", False),
    (b"\n" + PW + b"\n", False),
]


@pytest.mark.parametrize(("content", "opens"), RUSTIC_0_11_2)
def test_tier3_rule_matches_rustic(content: bytes, opens: bool) -> None:
    assert (password_from_file_bytes(content) == PW) is opens


def test_rule_keeps_spaces_and_a_second_cr() -> None:
    assert password_from_file_bytes(b"pass word  \r\nx") == b"pass word  "
    assert password_from_file_bytes(b"pw\r\r\n") == b"pw\r"
    assert password_from_file_bytes(b"") == b""
    assert password_from_file_bytes(b"\nsecond") == b""


@pytest.mark.parametrize(("content", "opens"), RUSTIC_0_11_2)
def test_key_commands_use_the_same_rule(
    tmp_path: Path, content: bytes, opens: bool,
) -> None:
    f = tmp_path / "pw.key"
    f.write_bytes(content)
    assert (_read_password_file(f) == PW) is opens
    assert _read_password_file(f) == read_password_file(f)


@pytest.mark.parametrize(
    "content",
    [b"one-line", b"one-line\n", b"one-line\r\n", b"one-line\n\n\n",
     b"one-line\n  \n"],
)
def test_one_line_file_gets_no_warning(tmp_path: Path, content: bytes) -> None:
    f = tmp_path / "pw.key"
    f.write_bytes(content)
    assert password_file_lines_warning(f) is None


def test_two_line_base64_file_is_warned_about(tmp_path: Path) -> None:
    # What `openssl rand -base64 64` gives: 76 + 12 characters.
    f = tmp_path / "pw.key"
    f.write_bytes(b"A" * 76 + b"\n" + b"B" * 12 + b"\n")
    note = password_file_lines_warning(f)
    assert note is not None
    assert str(f) in note
    assert "only the first line" in note
    assert "EMPTY" not in note


def test_empty_first_line_is_called_out(tmp_path: Path) -> None:
    f = tmp_path / "pw.key"
    f.write_bytes(b"\nsecret\n")
    note = password_file_lines_warning(f)
    assert note is not None and "EMPTY" in note


def test_missing_file_gives_no_warning(tmp_path: Path) -> None:
    assert password_file_lines_warning(tmp_path / "absent.key") is None


def test_validate_config_logs_the_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from lcsas.config.settings import LCSASConfig, RepositoryConfig, validate_config

    mirror = tmp_path / "mirror"
    (mirror / "r1").mkdir(parents=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    key = tmp_path / "r1.key"
    key.write_bytes(b"first\nsecond\n")
    config = LCSASConfig(
        mirror_base_path=mirror,
        staging_path=staging,
        db_path=tmp_path / "archive.db",
        repositories={
            "r1": RepositoryConfig(
                name="r1", mirror_path=mirror / "r1", password_file=key,
            ),
        },
    )
    with caplog.at_level(logging.WARNING, logger="lcsas"):
        errors = validate_config(config)
    assert not [e for e in errors if "password_file" in e]
    assert any(
        "repo 'r1'" in r.getMessage() and "more than one line" in r.getMessage()
        for r in caplog.records
    )
