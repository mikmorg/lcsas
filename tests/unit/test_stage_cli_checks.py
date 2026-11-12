"""``lcsas stage`` checks that must hold for a dry run as for a real run.

- ``--for-location`` must name a registered location (#738), as
  ``--collection``'s location must (#701).
- A dry run refuses on password split drift like the real run, unless
  ``--allow-escrow-drift`` (#739).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.locations import create_location
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all


def _setup(tmp_path: Path, *, key_split: bool = False) -> tuple[Path, Path]:
    """Config + catalog with two scanned packs and the location Home."""
    staging = tmp_path / "staging"
    staging.mkdir()
    mirror = tmp_path / "mirror" / "fam"
    data = mirror / "data"
    for sub in ("index", "snapshots", "keys"):
        (mirror / sub).mkdir(parents=True)
        (mirror / sub / "dummy").write_text("{}")
    (mirror / "config").write_text("{}")
    data.mkdir()
    db = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
    split = ("key_split = true\nkey_threshold = 2\nkey_shares = 3\n"
             if key_split else "")
    cfg.write_text(f"""
[paths]
mirror_base = "{tmp_path / 'mirror'}"
staging = "{staging}"
database = "{db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
label_prefix = "T"
allow_unverified_repo_format = true
{split}
[repos.fam]
mirror_path = "{mirror}"
""")
    conn = get_connection(db)
    create_all(conn)
    register_repo(conn, "fam", "fam", str(mirror))
    create_location(conn, "Home")
    create_location(conn, "Bank_Box")
    for i in range(2):
        content = (f"{i:08x}".encode() * 8)[:64]
        sha = hashlib.sha256(content).hexdigest()
        (data / sha[:2]).mkdir(exist_ok=True)
        (data / sha[:2] / sha).write_bytes(content)
        register_pack(conn, sha256=sha, size_bytes=64, repo_id="fam")
    conn.commit()
    conn.close()
    return cfg, db


def _fake_create_iso(self, source_dir, output_iso, volume_label, **_kw):
    Path(output_iso).write_bytes(b"\x00" * 2048)
    return Path(output_iso)


def _counts(db: Path) -> tuple[int, int]:
    conn = get_connection(db)
    try:
        return (
            conn.execute("SELECT COUNT(*) FROM volumes").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM burn_sessions").fetchone()[0],
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------
# --for-location must be registered
# ---------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_unknown_for_location_is_refused(tmp_path, caplog, dry_run):
    cfg, db = _setup(tmp_path)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lcsas"):
        rc = main(["--config", str(cfg), "stage", "--for-location", "Hoem",
                   *(["--dry-run"] if dry_run else [])])
    assert rc == 1
    assert ("--for-location: location 'Hoem' is not registered "
            "(did you mean: Home?)") in caplog.text
    assert "Registered locations: Bank_Box, Home." in caplog.text
    assert "lcsas location add Hoem" in caplog.text
    assert "[DRY RUN]" not in caplog.text
    assert _counts(db) == (0, 0)


def test_unknown_for_location_with_no_location_registered(tmp_path, caplog):
    cfg, db = _setup(tmp_path)
    conn = get_connection(db)
    conn.execute("DELETE FROM locations")
    conn.commit()
    conn.close()
    caplog.clear()
    rc = main(["--config", str(cfg), "stage", "--for-location", "Home"])
    assert rc == 1
    assert "No location is registered yet." in caplog.text
    assert _counts(db) == (0, 0)


def test_registered_for_location_stages(tmp_path, caplog):
    cfg, db = _setup(tmp_path)
    with patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.create_iso",
               _fake_create_iso):
        rc = main(["--config", str(cfg), "stage", "--for-location", "Home"])
    assert rc == 0, caplog.text
    conn = get_connection(db)
    try:
        assert [r[0] for r in conn.execute(
            "SELECT location FROM volumes")] == ["Home"]
    finally:
        conn.close()


# ---------------------------------------------------------------------
# A dry run checks password split drift like the real run
# ---------------------------------------------------------------------


def test_dry_run_refuses_on_escrow_drift_like_the_real_run(tmp_path, caplog):
    cfg, db = _setup(tmp_path, key_split=True)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lcsas"):
        rc = main(["--config", str(cfg), "stage", "--dry-run"])
    assert rc == 1
    assert "password split drift for repository 'fam'" in caplog.text
    assert "Pass --allow-escrow-drift to override" in caplog.text
    assert "volume(s) planned" not in caplog.text
    assert _counts(db) == (0, 0)

    caplog.clear()
    with patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.create_iso",
               _fake_create_iso):
        assert main(["--config", str(cfg), "stage"]) == 1
    assert "password split drift for repository 'fam'" in caplog.text
    assert _counts(db)[0] == 0


def test_dry_run_with_allow_escrow_drift_warns_and_plans(tmp_path, caplog):
    cfg, db = _setup(tmp_path, key_split=True)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lcsas"):
        rc = main(["--config", str(cfg), "stage", "--dry-run",
                   "--allow-escrow-drift"])
    assert rc == 0, caplog.text
    assert "password split drift for repository 'fam'" in caplog.text
    assert "proceeding: --allow-escrow-drift" in caplog.text
    assert "[DRY RUN] 1 volume(s) planned" in caplog.text
    assert _counts(db) == (0, 0)


def test_dry_run_without_drift_plans(tmp_path, caplog):
    cfg, db = _setup(tmp_path)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lcsas"):
        rc = main(["--config", str(cfg), "stage", "--dry-run"])
    assert rc == 0, caplog.text
    assert "drift" not in caplog.text
    assert "[DRY RUN] 1 volume(s) planned" in caplog.text
