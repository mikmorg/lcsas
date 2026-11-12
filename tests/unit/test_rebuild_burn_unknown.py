"""#758: after a catalog rebuild, a volume whose disc was not read is kept.

Each disc carries the catalog as it was when the disc was staged, so it
lists the volumes staged before it as STAGING with no events.  A volume
whose own disc the rebuild did not read comes back looking never burned.
The rebuild now notes it; ``session abort`` (session and ``--volume``),
``stage --clean --force`` and ``catalog reconcile --fix`` refuse to
delete it unless the owner says ``--never-burned``.  That flag never
overrides the #737 refusal for a BURNED volume with no disc record.

The discs are the staging trees the real orchestrator builds (they hold
the disc catalog and ``volume_info.json``); xorriso and the drive are
faked.  Every rebuild is a real ``lcsas catalog rebuild``.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.rebuild import rebuild_catalog
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volumes import get_volume_by_label
from tests.unit.test_rebuild_newest_session import (
    _add_packs,
    _config,
    _orchestrator,
    discs,  # noqa: F401  (fixture)
)


def _toml(tmp_path: Path, config, db: Path) -> Path:
    cfg = tmp_path / "rebuilt.toml"
    cfg.write_text(
        f'[paths]\nmirror_base = "{config.mirror_base_path}"\n'
        f'staging = "{config.staging_path}"\n'
        f'database = "{db}"\n'
        '[defaults]\nmedia_type = "TEST_TINY"\nmetadata_reserve_mb = 0\n'
    )
    return cfg


def _labels(db: Path) -> dict[str, str]:
    conn = get_connection(db)
    try:
        return {r["label"]: r["status"]
                for r in conn.execute("SELECT label, status FROM volumes")}
    finally:
        conn.close()


def _notes(db: Path, label: str) -> list[str]:
    conn = get_connection(db)
    try:
        return [r[0] for r in conn.execute(
            "SELECT e.detail FROM volume_events e JOIN volumes v "
            "USING (volume_id) WHERE v.label = ? AND e.event_type = 'NOTE'",
            (label,),
        )]
    finally:
        conn.close()


def _move_discs(trees: list[Path], tmp_path: Path) -> list[Path]:
    """Copy the disc trees out of the staging area, then empty it: the
    rebuild runs where no staged ISO is left (a new machine)."""
    out = []
    for tree in trees:
        dest = tmp_path / "discs" / tree.name
        shutil.copytree(tree, dest)
        out.append(dest)
    return out


@pytest.fixture
def two_sessions(tmp_path):
    """Session S1 (volume Y) is staged; while Y waits, session S2 (volume
    X) is staged.  Both are burned, then the catalog is lost.  X's disc
    lists Y as STAGING; Y's own disc is not at hand."""
    config = _config(tmp_path)
    conn = get_connection(config.db_path)
    create_all(conn)
    register_repo(conn, "family", "Family",
                  str(config.repositories["family"].mirror_path))
    conn.commit()
    orch = _orchestrator(config, conn)
    _add_packs(conn, config, 1, 1, 50)
    s1 = orch.stage()
    _add_packs(conn, config, 2, 1, 50)
    s2 = orch.stage()
    conn.close()
    (y,) = [m.volume_label for m in s1.manifests]
    (x,) = [m.volume_label for m in s2.manifests]
    (x_tree,) = _move_discs([s2.staging_dir / x], tmp_path)
    shutil.rmtree(config.staging_path)
    config.staging_path.mkdir()
    db = tmp_path / "rebuilt" / "archive.db"
    db.parent.mkdir()
    return {"config": config, "x": x, "y": y, "s1": s1.session_id,
            "s2": s2.session_id, "x_tree": x_tree, "db": db,
            "cfg": _toml(tmp_path, config, db)}


def _rebuild(trees: list[Path], db: Path) -> None:
    assert main(["catalog", "rebuild", *[str(t) for t in trees],
                 "--output", str(db)]) == 0


def test_rebuild_marks_and_lists_the_unread_volume(two_sessions, caplog):
    env = two_sessions
    caplog.clear()
    _rebuild([env["x_tree"]], env["db"])
    assert _labels(env["db"]) == {env["x"]: "BURNED", env["y"]: "STAGING"}
    assert "no disc that was read shows whether they were burned: " \
        f"{env['y']}" in caplog.text
    assert "--never-burned" in caplog.text
    (note,) = _notes(env["db"], env["y"])
    assert note.startswith("Catalog rebuild: burn state unknown")
    assert _notes(env["db"], env["x"]) == []

    # Rebuilt again into the same output: listed once, noted once.
    result = rebuild_catalog([env["x_tree"]], env["db"])
    assert result.burn_unknown == []
    assert len(_notes(env["db"], env["y"])) == 1


def _abort(env, *extra: str) -> int:
    return main(["--config", str(env["cfg"]), "session", "abort", *extra])


@pytest.mark.parametrize("how", ["session", "volume", "clean", "reconcile",
                                 "reconcile-waiting"])
def test_every_delete_path_refuses_the_unread_volume(two_sessions, caplog,
                                                     how):
    env = two_sessions
    _rebuild([env["x_tree"]], env["db"])
    caplog.clear()
    if how == "session":
        assert _abort(env, env["s1"]) == 1
    elif how == "volume":
        assert _abort(env, "--volume", env["y"]) == 1
    elif how == "clean":
        assert main(["--config", str(env["cfg"]), "stage", "--clean",
                     "--force", "--session", env["s1"]]) == 1
    else:
        extra = ["--include-waiting"] if how == "reconcile-waiting" else []
        main(["--config", str(env["cfg"]), "catalog", "reconcile", "--fix",
              "--yes", "--older-than-hours", "0", *extra])
        assert "--fix never deletes them" in caplog.text
    if how in ("session", "volume", "clean"):
        assert "no disc it read shows whether it was burned" in caplog.text
        assert "--never-burned" in caplog.text
        assert "Nothing was deleted" in caplog.text
    assert env["y"] in _labels(env["db"])


@pytest.mark.parametrize("how", ["session", "volume"])
def test_never_burned_deletes_it(two_sessions, caplog, how):
    env = two_sessions
    _rebuild([env["x_tree"]], env["db"])
    if how == "session":
        assert _abort(env, env["s1"], "--never-burned") == 0, caplog.text
    else:
        assert _abort(env, "--volume", env["y"], "--never-burned") == 0, \
            caplog.text
    assert _labels(env["db"]) == {env["x"]: "BURNED"}
    # Its pack is back in the stage pool.
    conn = get_connection(env["db"])
    try:
        from lcsas.db.queries import get_unarchived_packs
        assert len(get_unarchived_packs(conn)) == 1
    finally:
        conn.close()


def _verify_disc(db: Path, label: str, *extra: str) -> int:
    with (
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
              "read_disc_volume_id", return_value=label),
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
              return_value=True),
        patch("lcsas.burn.device_verify.read_device_sha256",
              side_effect=lambda device, n:
              hashlib.sha256(b"\x00" * n).hexdigest()),
    ):
        return main(["--db", str(db), "verify", label, "--disc",
                     "--device", "/dev/null", *extra])


def test_recording_the_disc_ends_the_refusal(two_sessions, caplog):
    env = two_sessions
    _rebuild([env["x_tree"]], env["db"])
    conn = get_connection(env["db"])
    try:
        from lcsas.db.locations import create_location
        create_location(conn, "Home")
    finally:
        conn.close()
    assert _verify_disc(env["db"], env["y"], "--location", "Home") == 0, \
        caplog.text
    from lcsas.db.volume_events import rebuild_burn_unknown

    conn = get_connection(env["db"])
    try:
        vol = get_volume_by_label(conn, env["y"])
        assert vol.status == "VERIFIED"
        assert not rebuild_burn_unknown(conn, vol.volume_id)
    finally:
        conn.close()
    # Abort now finds nothing to delete: Y has its disc.
    assert _abort(env, env["s1"]) == 0, caplog.text
    assert env["y"] in _labels(env["db"])


def test_multi_volume_session_with_its_later_disc_recorded(
    discs, tmp_path, caplog,  # noqa: F811
):
    """Session B's first disc catalog was copied before its second volume
    existed.  Rebuilt from A's disc and B's last disc, with that last
    disc then recorded, B's first volume used to be deleted by
    'session abort'."""
    trees, labels_b, config = discs
    first_b, last_b = labels_b
    moved = _move_discs([trees[0], trees[-1]], tmp_path)
    shutil.rmtree(config.staging_path)
    config.staging_path.mkdir()
    db = tmp_path / "rebuilt.db"
    _rebuild(moved, db)
    assert _labels(db)[first_b] == "STAGING"
    assert _labels(db)[last_b] == "BURNED"
    assert _verify_disc(db, last_b, "--location", "Home") == 0, caplog.text

    conn = get_connection(db)
    try:
        (session_b,) = [r[0] for r in conn.execute(
            "SELECT sv.session_id FROM session_volumes sv JOIN volumes v "
            "USING (volume_id) WHERE v.label = ?", (last_b,))]
    finally:
        conn.close()
    cfg = _toml(tmp_path, config, db)
    caplog.clear()
    assert main(["--config", str(cfg), "session", "abort", session_b]) == 1
    assert first_b in caplog.text and "--never-burned" in caplog.text
    assert set(_labels(db)) >= {first_b, last_b}


def test_never_burned_never_overrides_the_737_refusal(
    discs, tmp_path, caplog,  # noqa: F811
):
    """B's last volume is BURNED with no disc record: --never-burned
    still refuses the whole abort, and nothing is deleted."""
    trees, labels_b, config = discs
    first_b, last_b = labels_b
    moved = _move_discs([trees[0], trees[-1]], tmp_path)
    shutil.rmtree(config.staging_path)
    config.staging_path.mkdir()
    db = tmp_path / "rebuilt.db"
    _rebuild(moved, db)
    conn = get_connection(db)
    try:
        (session_b,) = [r[0] for r in conn.execute(
            "SELECT sv.session_id FROM session_volumes sv JOIN volumes v "
            "USING (volume_id) WHERE v.label = ?", (last_b,))]
    finally:
        conn.close()
    cfg = _toml(tmp_path, config, db)
    before = _labels(db)
    caplog.clear()
    assert main(["--config", str(cfg), "session", "abort", session_b,
                 "--never-burned"]) == 1
    assert "BURNED, but no disc of it is recorded" in caplog.text
    assert _labels(db) == before
    caplog.clear()
    assert main(["--config", str(cfg), "session", "abort", "--volume",
                 last_b, "--never-burned"]) == 1
    assert _labels(db) == before


def test_volume_the_output_catalog_had_is_not_marked(tmp_path):
    """Rebuilding into a catalog that already knows a waiting volume
    leaves that volume as it was: its own catalog knows it."""
    config = _config(tmp_path)
    conn = get_connection(config.db_path)
    create_all(conn)
    register_repo(conn, "family", "Family",
                  str(config.repositories["family"].mirror_path))
    conn.commit()
    orch = _orchestrator(config, conn)
    _add_packs(conn, config, 1, 1, 50)
    s1 = orch.stage()
    _add_packs(conn, config, 2, 1, 50)
    s2 = orch.stage()
    conn.close()
    (y,) = [m.volume_label for m in s1.manifests]
    (x,) = [m.volume_label for m in s2.manifests]
    result = rebuild_catalog([s2.staging_dir / x], config.db_path)
    assert result.ok
    assert result.burn_unknown == []
    assert _notes(config.db_path, y) == []


def test_a_later_rebuild_still_takes_a_fresher_disc(tmp_path):
    """The note must not make the rebuilt catalog look newer than its
    discs: rebuilt again with a fresher disc, the volume takes that
    disc's view (here: burned and verified at Home)."""
    config = _config(tmp_path)
    conn = get_connection(config.db_path)
    create_all(conn)
    register_repo(conn, "family", "Family",
                  str(config.repositories["family"].mirror_path))
    conn.commit()
    orch = _orchestrator(config, conn)
    _add_packs(conn, config, 1, 1, 50)
    s1 = orch.stage()
    _add_packs(conn, config, 2, 1, 50)
    s2 = orch.stage()
    orch.burn_session(s1.session_id, location="Home", skip_burn=True)
    _add_packs(conn, config, 3, 1, 50)
    s3 = orch.stage()
    conn.close()
    (y,) = [m.volume_label for m in s1.manifests]
    (x,) = [m.volume_label for m in s2.manifests]
    (z,) = [m.volume_label for m in s3.manifests]
    db = tmp_path / "rebuilt.db"

    _rebuild([s2.staging_dir / x], db)
    assert _labels(db)[y] == "STAGING"
    assert len(_notes(db, y)) == 1

    result = rebuild_catalog([s3.staging_dir / z], db)
    assert result.ok
    assert not any(y in w for w in result.warnings), result.warnings
    assert _labels(db)[y] == "VERIFIED"
    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, y)
        locs = [r[0] for r in conn.execute(
            "SELECT location FROM volume_copies WHERE volume_id = ? "
            "AND status = 'ACTIVE'", (vol.volume_id,))]
    finally:
        conn.close()
    assert locs == ["Home"]
