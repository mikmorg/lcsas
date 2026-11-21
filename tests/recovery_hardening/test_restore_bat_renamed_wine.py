r"""restore.bat: the retired names fail, --restore-folder works, under wine (#934).

* ``--repo`` and ``--target`` stop with exit 2 and
  ``restore.bat: <old> was renamed: use <new>``; so do the set
  variables LCSAS_REPO, LCSAS_PWFILE, LCSAS_TARGET and
  LCSAS_FORCE_NONEMPTY_TARGET; LCSAS_PW (dropped) says "removed".  All of it
  at the top level of the script, before the first question.
* ``--restore-folder DIR`` is the answer to the folder question: it is
  not asked, and the files land in DIR.

Coarse, like test_restore_bat_snapshot_wine (whose helpers this uses);
the truth source is windows-e2e on a real runner.
"""

from __future__ import annotations

import pytest

from tests.recovery_hardening._diff_helpers import non_marker_files
from tests.recovery_hardening.test_restore_bat_snapshot_wine import (
    _built_meta,
    _init_prefix,
    _make_meta_tree,
    _map_drive,
    _require_working_wineboot,  # noqa: F401  (autouse: skip a stuck wine)
    _run,
    _stop_test_wineserver,  # noqa: F401  (autouse: remove each prefix)
    pytestmark,  # noqa: F401  (the wine/skip marks apply here too)
)


def _bare_meta(tmp_path):
    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)
    meta = tmp_path / "meta"
    _make_meta_tree(meta)
    _map_drive(prefix, "e", meta)
    return env, meta


@pytest.mark.parametrize("args,old,new", [
    (["--repo", "x"], "--repo", "--backup-set"),
    (["--target", "E:\\restored"], "--target", "--restore-folder"),
])
def test_an_old_flag_exits_2_with_the_new_name(tmp_path, args, old, new) -> None:
    env, _ = _bare_meta(tmp_path)
    res = _run(env, args, "\n\n\n")
    out = res.stdout + res.stderr
    assert res.returncode == 2, f"rc={res.returncode}\n{out}"
    assert f"restore.bat: {old} was renamed: use {new}" in out, out
    assert "Restore to which folder" not in out, out


@pytest.mark.parametrize("var,verb,new", [
    ("LCSAS_REPO", "renamed", "LCSAS_BACKUP_SET"),
    ("LCSAS_PWFILE", "renamed", "LCSAS_PASSWORD_FILE"),
    ("LCSAS_TARGET", "renamed", "LCSAS_PLATFORM"),
    ("LCSAS_FORCE_NONEMPTY_TARGET", "renamed", "LCSAS_FORCE_NONEMPTY_RESTORE_FOLDER"),
    ("LCSAS_PW", "removed", "LCSAS_PASSWORD"),
])
def test_an_old_variable_exits_2_with_the_new_name(tmp_path, var, verb, new) -> None:
    env, _ = _bare_meta(tmp_path)
    # _run sets LCSAS_PLATFORM itself; the old name is set on top of it.
    res = _run(env, [], "\n\n\n", **{var: "x"})
    out = res.stdout + res.stderr
    assert res.returncode == 2, f"rc={res.returncode}\n{out}"
    assert f"restore.bat: {var} was {verb}: use {new}" in out, out
    assert "Restore to which folder" not in out, out


def test_restore_folder_answers_the_folder_question(tmp_path) -> None:
    env, meta, _snap = _built_meta(tmp_path)
    res = _run(env, ["--restore-folder", "E:\\chosen"], "\n", LCSAS_PASSWORD="test")
    out = res.stdout + res.stderr
    assert res.returncode == 0, f"rc={res.returncode}\n{out}"
    assert "Restore to which folder" not in out, out
    assert "proof.txt" in {p.name for p in non_marker_files(meta / "chosen")}, out


def test_restore_folder_needs_a_folder(tmp_path) -> None:
    env, _ = _bare_meta(tmp_path)
    res = _run(env, ["--restore-folder"], "\n\n\n")
    out = res.stdout + res.stderr
    assert res.returncode == 2, f"rc={res.returncode}\n{out}"
    assert "--restore-folder needs a folder" in out, out
