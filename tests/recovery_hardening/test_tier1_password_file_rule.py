"""Tier 1 reads a password file as rustic does (#641).

The password is the file's first line: every byte before the first LF,
less one CR at its end.  Before #641 tier 1 took the whole file less its
trailing CR/LF bytes, so a repository written by rustic with a file of
two lines opened in tier 2 only.  The table is the one probed against
rustic 0.11.2 in tests/unit/test_password_file_rule.py; tier 3 and the
``lcsas password`` commands are checked against it there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.recovery_hardening.test_tier1_fault_handling import (
    _find_bin,
    _require_fixture,
    _run,
)
from tests.unit.test_password_file_rule import PW, RUSTIC_0_11_2

# The fixture repository's password (recovery/tests/fixtures/gen_fixture.py).
assert PW == b"test"


@pytest.mark.parametrize(("content", "opens"), RUSTIC_0_11_2)
def test_tier1_password_file_rule_matches_rustic(
    tmp_path: Path, content: bytes, opens: bool,
) -> None:
    bin_path = _find_bin()
    repo = _require_fixture()
    pw = tmp_path / "pw.key"
    pw.write_bytes(content)
    res = _run(bin_path, "--repo", str(repo), "--password-file", str(pw),
               "--list-snapshots", timeout=60)
    if opens:
        assert res.returncode == 0, res.stderr
    else:
        assert res.returncode == 77, (
            f"{content!r}: expected the wrong-password exit 77, got "
            f"{res.returncode}\n{res.stderr}"
        )
