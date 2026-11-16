"""#813: a stage that stops part-way re-finalises the volumes it keeps.

#657 puts on every disc of a stage the snapshots whose packs are on
durable volumes or anywhere in the session, so any disc of the session
is a starting point.  When the stage stopped after volume k of n,
volumes 1..k were kept with snapshot files that need the packs of
volumes that were never staged; "latest" on such a disc named a snapshot
that cannot be restored.  The kept volumes now lose those snapshot files
and are mastered again; one that cannot be mastered again is removed.
"""

from __future__ import annotations

import hashlib
from collections import namedtuple
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.config.media import MediaType
from lcsas.db.connection import get_memory_connection
from lcsas.db.queries import get_unarchived_packs
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volumes import list_volumes
from lcsas.packs.snapshot_coverage import SnapshotCoverage
from tests.unit.test_session_pipeline import _make_config, _seed_packs

_Usage = namedtuple("_Usage", "total used free")

OLD, FIRST, ALL = "a" * 64, "b" * 64, "c" * 64


def _env(tmp_path, monkeypatch, *, fail_on: int | None = None,
         fail_remaster: bool = False):
    """Three packs, one per TEST_TINY volume, and three snapshots: OLD
    needs no new pack, FIRST needs the pack of volume 1, ALL needs all
    three.  ``fail_on`` makes the Nth mastering fail; a later one (the
    re-master) writes ``remastered``, or fails with *fail_remaster*."""
    monkeypatch.setattr(
        "lcsas.burn.orchestrator.shutil.disk_usage",
        lambda _p: _Usage(10**13, 0, 10**13),
    )
    config = _make_config(tmp_path, num_repos=1)
    pw = tmp_path / "family.pw"
    pw.write_text("synthetic\n")
    repo = config.repositories["family"]
    config = replace(config, repositories={
        "family": replace(repo, password_file=pw)})
    snaps = repo.mirror_path / "snapshots"
    for sid in (OLD, FIRST, ALL):
        (snaps / sid).write_text("{}")

    conn = get_memory_connection()
    create_all(conn)
    register_repo(conn, "family", "Family", str(repo.mirror_path))
    _seed_packs(conn, config, num_packs=3, pack_size=900_000)

    xorriso = MagicMock()
    calls = {"n": 0}

    def _create_iso(source_dir, output_iso, volume_label, **kwargs):
        calls["n"] += 1
        if calls["n"] == fail_on:
            raise RuntimeError("xorriso failed")
        if fail_on is not None and calls["n"] > fail_on:
            if fail_remaster:
                raise OSError("disc full")
            Path(output_iso).write_bytes(b"remastered")
            return Path(output_iso)
        Path(output_iso).write_bytes(b"\x00" * 2048)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _create_iso
    orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                            prompt=NullOperatorPrompt())

    plans = orch._multi_bin_pack(get_unarchived_packs(conn),
                                 MediaType.TEST_TINY)
    assert [len(p) for p, _b in plans] == [1, 1, 1]
    order = [p[0].sha256 for p, _b in plans]

    def _coverage(mirror, password_file, held):
        need = {OLD: frozenset(), FIRST: frozenset(order[:1]),
                ALL: frozenset(order)}
        cov = SnapshotCoverage()
        for sid, packs in need.items():
            cov.missing[sid] = frozenset(packs - held)
            cov.times[sid] = "2026-11-16T00:00:00Z"
        return cov

    monkeypatch.setattr("lcsas.burn.orchestrator.snapshot_coverage",
                        _coverage)
    monkeypatch.setattr(
        "lcsas.burn.format_preflight.check_repo_recoverable",
        lambda *a, **k: None,
    )
    return orch, conn, xorriso


def _disc_snapshots(staging_root: Path) -> set[str]:
    [repo_dir] = (staging_root / "metadata").iterdir()
    return {p.name for p in (repo_dir / "snapshots").iterdir()}


def _hash_of(conn, volume_id: int) -> str:
    return conn.execute(
        "SELECT iso_sha256 FROM session_volumes WHERE volume_id = ?",
        (volume_id,),
    ).fetchone()[0]


def test_every_volume_of_a_full_stage_carries_the_session_snapshots(
    tmp_path, monkeypatch,
):
    orch, _conn, _x = _env(tmp_path, monkeypatch)
    result = orch.stage()
    trees = [m.staging_path for m in result.manifests]
    assert [_disc_snapshots(t) for t in trees] == [{OLD, FIRST, ALL}] * 3
    assert result.snapshots_left_out == {}


def test_stage_stopped_after_the_first_volume(tmp_path, monkeypatch):
    orch, conn, xorriso = _env(tmp_path, monkeypatch, fail_on=2)
    with pytest.raises(RuntimeError, match="xorriso failed"):
        orch.stage()
    [kept] = list_volumes(conn)
    session_dir = next(p for p in orch._config.staging_path.iterdir()
                       if p.is_dir())
    tree = session_dir / kept.label
    # FIRST's pack is on this volume; ALL needs volumes never staged.
    assert _disc_snapshots(tree) == {OLD, FIRST}
    # Mastered again (call 3), and the recorded hash is the new ISO's.
    assert xorriso.create_iso.call_count == 3
    iso = session_dir / f"{kept.label}.iso"
    assert iso.read_bytes() == b"remastered"
    assert _hash_of(conn, kept.volume_id) == hashlib.sha256(
        b"remastered").hexdigest()
    assert not iso.with_suffix(".unfinished").exists()


def test_kept_volume_that_cannot_be_mastered_again_is_removed(
    tmp_path, monkeypatch,
):
    orch, conn, _x = _env(tmp_path, monkeypatch, fail_on=2,
                          fail_remaster=True)
    with pytest.raises(RuntimeError, match="xorriso failed"):
        orch.stage()
    assert list_volumes(conn) == []
    assert len(get_unarchived_packs(conn)) == 3
    # No volume kept: the session and its folder are gone too.
    assert conn.execute("SELECT COUNT(*) FROM burn_sessions").fetchone()[0] \
        == 0
