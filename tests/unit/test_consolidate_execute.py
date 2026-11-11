"""#677: ``lcsas consolidate --execute`` stages the sources' packs.

These tests drive the real ``BurnOrchestrator.stage`` through the CLI;
only xorriso's ISO writer is replaced (TEST_TINY media carries no ECC, so
dvdisaster is never called).  Pack files on the mirror hash to their
names, because staging checks every pack's content (BURN-02).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.locations import create_location
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import add_volume_copy
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume, get_volume_by_label


def _fake_create_iso(self, source_dir, output_iso, volume_label, **kwargs):
    Path(output_iso).write_bytes(b"\x00" * 1024)
    return Path(output_iso)


@pytest.fixture
def archive(tmp_path):
    """Two VERIFIED volumes (2 packs each, one disc each at Home) and a
    config whose mirror holds every pack.  Returns (config path, db)."""
    mirror = tmp_path / "mirror" / "test"
    (mirror / "data").mkdir(parents=True)
    for sub in ("index", "snapshots", "keys"):
        (mirror / sub).mkdir()
        (mirror / sub / "dummy.json").write_text('{"test": true}')
    (mirror / "config").write_text('{"version": 2}')
    staging = tmp_path / "staging"
    staging.mkdir()
    db = tmp_path / "archive.db"

    conn = get_connection(db)
    create_all(conn)
    register_repo(conn, "test", "Test", str(mirror))
    create_location(conn, "Home")
    create_location(conn, "Vault")
    for v in range(2):
        vol = create_volume(
            conn, label=f"OLD_{v}", uuid=f"uuid-old-{v}",
            media_type="TEST_TINY", capacity_bytes=2_097_152,
            status="VERIFIED",
        )
        pack_ids = []
        for p in range(2):
            content = f"volume {v} pack {p} ".encode() * 50
            sha = hashlib.sha256(content).hexdigest()
            (mirror / "data" / sha).write_bytes(content)
            pack_ids.append(register_pack(
                conn, sha256=sha, size_bytes=len(content), repo_id="test",
            ).pack_id)
        bulk_link_packs(conn, vol.volume_id, pack_ids)
        add_volume_copy(conn, vol.volume_id, "Home", commit=False)
    conn.commit()
    conn.close()

    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        "[paths]\n"
        f'mirror_base = "{mirror.parent}"\n'
        f'staging = "{staging}"\n'
        f'database = "{db}"\n'
        "\n"
        "[defaults]\n"
        'media_type = "TEST_TINY"\n'
        'label_prefix = "NEW"\n'
        'location = "Home"\n'
        "metadata_reserve_mb = 1\n"
        "allow_unverified_repo_format = true\n"
        "\n"
        "[repos.test]\n"
        f'mirror_path = "{mirror}"\n'
    )
    return str(cfg), db


def _consolidate(cfg: str, *extra: str, answer: str = "yes") -> tuple[int, MagicMock]:
    prompt = MagicMock(return_value=answer)
    with (
        patch("builtins.input", prompt),
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.create_iso",
              _fake_create_iso),
    ):
        rc = main(["--config", cfg, "consolidate", "OLD_0", "OLD_1",
                   "--target-media", "TEST_TINY", "--execute", *extra])
    return rc, prompt


def _source_packs(conn) -> set[str]:
    return {
        row[0] for row in conn.execute(
            "SELECT p.sha256 FROM packs p "
            "JOIN volume_packs vp ON vp.pack_id = p.pack_id "
            "JOIN volumes v ON v.volume_id = vp.volume_id "
            "WHERE v.label IN ('OLD_0', 'OLD_1')"
        )
    }


def test_execute_stages_one_volume_with_the_sources_packs(archive, caplog):
    cfg, db = archive
    rc, _prompt = _consolidate(cfg, "--location", "Vault")
    assert rc == 0, caplog.text

    conn = get_connection(db)
    try:
        new = conn.execute(
            "SELECT volume_id, label, status, location FROM volumes "
            "WHERE label NOT IN ('OLD_0', 'OLD_1')"
        ).fetchall()
        assert len(new) == 1
        assert new[0]["status"] == "STAGING"
        # The new burn session records where its discs are meant to go.
        assert new[0]["location"] == "Vault"
        staged = {
            row[0] for row in conn.execute(
                "SELECT p.sha256 FROM packs p "
                "JOIN volume_packs vp ON vp.pack_id = p.pack_id "
                "WHERE vp.volume_id = ?", (new[0]["volume_id"],),
            )
        }
        assert staged == _source_packs(conn)
        assert len(staged) == 4
        assert conn.execute(
            "SELECT COUNT(*) FROM session_volumes WHERE volume_id = ?",
            (new[0]["volume_id"],),
        ).fetchone()[0] == 1
        for label in ("OLD_0", "OLD_1"):
            assert get_volume_by_label(conn, label).status == "CONSOLIDATING"
    finally:
        conn.close()
    assert "No packs need staging" not in caplog.text


def test_default_location_is_the_configured_one(archive, caplog):
    cfg, db = archive
    rc, _prompt = _consolidate(cfg)
    assert rc == 0, caplog.text
    conn = get_connection(db)
    try:
        (location,) = conn.execute(
            "SELECT location FROM volumes WHERE label LIKE 'NEW%'"
        ).fetchone()
    finally:
        conn.close()
    assert location == "Home"


@pytest.mark.parametrize("status", ["BURNED", "CONSOLIDATING", "DEPRECATED"])
def test_a_source_that_is_not_verified_is_refused_before_the_prompt(
    archive, caplog, status,
):
    cfg, db = archive
    conn = get_connection(db)
    conn.execute("UPDATE volumes SET status = ? WHERE label = 'OLD_1'",
                 (status,))
    conn.commit()
    conn.close()

    rc, prompt = _consolidate(cfg)
    assert rc == 1
    prompt.assert_not_called()
    assert f"Volume OLD_1 is {status}" in caplog.text
    assert "Invalid status transition" not in caplog.text

    conn = get_connection(db)
    try:
        assert get_volume_by_label(conn, "OLD_0").status == "VERIFIED"
        assert conn.execute("SELECT COUNT(*) FROM volumes").fetchone()[0] == 2
    finally:
        conn.close()


def test_unknown_location_is_refused_before_the_prompt(archive, caplog):
    cfg, _db = archive
    rc, prompt = _consolidate(cfg, "--location", "Valut")
    assert rc == 1
    prompt.assert_not_called()
    assert "Unknown location 'Valut'" in caplog.text


def test_session_abort_returns_the_sources_to_verified(archive, caplog):
    cfg, db = archive
    rc, _prompt = _consolidate(cfg)
    assert rc == 0, caplog.text

    caplog.clear()
    assert main(["--config", cfg, "session", "abort", "latest"]) == 0, caplog.text
    assert "OLD_0, OLD_1" in caplog.text

    conn = get_connection(db)
    try:
        for label in ("OLD_0", "OLD_1"):
            assert get_volume_by_label(conn, label).status == "VERIFIED"
        assert conn.execute(
            "SELECT COUNT(*) FROM volumes WHERE label LIKE 'NEW%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()

    # A second consolidation can then run again.
    rc, _prompt = _consolidate(cfg)
    assert rc == 0, caplog.text


def test_aborting_another_session_leaves_the_sources_alone(archive, caplog):
    """Only the consolidation's own session releases its sources."""
    cfg, db = archive
    rc, _prompt = _consolidate(cfg)
    assert rc == 0, caplog.text
    conn = get_connection(db)
    (consolidation_session,) = conn.execute(
        "SELECT session_id FROM burn_sessions"
    ).fetchone()
    conn.close()

    from lcsas.consolidate.merger import release_consolidation_sources

    conn = get_connection(db)
    try:
        assert release_consolidation_sources(conn, "some-other-session") == []
        assert get_volume_by_label(conn, "OLD_0").status == "CONSOLIDATING"
        assert release_consolidation_sources(
            conn, consolidation_session,
        ) == ["OLD_0", "OLD_1"]
    finally:
        conn.close()
