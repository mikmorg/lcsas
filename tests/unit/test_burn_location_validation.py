"""Tests for `lcsas burn --location` strictness against unknown names (#19).

The burn CLI must reject typos in `--location` rather than silently
auto-creating a phantom location row in the catalog. Explicit
`--create-location` is the only way to mint a new location during burn.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.locations import create_location, list_locations
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all


def _write_config(tmp_path: Path) -> Path:
    """Write a minimal TOML config and return its path."""
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    db = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(f"""
[paths]
mirror_base = "{mirror}"
staging = "{staging}"
database = "{db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
""")
    return cfg


def _init_db(tmp_path: Path) -> Path:
    """Initialise the catalog DB and register a minimal repo."""
    db = tmp_path / "archive.db"
    conn = get_connection(db)
    create_all(conn)
    register_repo(conn, "fam", "fam", str(tmp_path / "mirror"), "")
    conn.close()
    return db


class TestBurnLocationStrict:
    """`lcsas burn --location <unknown>` must error without auto-creating."""

    def test_unknown_location_rejected_no_phantom_row(self, tmp_path, capsys):
        """Test A: typo in --location -> exit non-zero, no row inserted."""
        cfg = _write_config(tmp_path)
        db = _init_db(tmp_path)

        # Pre-create the "real" location so the fix has something to
        # suggest as a close match.
        conn = get_connection(db)
        create_location(conn, "home_safe", "Real location")
        conn.close()

        # User typos: "home-safe" instead of "home_safe"
        # Mock burn_session so we never get past location resolution
        # (a real burn would need an optical device).
        with patch(
            "lcsas.burn.orchestrator.BurnOrchestrator.burn_session"
        ) as mock_burn:
            result = main([
                "--config", str(cfg),
                "burn", "--session", "latest",
                "--location", "home-safe",
            ])

        assert result == 1, "burn must exit non-zero on unknown location"

        out = capsys.readouterr().out
        assert "home-safe" in out, "error message must mention the typo'd name"
        # Error should hint at the close match.
        assert "home_safe" in out, (
            "error message should suggest the close existing location"
        )

        # Critical: no phantom location row was inserted.
        conn = get_connection(db)
        try:
            names = {loc.name for loc in list_locations(conn)}
        finally:
            conn.close()
        assert "home-safe" not in names, (
            "phantom location was silently created on typo"
        )
        assert names == {"home_safe"}, (
            f"only the pre-existing location should remain, got {names}"
        )

        # And burn_session must NOT have been called — we rejected early.
        mock_burn.assert_not_called()

    def test_existing_location_still_works(self, tmp_path, capsys):
        """Test B: --location <existing> still passes through normally."""
        cfg = _write_config(tmp_path)
        db = _init_db(tmp_path)

        conn = get_connection(db)
        create_location(conn, "home_safe", "Real location")
        conn.close()

        # Mock burn_session so we don't need a real optical device,
        # and skip the device existence check by mocking os.path.exists
        # for the device path only.
        with (
            patch(
                "lcsas.burn.orchestrator.BurnOrchestrator.burn_session",
                return_value=[],
            ) as mock_burn,
            patch("os.path.exists", return_value=True),
        ):
            result = main([
                "--config", str(cfg),
                "burn", "--session", "latest",
                "--location", "home_safe",
            ])

        assert result == 0, "burn with an existing location must succeed"
        mock_burn.assert_called_once()
        # The location passed through to burn_session must be the
        # validated name verbatim.
        _, kwargs = mock_burn.call_args
        assert kwargs.get("location") == "home_safe"

    def test_create_location_flag_creates_and_uses_new_location(
        self, tmp_path, capsys,
    ):
        """Test C: --create-location explicitly mints the new location."""
        cfg = _write_config(tmp_path)
        db = _init_db(tmp_path)

        with (
            patch(
                "lcsas.burn.orchestrator.BurnOrchestrator.burn_session",
                return_value=[],
            ) as mock_burn,
            patch("os.path.exists", return_value=True),
        ):
            result = main([
                "--config", str(cfg),
                "burn", "--session", "latest",
                "--location", "new-place",
                "--create-location",
            ])

        assert result == 0, "burn with --create-location must succeed"
        mock_burn.assert_called_once()
        _, kwargs = mock_burn.call_args
        assert kwargs.get("location") == "new-place"

        # The new location must now be in the catalog.
        conn = get_connection(db)
        try:
            names = {loc.name for loc in list_locations(conn)}
        finally:
            conn.close()
        assert "new-place" in names, (
            "--create-location must register the new location row"
        )
