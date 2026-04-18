"""Tests for db/sessions.py — burn session management."""

from __future__ import annotations

import pytest

from lcsas.db.sessions import (
    add_session_volume,
    create_session,
    delete_session,
    get_latest_session,
    get_session,
    get_session_volumes,
    list_sessions,
    resolve_session_id,
    update_iso_sha256,
    update_session_status,
)
from lcsas.db.volumes import create_volume
from lcsas.utils.labels import generate_uuid


class TestSessionCRUD:
    def test_create_and_get(self, conn):
        s = create_session(conn, "MDISC100", "/mnt/staging/s1",
                           session_id="2026-02-14T19:30:00")
        assert s.session_id == "2026-02-14T19:30:00"
        assert s.media_type == "MDISC100"
        assert s.status == "STAGED"
        assert s.staging_dir == "/mnt/staging/s1"

        fetched = get_session(conn, "2026-02-14T19:30:00")
        assert fetched.session_id == s.session_id

    def test_auto_session_id(self, conn):
        s = create_session(conn, "TEST_TINY", "/tmp/staging")
        assert len(s.session_id) > 0

    def test_get_latest(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="2026-01-01T00:00:00")
        create_session(conn, "TEST_TINY", "/tmp/s2", session_id="2026-02-01T00:00:00")
        # Manually set different created_at timestamps to ensure deterministic order
        conn.execute(
            "UPDATE burn_sessions SET created_at = ? WHERE session_id = ?",
            ("2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.execute(
            "UPDATE burn_sessions SET created_at = ? WHERE session_id = ?",
            ("2026-02-01T00:00:00", "2026-02-01T00:00:00"),
        )
        conn.commit()

        latest = get_latest_session(conn)
        assert latest.session_id == "2026-02-01T00:00:00"

    def test_get_latest_no_sessions_raises(self, conn):
        with pytest.raises(ValueError, match="No sessions"):
            get_latest_session(conn)

    def test_resolve_latest(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="s1")
        resolved = resolve_session_id(conn, "latest")
        assert resolved == "s1"

    def test_resolve_explicit(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="explicit_id")
        resolved = resolve_session_id(conn, "explicit_id")
        assert resolved == "explicit_id"

    def test_resolve_nonexistent_raises(self, conn):
        with pytest.raises(ValueError):
            resolve_session_id(conn, "nonexistent")

    def test_update_status(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="s1")
        update_session_status(conn, "s1", "COMPLETE")
        s = get_session(conn, "s1")
        assert s.status == "COMPLETE"

    def test_list_sessions(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="s1")
        create_session(conn, "TEST_TINY", "/tmp/s2", session_id="s2")

        all_sessions = list_sessions(conn)
        assert len(all_sessions) == 2

    def test_list_sessions_filtered(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="s1")
        create_session(conn, "TEST_TINY", "/tmp/s2", session_id="s2")
        update_session_status(conn, "s2", "COMPLETE")

        staged = list_sessions(conn, status_filter="STAGED")
        assert len(staged) == 1
        assert staged[0].session_id == "s1"

    def test_delete_session(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="s1")
        delete_session(conn, "s1")
        with pytest.raises(ValueError):
            get_session(conn, "s1")


class TestSessionVolumes:
    def test_add_and_get(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="s1")
        vol = create_volume(conn, "VOL_001", generate_uuid(),
                            "TEST_TINY", 1_000_000)
        sv = add_session_volume(conn, "s1", vol.volume_id,
                                "/tmp/s1/VOL_001.iso", "abc123")
        assert sv.session_id == "s1"
        assert sv.volume_id == vol.volume_id
        assert sv.iso_sha256 == "abc123"

    def test_get_session_volumes(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="s1")
        v1 = create_volume(conn, "V1", generate_uuid(), "TEST_TINY", 1_000_000)
        v2 = create_volume(conn, "V2", generate_uuid(), "TEST_TINY", 1_000_000)
        add_session_volume(conn, "s1", v1.volume_id, "/tmp/V1.iso")
        add_session_volume(conn, "s1", v2.volume_id, "/tmp/V2.iso")

        vols = get_session_volumes(conn, "s1")
        assert len(vols) == 2

    def test_update_iso_sha256(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="s1")
        vol = create_volume(conn, "V1", generate_uuid(), "TEST_TINY", 1_000_000)
        add_session_volume(conn, "s1", vol.volume_id, "/tmp/V1.iso")

        update_iso_sha256(conn, "s1", vol.volume_id, "deadbeef")
        vols = get_session_volumes(conn, "s1")
        assert vols[0].iso_sha256 == "deadbeef"

    def test_delete_session_removes_volumes(self, conn):
        create_session(conn, "TEST_TINY", "/tmp/s1", session_id="s1")
        vol = create_volume(conn, "V1", generate_uuid(), "TEST_TINY", 1_000_000)
        add_session_volume(conn, "s1", vol.volume_id, "/tmp/V1.iso")

        delete_session(conn, "s1")
        vols = get_session_volumes(conn, "s1")
        assert len(vols) == 0
