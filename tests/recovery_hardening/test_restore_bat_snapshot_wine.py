r"""restore.bat --snapshot ID, and LCSAS_BACKUP_SET with one set, under wine (#807, #802).

* ``restore.bat --snapshot ID`` restores that snapshot (tier 1 gets
  ``--snapshot ID``, not ``latest``); an ID with anything but 0-9 and
  a-f is refused with exit 2 before any question.
* LCSAS_BACKUP_SET naming no set stops with the list of sets, with ONE set
  too (restore.sh restored the lone set whatever the name, #802).

Coarse, like test_restore_bat_wine_smoke (whose prefix helpers this
uses); the truth source is windows-e2e on a real runner.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import non_marker_files
from tests.recovery_hardening.test_restore_bat_wine_smoke import (
    REPO_ROOT,
    RESTORE_EXE,
    TIMEOUT,
    _init_prefix,
    _make_meta_tree,
    _map_drive,
    _require_working_wineboot,  # noqa: F401  (autouse: skip a stuck wine)
    _stop_test_wineserver,  # noqa: F401  (autouse: remove each prefix)
    pytestmark,  # noqa: F401  (the wine/skip marks apply here too)
)


def _run(env: dict[str, str], args: list[str], stdin_data: str,
         **extra: str) -> subprocess.CompletedProcess[str]:
    run_env = {
        **env,
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_PLATFORM": "x86_64-pc-windows-gnu",
        "LCSAS_NEWEST_DISC_PROMPT": "0",
        **extra,
    }
    return subprocess.run(
        ["wine", "cmd", "/c", "E:\\recovery\\scripts\\restore.bat", *args],
        input=stdin_data, env=run_env, capture_output=True, text=True,
        timeout=TIMEOUT,
    )


def _built_meta(tmp_path: Path) -> tuple[dict[str, str], Path, str]:
    """E: = a meta tree whose set holds one snapshot of proof.txt
    (password "test"); returns the env, the tree and the snapshot id."""
    if not RESTORE_EXE.is_file():
        pytest.skip(f"{RESTORE_EXE} not present")
    sys.path.insert(0, str(REPO_ROOT / "recovery" / "tests"))
    import test_e2e  # sibling fixture builder

    repo = tmp_path / "repo-src"
    snap = test_e2e.build_repo(repo, "test", {"proof.txt": b"snapshot proof\n"})
    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)
    meta = tmp_path / "meta"
    _make_meta_tree(meta, repo_src=repo)
    _map_drive(prefix, "e", meta)
    return env, meta, snap


def test_snapshot_flag_restores_that_snapshot(tmp_path: Path) -> None:
    env, meta, snap = _built_meta(tmp_path)
    res = _run(env, ["--snapshot", snap[:8]], "E:\\restored\n", LCSAS_PW="test")
    out = res.stdout + res.stderr
    assert res.returncode == 0, f"rc={res.returncode}\n{out}"
    assert "proof.txt" in {p.name for p in non_marker_files(meta / "restored")}, out


def test_snapshot_flag_reaches_tier_1(tmp_path: Path) -> None:
    """An id the set has no snapshot for: tier 1 is asked for it and
    fails, so the restore does not finish (with latest it would)."""
    env, meta, snap = _built_meta(tmp_path)
    assert not snap.startswith("0000aaaa")
    res = _run(env, ["--snapshot", "0000aaaa"], "E:\\restored\n", LCSAS_PW="test")
    out = res.stdout + res.stderr
    assert "[tier 1] running" in out, out
    assert res.returncode != 0, out
    assert "Files restored to" not in out, out
    restored = meta / "restored"
    assert not restored.exists() or not non_marker_files(restored), out


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
    res = _run(env, [], "\n\n\n", LCSAS_BACKUP_SET="no-such-set")
    out = res.stdout + res.stderr
    assert res.returncode == 1, f"rc={res.returncode}\n{out}"
    assert "is not among the backup sets" in out, out
    assert "[1] repo" in out, out
    assert "Restore to which folder" not in out, out
