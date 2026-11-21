"""No command creates a catalog beside an old ``archive.db`` (#934 upgrade).

The master catalog's default name changed from ``archive.db`` to
``catalog.db`` in rc11.  The pre-final upgrade rehearsal (2026-11-21)
showed the trap: an install that renamed ``[paths] database =
.../archive.db`` to ``catalog = .../catalog.db`` but not the file got a
second, empty catalog from its first ``scan``, which hid every volume of
the real one.  Now every command refuses, and the hint says to rename the
file, not to run ``lcsas init``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcsas.cli.main import main


def _config(tmp_path: Path, catalog: Path) -> Path:
    mirror = tmp_path / "mirror"
    (mirror / "alpha").mkdir(parents=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        "[paths]\n"
        f'mirror_base = "{mirror}"\n'
        f'staging = "{staging}"\n'
        f'catalog = "{catalog}"\n'
    )
    return cfg


@pytest.fixture
def upgraded(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A config renamed to catalog.db while the file is still archive.db."""
    old = tmp_path / "db" / "archive.db"
    old.parent.mkdir()
    assert main(["--catalog", str(old), "init"]) == 0
    catalog = old.with_name("catalog.db")
    return _config(tmp_path, catalog), catalog, old


@pytest.mark.parametrize(
    "argv",
    [
        ["init"],
        ["scan"],
        ["status"],
        ["stage", "--media", "TEST_TINY", "--dry-run"],
        ["repo", "add", "alpha", "MIRROR/alpha"],
        ["session", "list"],
        ["location", "list"],
    ],
    ids=lambda a: " ".join(a[:2]),
)
def test_no_command_creates_a_catalog_beside_archive_db(
    tmp_path: Path,
    upgraded: tuple[Path, Path, Path],
    argv: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg, catalog, old = upgraded
    argv = [a.replace("MIRROR", str(tmp_path / "mirror")) for a in argv]
    rc = main(["--config", str(cfg), *argv])
    assert rc != 0
    assert not catalog.exists(), "a second, empty catalog was created"
    assert f"No catalog at {catalog}" in caplog.text
    assert f"{old} is there" in caplog.text
    assert "Rename that file to catalog.db" in caplog.text
    assert "Do not run `lcsas init`" in caplog.text


def test_after_the_rename_it_opens(upgraded: tuple[Path, Path, Path]) -> None:
    cfg, catalog, old = upgraded
    old.rename(catalog)
    assert main(["--config", str(cfg), "status"]) == 0
    assert main(["--config", str(cfg), "scan"]) == 0


def test_a_fresh_install_still_gets_its_catalog(tmp_path: Path) -> None:
    catalog = tmp_path / "db" / "catalog.db"
    cfg = _config(tmp_path, catalog)
    assert main(["--config", str(cfg), "init"]) == 0
    assert catalog.exists()
