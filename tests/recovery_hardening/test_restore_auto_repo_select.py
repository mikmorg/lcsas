"""test_restore_auto_repo_select.py -- restore_auto.sh per-repo selection.

Issue #373: the non-interactive restore_auto.sh loops over every repo
discovered under the recovery tree and calls restore.sh for each, but it
did not tell restore.sh WHICH repo to restore.  On a multi-tenant archive
restore.sh would then hit its interactive selection prompt (EOF → fail)
or restore the same repo every loop.  The fix exports LCSAS_REPO=<name>
for each iteration so restore.sh restores the matching tenant.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_AUTO = REPO_ROOT / "recovery" / "scripts" / "restore_auto.sh"


def test_restore_auto_selects_each_repo_via_lcsas_repo(tmp_path: Path) -> None:
    recovery = tmp_path / "recovery"
    (recovery / "scripts").mkdir(parents=True)
    for repo in ("alpha", "bravo"):
        (recovery / "repos" / repo / "keys").mkdir(parents=True)

    record = tmp_path / "calls.txt"
    # Stub restore.sh: record the LCSAS_REPO it was invoked with, exit 0.
    stub = recovery / "scripts" / "restore.sh"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "LCSAS_REPO=%s\\n" "${LCSAS_REPO:-UNSET}" >> "$RECORD"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    pwfile = tmp_path / "pw"
    pwfile.write_text("secret\n")
    target = tmp_path / "restored"

    env = {
        **os.environ,
        "LCSAS_PWFILE": str(pwfile),
        "RECORD": str(record),
    }
    res = subprocess.run(
        ["sh", str(RESTORE_AUTO), str(recovery), str(target)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert res.returncode == 0, f"stderr:\n{res.stderr}"

    lines = [ln for ln in record.read_text().splitlines() if ln.strip()]
    seen = sorted(ln.removeprefix("LCSAS_REPO=") for ln in lines)
    assert seen == ["alpha", "bravo"], (
        f"each repo must be restored once with its own LCSAS_REPO; got {lines}"
    )
    assert "UNSET" not in record.read_text(), (
        "restore.sh was invoked without LCSAS_REPO — it would re-resolve the "
        "repo itself (prompt/EOF on a multi-tenant archive, issue #373)"
    )


def test_restore_auto_keeps_going_after_wrong_password(tmp_path: Path) -> None:
    """#384: repos have independent keys, so one shared LCSAS_PWFILE may
    be right for some tenants and wrong for others.  A per-repo tier-1
    exit 77 (wrong password for THAT repo) must NOT abort the whole run —
    restore_auto.sh reports it distinctly and still attempts the
    remaining repos.  Overall exit is 1 (at least one repo failed)."""
    recovery = tmp_path / "recovery"
    (recovery / "scripts").mkdir(parents=True)
    for repo in ("alpha", "bravo", "charlie"):
        (recovery / "repos" / repo / "keys").mkdir(parents=True)

    record = tmp_path / "calls.txt"
    # Stub restore.sh: record every repo it was asked to restore, then
    # exit 77 for 'bravo' (wrong password) and 0 otherwise.
    stub = recovery / "scripts" / "restore.sh"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "${LCSAS_REPO:-UNSET}" >> "$RECORD"\n'
        'if [ "$LCSAS_REPO" = "bravo" ]; then exit 77; fi\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    pwfile = tmp_path / "pw"
    pwfile.write_text("secret\n")
    target = tmp_path / "restored"

    env = {
        **os.environ,
        "LCSAS_PWFILE": str(pwfile),
        "RECORD": str(record),
    }
    res = subprocess.run(
        ["sh", str(RESTORE_AUTO), str(recovery), str(target)],
        capture_output=True, text=True, env=env, timeout=30,
    )

    # Overall failure because one repo failed...
    assert res.returncode == 1, (
        f"one failed repo should make the run exit 1; got {res.returncode}\n"
        f"stderr:\n{res.stderr}"
    )
    # ...but ALL THREE repos were still attempted (77 did not abort).
    attempted = sorted(
        ln for ln in record.read_text().splitlines() if ln.strip()
    )
    assert attempted == ["alpha", "bravo", "charlie"], (
        f"a wrong password on one repo aborted the loop; attempted={attempted}"
    )
    # And the wrong-password repo is named distinctly.
    assert "bravo" in res.stderr and "wrong password" in res.stderr.lower(), (
        f"restore_auto.sh did not report bravo's wrong password:\n{res.stderr}"
    )
