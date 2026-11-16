r"""Hardening (#807, #802): restore.bat --snapshot ID, and LCSAS_REPO with
one backup set, under wine's cmd.

* ``restore.bat --snapshot ID`` restores that snapshot (tier 1 gets
  ``--snapshot ID``, not ``latest``); an ID with anything but 0-9 and
  a-f is refused with exit 2 before any question.
* LCSAS_REPO naming no set stops with the list of sets, with ONE set
  too (restore.sh restored the lone set whatever the name, #802).

Coarse, like test_restore_bat_wine_smoke (whose prefix helpers this
uses); the truth source is windows-e2e on a real runner.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.recovery_hardening.test_restore_bat_wine_smoke import (
    REPO_ROOT,
    TIMEOUT,
    _fixture_meta,
    _init_prefix,
    _make_meta_tree,
    _map_drive,
    _require_working_wineboot,  # noqa: F401  (autouse: skip a stuck wine)
    _stop_test_wineserver,  # noqa: F401  (autouse: remove each prefix)
    pytestmark,  # noqa: F401  (the wine/skip marks apply here too)
)

FIXTURE_SNAPSHOTS = REPO_ROOT / "recovery" / "tests" / "fixtures" / "repo" / "snapshots"


def _run(env: dict[str, str], args: list[str], stdin_data: str,
         **extra: str) -> subprocess.CompletedProcess[str]:
    run_env = {
        **env,
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_TARGET": "x86_64-pc-windows-gnu",
        "LCSAS_NEWEST_DISC_PROMPT": "0",
        **extra,
    }
    return subprocess.run(
        ["wine", "cmd", "/c", "E:\\recovery\\scripts\\restore.bat", *args],
        input=stdin_data, env=run_env, capture_output=True, text=True,
        timeout=TIMEOUT,
    )


def _a_snapshot_id() -> str:
    ids = sorted(p.name for p in FIXTURE_SNAPSHOTS.iterdir()) if FIXTURE_SNAPSHOTS.is_dir() else []
    if not ids:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    return ids[0]


def test_snapshot_flag_restores_that_snapshot(tmp_path: Path) -> None:
    env, meta = _fixture_meta(tmp_path)
    snap = _a_snapshot_id()[:8]
    res = _run(env, ["--snapshot", snap], "E:\\restored\n", LCSAS_PW="test")
    out = res.stdout + res.stderr
    assert res.returncode == 0, f"rc={res.returncode}\n{out}"
    assert "Files restored to: E:\\restored" in out, out


def test_snapshot_flag_reaches_tier_1(tmp_path: Path) -> None:
    """An id no snapshot has: tier 1 is asked for it and fails, so the
    restore does not finish (with `latest` it would)."""
    env, _meta = _fixture_meta(tmp_path)
    res = _run(env, ["--snapshot", "0000aaaa"], "E:\\restored\n\n\n\n", LCSAS_PW="test")
    out = res.stdout + res.stderr
    assert "[tier 1] running" in out, out
    assert res.returncode != 0, out
    assert "Files restored to" not in out, out


@pytest.mark.parametrize("snap", ["a1b2&echo HACKED", "not-hex", ""])
def test_a_bad_snapshot_id_exits_2_before_any_question(tmp_path: Path, snap: str) -> None:
    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)
    meta = tmp_path / "meta"
    _make_meta_tree(meta)
    _map_drive(prefix, "e", meta)
    args = ["--snapshot", snap] if snap else ["--snapshot"]
    res = _run(env, args, "\n\n\n")
    out = res.stdout + res.stderr
    assert res.returncode == 2, f"rc={res.returncode}\n{out}"
    assert "--snapshot needs a snapshot id" in out, out
    assert "HACKED" not in out, out
    assert "Restore to which folder" not in out, out


def test_an_unknown_lcsas_repo_with_one_set_is_refused(tmp_path: Path) -> None:
    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)
    meta = tmp_path / "meta"
    _make_meta_tree(meta)
    _map_drive(prefix, "e", meta)
    res = _run(env, [], "\n\n\n", LCSAS_REPO="no-such-set")
    out = res.stdout + res.stderr
    assert res.returncode == 1, f"rc={res.returncode}\n{out}"
    assert "is not among the backup sets" in out, out
    assert "[1] repo" in out, out
    assert "Restore to which folder" not in out, out
