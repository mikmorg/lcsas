"""#753: a stage that fails inside its burn session leaves no empty session.

``stage()`` creates the burn session row and its staging directory, then
stages each volume.  ``_stage_single_volume`` removes the volume that
failed; ``stage()`` now also removes the session when no volume of it was
finished, and names the kept volumes when some were.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.config.media import MediaType
from lcsas.db.connection import get_memory_connection
from lcsas.db.queries import get_unarchived_packs
from lcsas.db.schema import create_all
from lcsas.db.sessions import get_session_volumes, list_sessions
from lcsas.db.volumes import list_volumes
from lcsas.staging.builder import MirrorUnavailableError
from tests.unit.test_burn_orchestrator import (
    _make_config,
    _make_orch,
    _seed_db_and_mirror,
    multi_vol_env,  # noqa: F401  (fixture)
)


def _sessions(conn) -> list[str]:
    return [s.session_id for s in list_sessions(conn)]


def _single_env(tmp_path):
    from unittest.mock import MagicMock

    from lcsas.burn.operator import NullOperatorPrompt
    from tests.unit.test_burn_orchestrator import _fake_xorriso

    config = _make_config(tmp_path)
    conn = get_memory_connection()
    create_all(conn)
    packs = _seed_db_and_mirror(conn, config)
    xorriso = _fake_xorriso()
    dvdisaster = MagicMock()
    orch = BurnOrchestrator(config, conn, xorriso, dvdisaster,
                            prompt=NullOperatorPrompt())
    return orch, config, conn, packs, xorriso, dvdisaster


def _assert_nothing_left(config, conn, packs) -> None:
    assert _sessions(conn) == []
    assert list_volumes(conn) == []
    assert len(get_unarchived_packs(conn)) == len(packs)
    assert list(config.staging_path.iterdir()) == []


class TestFirstVolumeFails:
    def test_missing_mirror_leaves_no_session(self, tmp_path):
        """Refused by the up-front mirror check: no session is created."""
        orch, config, conn, packs, _x, _d = _single_env(tmp_path)
        shutil.rmtree(config.repositories["family"].mirror_path / "data")
        with pytest.raises(MirrorUnavailableError, match="family"):
            orch.stage()
        _assert_nothing_left(config, conn, packs)

    def test_missing_mirror_inside_the_session_is_cleaned_up(
        self, tmp_path, monkeypatch,
    ):
        """The mirror goes away after the up-front check (an automount
        dropping mid-stage): the per-volume check refuses, and the session
        and its directory are removed."""
        orch, config, conn, packs, _x, _d = _single_env(tmp_path)
        monkeypatch.setattr(orch, "_mirror_preflight", lambda plans: None)
        shutil.rmtree(config.repositories["family"].mirror_path / "data")
        with pytest.raises(MirrorUnavailableError, match="family"):
            orch.stage()
        _assert_nothing_left(config, conn, packs)

    def test_mastering_failure_leaves_no_session(self, tmp_path, caplog):
        orch, config, conn, packs, xorriso, _d = _single_env(tmp_path)
        xorriso.create_iso.side_effect = RuntimeError("xorriso exploded")
        with pytest.raises(RuntimeError, match="xorriso exploded"):
            orch.stage()
        _assert_nothing_left(config, conn, packs)
        assert "no volume was staged" in caplog.text

    def test_ecc_failure_leaves_no_session(self, tmp_path, monkeypatch):
        orch, config, conn, packs, _x, dvdisaster = _single_env(tmp_path)
        # TEST_TINY carries no ECC; give it some so augment_iso runs.
        monkeypatch.setattr(MediaType.TEST_TINY, "_ecc_overhead_pct", 1)
        monkeypatch.setattr(orch, "_check_redundancy",
                            lambda iso, label, vid: None)
        dvdisaster.augment_iso.side_effect = RuntimeError("dvdisaster failed")
        with pytest.raises(RuntimeError, match="dvdisaster failed"):
            orch.stage()
        dvdisaster.augment_iso.assert_called_once()
        _assert_nothing_left(config, conn, packs)

    def test_ctrl_c_leaves_no_session(self, tmp_path):
        orch, config, conn, packs, xorriso, _d = _single_env(tmp_path)
        xorriso.create_iso.side_effect = KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            orch.stage()
        _assert_nothing_left(config, conn, packs)


class TestLaterVolumeFails:
    def test_kept_volumes_stay_listed_and_are_named(
        self, multi_vol_env, caplog,  # noqa: F811
    ):
        """Volume 1 is finished, volume 2 fails: volume 1 and its session
        stay (as before), and the error names them."""
        from lcsas.burn.operator import NullOperatorPrompt

        env = multi_vol_env
        conn, config = env["conn"], env["config"]
        real = env["xorriso"].create_iso.side_effect
        calls = []

        def master(source_dir, output_iso, volume_label, **kwargs):
            calls.append(volume_label)
            if len(calls) == 2:
                raise RuntimeError("xorriso exploded on volume 2")
            return real(source_dir, output_iso, volume_label, **kwargs)

        env["xorriso"].create_iso.side_effect = master
        orch = _make_orch(env, NullOperatorPrompt())
        with pytest.raises(RuntimeError, match="volume 2"):
            orch.stage()

        (session_id,) = _sessions(conn)
        kept = [v.label for v in list_volumes(conn)]
        assert kept == [calls[0]]
        assert [sv.volume_id for sv in get_session_volumes(conn, session_id)
                ] == [v.volume_id for v in list_volumes(conn)]
        session_dir = config.staging_path / session_id.replace(":", "-")
        assert (session_dir / f"{calls[0]}.iso").is_file()
        assert (session_dir / "session.json").is_file()
        assert not (session_dir / f"{calls[1]}.iso").exists()
        assert "Staging stopped after 1 of 3 volume(s)" in caplog.text
        assert f"keeps the volume(s) staged before the failure: {calls[0]}" \
            in caplog.text
        assert f"lcsas session abort {session_id}" in caplog.text

    def test_ctrl_c_on_a_later_volume_keeps_the_finished_ones(
        self, multi_vol_env,  # noqa: F811
    ):
        from lcsas.burn.operator import NullOperatorPrompt

        env = multi_vol_env
        conn = env["conn"]
        real = env["xorriso"].create_iso.side_effect
        calls = []

        def master(source_dir, output_iso, volume_label, **kwargs):
            calls.append(volume_label)
            if len(calls) == 3:
                raise KeyboardInterrupt
            return real(source_dir, output_iso, volume_label, **kwargs)

        env["xorriso"].create_iso.side_effect = master
        with pytest.raises(KeyboardInterrupt):
            _make_orch(env, NullOperatorPrompt()).stage()
        (session_id,) = _sessions(conn)
        assert sorted(v.label for v in list_volumes(conn)) == calls[:2]
        assert len(get_session_volumes(conn, session_id)) == 2


def test_refused_stage_keeps_existing_sessions(tmp_path):
    """Cleaning up a failed stage touches only its own session."""
    orch, config, conn, packs, xorriso, _d = _single_env(tmp_path)
    first = orch.stage()
    # New packs for a second stage, which fails.
    from lcsas.db.packs import register_pack
    from tests.unit.test_burn_orchestrator import _pack_bytes, _sha

    data_dir = config.repositories["family"].mirror_path / "data"
    content = _pack_bytes(99, 100)
    register_pack(conn, sha256=_sha(content), size_bytes=100,
                  repo_id="family")
    (data_dir / _sha(content)).write_bytes(content)
    xorriso.create_iso.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        orch.stage(for_location=None, pack_sha256s=[_sha(content)])
    assert _sessions(conn) == [first.session_id]
    assert Path(first.staging_dir).is_dir()
    assert sorted(p.name for p in config.staging_path.iterdir()) == [
        Path(first.staging_dir).name
    ]
