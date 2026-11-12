"""#751: status does not call a burned volume with no disc record a ghost.

``lcsas status`` listed every never-recorded STAGING/BURNING volume with
its ISO gone as a ghost and said ``catalog reconcile --fix`` deletes it,
which reconcile no longer does for a volume that was burned (#746) or
that a catalog rebuild knows nothing about (#758).  Status now lists
those separately, with how to record the disc, and ``status --json``
gains ``burned_unrecorded`` and ``burn_unknown``; ``ghosts`` keeps only
volumes reconcile --fix deletes.

Every catalog here comes from a real ``lcsas catalog rebuild`` of the
staging trees the real orchestrator builds, with the staging area
emptied (a new machine).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from tests.unit.test_rebuild_burn_unknown import (
    _move_discs,
    _rebuild,
    _verify_disc,
    two_sessions,  # noqa: F401  (fixture)
)
from tests.unit.test_rebuild_newest_session import (
    discs,  # noqa: F401  (fixture)
)


def _status(db: Path, caplog, capsys) -> tuple[str, dict]:
    caplog.clear()
    assert main(["--db", str(db), "status"]) == 0
    text = caplog.text
    capsys.readouterr()
    assert main(["--db", str(db), "status", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    return text, doc


def _warnings(text: str, word: str) -> list[str]:
    return [line for line in text.splitlines() if word in line]


def test_newest_session_rebuilt_from_its_last_disc(
    discs, tmp_path, caplog, capsys,  # noqa: F811
):
    """Rebuilt from session A's disc and session B's last disc: B's last
    volume is BURNED with no disc; B's first was read by nothing.  Then
    'verify <first> --disc' without --location: it passes, records no
    disc, and the volume is STAGING with burn evidence."""
    trees, labels_b, config = discs
    first_b, last_b = labels_b
    moved = _move_discs([trees[0], trees[-1]], tmp_path)
    shutil.rmtree(config.staging_path)
    config.staging_path.mkdir()
    db = tmp_path / "rebuilt.db"
    _rebuild(moved, db)

    text, doc = _status(db, caplog, capsys)
    assert doc["ghosts"] == []
    assert doc["burned_unrecorded"] == [last_b]
    assert doc["burn_unknown"] == [first_b]
    assert "reconcile --fix' deletes" not in text
    (burned,) = _warnings(text, "were burned, but no disc of them")
    assert f"{last_b} (BURNED, but no disc of it is recorded)" in burned
    assert "lcsas verify <label> --disc --location" in burned
    (unknown,) = _warnings(text, "no disc it read shows")
    assert first_b in unknown and "--never-burned" in unknown

    assert _verify_disc(db, first_b) == 0, caplog.text
    text, doc = _status(db, caplog, capsys)
    assert doc["ghosts"] == []
    assert doc["burn_unknown"] == []
    assert doc["burned_unrecorded"] == sorted([first_b, last_b])
    (burned,) = _warnings(text, "were burned, but no disc of them")
    assert (f"{first_b} (STAGING, but a disc of it passed verification)"
            in burned)
    assert "reconcile --fix' deletes" not in text
    # The staged packs are explained; no "see reconcile" fallback.
    assert "were never burned. See" not in text


def test_true_ghost_is_still_a_ghost(two_sessions, caplog, capsys):  # noqa: F811
    """With the rebuild's note gone (a catalog that knows nothing more of
    it than a never-burned volume), the volume is a ghost again and the
    reconcile hint stays."""
    env = two_sessions
    _rebuild([env["x_tree"]], env["db"])
    conn = get_connection(env["db"])
    conn.execute("DELETE FROM volume_events WHERE event_type = 'NOTE'")
    conn.commit()
    conn.close()
    text, doc = _status(env["db"], caplog, capsys)
    assert doc["ghosts"] == [env["y"]]
    assert doc["burn_unknown"] == []
    assert doc["burned_unrecorded"] == [env["x"]]
    assert "'lcsas catalog reconcile --fix' deletes them" in text
