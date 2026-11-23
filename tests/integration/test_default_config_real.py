"""An owner who never types ``--config`` (#964), on real rustic and xorriso.

The config sits at ``~/.config/lcsas/lcsas.toml`` (HOME is a temp folder)
and every command goes through ``lcsas.cli.main.main`` with no
``--config``: init, repo add, scan, stage.  ``--config`` still wins over
the default, and no command but ``init`` makes a catalog.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from lcsas.cli.main import main
from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


class DefaultConfigInstall(ScratchInstall):
    """A scratch install whose config is at the default path and whose
    commands never name it."""

    def lcsas(self, *argv: str) -> int:
        return main(list(argv))


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("LCSAS_CONFIG", raising=False)
    monkeypatch.chdir(cwd)
    return h


def test_init_repo_add_scan_stage_with_no_config_flag(
    home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    root = home / ".config" / "lcsas"
    inst = DefaultConfigInstall(root, repos=("alpha",))
    assert inst.config == root / "lcsas.toml"
    assert inst.db.is_file(), "init did not make the configured catalog"

    inst.backup("alpha", "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.query("SELECT COUNT(*) FROM packs")[0][0] > 0
    assert inst.lcsas("stage") == 0
    assert inst.labels(), "stage made no volume"
    assert inst.disc_trees(), "stage left no disc tree"
    assert inst.lcsas("status") == 0

    # Nothing was written in the folder the owner stood in.
    assert list((tmp_path / "cwd").iterdir()) == []

    # --config outranks the default: another config, another catalog.
    other_db = tmp_path / "other" / "catalog.db"
    other = tmp_path / "other.toml"
    other.write_text(
        inst.config.read_text().replace(str(inst.db), str(other_db))
    )
    caplog.clear()
    assert main(["--config", str(other), "repo", "list"]) == 1
    assert f"No catalog at {other_db}" in caplog.text
    assert "Run `lcsas init` first" in caplog.text
    assert not other_db.exists()
    assert main(["--config", str(other), "init"]) == 0
    assert other_db.is_file()
    assert main(["--config", str(other), "repo", "add", "alpha",
                 str(inst.mirror("alpha"))]) == 0
    assert inst.repo_id("alpha")  # the default catalog still has its set
    assert main(["--config", str(other), "scan"]) == 0


def test_without_init_the_default_config_makes_no_catalog(
    home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Every write command on a fresh install, before ``init``: each is
    refused with the hint, and the configured catalog stays absent."""
    root = home / ".config" / "lcsas"
    # The config only: ScratchInstall.__init__ would run init.
    db = tmp_path / "db" / "catalog.db"
    db.parent.mkdir()
    mirror = tmp_path / "mirror" / "alpha"
    mirror.mkdir(parents=True)
    key = tmp_path / "alpha.key"
    key.write_text("scratch-password\n")
    subprocess.run(["rustic", "-r", str(mirror), "--password-file", str(key),
                    "init"], check=True, capture_output=True,
                   env={**os.environ, "HOME": str(tmp_path)})
    root.mkdir(parents=True)
    (root / "lcsas.toml").write_text(
        "[paths]\n"
        f'mirror_base = "{mirror.parent}"\n'
        f'staging = "{db.parent}"\n'
        f'catalog = "{db}"\n'
        "[defaults]\n"
        'media_type = "TEST_TINY"\n'
        "metadata_reserve_mb = 0\n"
        "[survivability]\n"
        'password_hints = "In the safe"\n'
        "[repos.alpha]\n"
        f'mirror_path = "{mirror}"\n'
        f'password_file = "{key}"\n'
    )
    for argv in (["repo", "add", "alpha", str(mirror)], ["scan"],
                 ["stage", "--dry-run"], ["session", "list"],
                 ["volume", "impact", "LCSAS_X_001"], ["verify", "--all"]):
        caplog.clear()
        assert main(argv) != 0, argv
        assert f"No catalog at {db}" in caplog.text, argv
        assert not db.exists(), argv
