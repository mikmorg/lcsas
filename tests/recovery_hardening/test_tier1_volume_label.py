"""Hardening: tier-1 names each disc it finds by its label (#456).

Every data disc carries a ``volume_info.json`` written at staging time
(``staging/metadata.py::HolographicInjector.write_volume_info``), but
before #456 tier-1 never read it.  An operator who swapped in the wrong
disc saw only "still not found", with nothing to say which disc had
gone in.  Each rescan of the mount parents now prints::

    [lcsas-restore] mounted volume <label> at <path>

once per insertion.  A missing file is silent (older discs, plain
directories), and a bad one costs only the label: a one-line note, and
the disc is searched for packs as before.

The labels here are written by the real Python producer, so a change to
its output format that tier-1 can't read fails this test.

  1. Polling: the wrong disc is named and flagged "still not found"
     with no ENTER; swapped for the right one, that is named and the
     restore completes.
  2. ``--auto-mount off``: the disc rescanned after ENTER is named.
  3. Absent, malformed or unreadable ``volume_info.json`` never blocks
     the restore.

The C side (recovery/tests/test_disc_locator.c) pins the rest: once per
insertion, re-named after eject and reinsert, the meta disc, the size
cap, and control bytes in a label.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lcsas.db.models import Pack, Volume
from lcsas.staging.metadata import HolographicInjector

from .test_tier1_auto_mount import _DETECTED, _Run, _setup
from .test_tier1_rescan import _require_binary, _verify_restore

_RIGHT = "LCSAS_BD25_2026_0002"
_WRONG = "LCSAS_BD25_2026_0009"
_STILL = "(still not found -- check the disc label and try again)"


def _write_label(disc: Path, label: str) -> None:
    """Write ``disc/volume_info.json`` exactly as a burn would."""
    data = disc / "data"
    packs = [
        Pack(pack_id=i, sha256=p.name, size_bytes=p.stat().st_size,
             repo_id="family", is_pruned=False, created_at="2026-10-19")
        for i, p in enumerate(sorted(data.rglob("*")))
        if p.is_file()
    ]
    vol = Volume(
        volume_id=2, label=label, uuid=f"uuid-{label}",
        media_type="BD25", capacity_bytes=25_025_314_816,
        used_bytes=0, location="Home", status="STAGING",
        created_at="2026-10-19", closed_at=None, verified_at=None,
    )
    HolographicInjector(disc).write_volume_info(vol, packs)


def _wait_for(run: _Run, text: str) -> None:
    deadline = time.monotonic() + 60
    while text not in run.output():
        assert run.proc.poll() is None, (
            f"binary exited {run.proc.returncode} before printing "
            f"{text!r}:\n{run.output()}"
        )
        assert time.monotonic() < deadline, (
            f"binary never printed {text!r}:\n{run.output()}"
        )
        time.sleep(0.1)


def test_poll_names_the_wrong_disc_then_the_right_one(tmp_path: Path) -> None:
    _require_binary()
    cmd, staged, mount_parent, target, files = _setup(tmp_path)
    _write_label(staged, _RIGHT)
    wrong = staged.parent / "LCSAS_DISC_C"
    (wrong / "data").mkdir(parents=True)
    _write_label(wrong, _WRONG)

    run = _Run(cmd)
    try:
        run.wait_for_prompt()
        os.rename(wrong, mount_parent / wrong.name)
        _wait_for(run, _STILL)
        os.rename(mount_parent / wrong.name, wrong)
        os.rename(staged, mount_parent / staged.name)
        rc = run.finish()
    finally:
        run.reap()
    out = run.output()
    assert rc == 0, f"binary exited {rc}; expected 0 once the right disc mounted"
    _verify_restore(target, files)

    wrong_line = f"[lcsas-restore] mounted volume {_WRONG} at "
    right_line = f"[lcsas-restore] mounted volume {_RIGHT} at "
    lines = out.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(wrong_line)]
    assert len(starts) == 1, (
        f"wrong disc named {len(starts)} times, on its own line; want once"
    )
    assert lines[starts[0] + 1] == _STILL, (
        "naming the wrong disc must be followed by 'still not found'"
    )
    rights = [i for i, ln in enumerate(lines) if ln.startswith(right_line)]
    assert len(rights) == 1 and rights[0] > starts[0], (
        "the right disc was not named once, after the wrong one"
    )
    assert _DETECTED in lines[rights[0] + 1], (
        "the right disc's name must come just before 'new disc detected'"
    )


def test_enter_names_the_disc_it_rescanned(tmp_path: Path) -> None:
    _require_binary()
    cmd, staged, mount_parent, target, files = _setup(tmp_path)
    _write_label(staged, _RIGHT)
    run = _Run(cmd + ["--auto-mount", "off"])
    try:
        run.wait_for_prompt()
        os.rename(staged, mount_parent / staged.name)
        run.type("\n")
        rc = run.finish()
    finally:
        run.reap()
    out = run.output()
    assert rc == 0, f"binary exited {rc}; expected 0 after ENTER"
    _verify_restore(target, files)
    named = out.find(f"[lcsas-restore] mounted volume {_RIGHT} at ")
    found = out.find("; continuing.")
    assert named >= 0, "the disc rescanned after ENTER was not named"
    assert named < found, "the disc must be named before the restore continues"


@pytest.mark.parametrize(
    ("content", "note"),
    [
        (None, None),
        ('{"label": "LCSAS_BD25', "is not valid JSON"),
        ('["LCSAS_BD25_2026_0002"]', "is not valid JSON"),
        ('{"label": 2}', "has no usable label"),
        ("<directory>", "could not be read"),
    ],
    ids=["absent", "truncated", "not-an-object", "label-not-a-string",
         "unreadable"],
)
def test_bad_or_missing_volume_info_never_blocks_restore(
    tmp_path: Path, content: str | None, note: str | None
) -> None:
    _require_binary()
    cmd, staged, mount_parent, target, files = _setup(tmp_path)
    info = staged / "volume_info.json"
    if content == "<directory>":
        info.mkdir()
    elif content is not None:
        info.write_text(content)

    run = _Run(cmd)
    try:
        run.wait_for_prompt()
        os.rename(staged, mount_parent / staged.name)
        rc = run.finish()
    finally:
        run.reap()
    out = run.output()
    assert rc == 0, f"binary exited {rc}; a bad volume_info.json blocked the restore"
    assert _DETECTED in out
    _verify_restore(target, files)
    assert "mounted volume" not in out
    if note is None:
        assert "volume_info.json" not in out, (
            "a disc without volume_info.json must stay silent"
        )
    else:
        assert (
            f"/{staged.name}: its volume_info.json {note}; searching it anyway."
            in out
        ), f"no note saying the file {note}"
