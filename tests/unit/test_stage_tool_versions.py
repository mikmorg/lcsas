"""#731: ``lcsas stage`` refuses an xorriso or dvdisaster that is too old.

The check ran only in the single-volume ``execute()`` path removed by
#699.  ``stage()`` now runs it before any side effect; a dry run does not
run the tools, so it does not need them.  The tools here are small fake
executables that print a version, put first on PATH.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.orchestrator import (
    DVDISASTER_MIN_VERSION,
    XORRISO_MIN_VERSION,
    BurnOrchestrator,
)
from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
from lcsas.exceptions import BinaryError
from lcsas.iso.xorriso import SubprocessXorrisoRunner

# The real check; tests/unit/conftest.py stubs it for every other test.
_REAL_PREFLIGHT = BurnOrchestrator._tool_version_preflight


@pytest.fixture(autouse=True)
def _real_check(monkeypatch):
    monkeypatch.setattr(BurnOrchestrator, "_tool_version_preflight",
                        _REAL_PREFLIGHT)


def _tool(bin_dir: Path, name: str, banner: str) -> Path:
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / name
    p.write_text(f"#!/bin/sh\necho '{banner}'\n")
    p.chmod(0o755)
    return p


def _orch(xorriso: Path, dvdisaster: Path) -> BurnOrchestrator:
    return BurnOrchestrator(
        MagicMock(), MagicMock(),
        SubprocessXorrisoRunner(str(xorriso)),
        SubprocessDVDisasterRunner(str(dvdisaster)),
    )


def test_minimums_are_the_documented_ones():
    assert XORRISO_MIN_VERSION == (1, 4, 0)
    assert DVDISASTER_MIN_VERSION == (0, 79, 0)


def test_old_xorriso_is_refused(tmp_path):
    bins = tmp_path / "bin"
    orch = _orch(_tool(bins, "xorriso", "xorriso 1.3.8 : RockRidge"),
                 _tool(bins, "dvdisaster", "dvdisaster 0.79.10"))
    with pytest.raises(BinaryError, match=r"version 1\.3\.8 is too old; 1\.4\.0\+"):
        orch._tool_version_preflight(apply_ecc=False)


def test_old_dvdisaster_is_refused_only_with_ecc(tmp_path):
    bins = tmp_path / "bin"
    orch = _orch(_tool(bins, "xorriso", "xorriso 1.5.6 : RockRidge"),
                 _tool(bins, "dvdisaster", "dvdisaster 0.72.4"))
    with pytest.raises(BinaryError, match=r"version 0\.72\.4 is too old; 0\.79\.0\+"):
        orch._tool_version_preflight(apply_ecc=True)
    # No ECC on this media: dvdisaster is not run, so not checked.
    orch._tool_version_preflight(apply_ecc=False)


def test_new_enough_tools_pass(tmp_path):
    bins = tmp_path / "bin"
    orch = _orch(_tool(bins, "xorriso", "xorriso 1.5.6 : RockRidge"),
                 _tool(bins, "dvdisaster", "dvdisaster 0.79.10 (unstable)"))
    orch._tool_version_preflight(apply_ecc=True)


def test_missing_xorriso_is_refused(tmp_path):
    orch = _orch(tmp_path / "no-such-xorriso", tmp_path / "no-such-dvdisaster")
    with pytest.raises(BinaryError, match="not found"):
        orch._tool_version_preflight(apply_ecc=False)


def test_fakes_are_not_checked():
    """An injected fake runner runs no binary, so it is not checked."""
    orch = BurnOrchestrator(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    orch._tool_version_preflight(apply_ecc=True)


# ---------------------------------------------------------------------------
# Through the CLI
# ---------------------------------------------------------------------------


def _catalog(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir()
    mirror = tmp_path / "mirror" / "fam"
    for sub in ("index", "snapshots", "keys"):
        (mirror / sub).mkdir(parents=True)
        (mirror / sub / "dummy").write_text("{}")
    (mirror / "config").write_text("{}")
    content = b"x" * 64
    sha = hashlib.sha256(content).hexdigest()
    (mirror / "data" / sha[:2]).mkdir(parents=True)
    (mirror / "data" / sha[:2] / sha).write_bytes(content)
    db = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
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

[repos.fam]
mirror_path = "{mirror}"
""")
    conn = get_connection(db)
    create_all(conn)
    register_repo(conn, "fam", "fam", str(mirror))
    register_pack(conn, sha256=sha, size_bytes=64, repo_id="fam")
    conn.commit()
    conn.close()
    return cfg


def _sessions(cfg: Path) -> int:
    conn = get_connection(cfg.parent / "archive.db")
    try:
        return int(conn.execute("SELECT COUNT(*) FROM burn_sessions").fetchone()[0])
    finally:
        conn.close()


def _old_xorriso_first_on_path(tmp_path: Path, monkeypatch) -> None:
    bins = tmp_path / "bin"
    _tool(bins, "xorriso", "xorriso 1.3.8 : RockRidge")
    monkeypatch.setenv("PATH", f"{bins}{os.pathsep}{os.environ['PATH']}")


def test_stage_cli_refuses_old_xorriso(tmp_path, monkeypatch, capsys):
    cfg = _catalog(tmp_path)
    _old_xorriso_first_on_path(tmp_path, monkeypatch)

    assert main(["--config", str(cfg), "stage"]) == 1
    out = capsys.readouterr()
    text = out.out + out.err
    assert "'xorriso' version 1.3.8 is too old; 1.4.0+ is required." in text
    assert "Unexpected error" not in text
    assert _sessions(cfg) == 0  # refused before any side effect


def test_stage_dry_run_needs_no_tools(tmp_path, monkeypatch, capsys):
    cfg = _catalog(tmp_path)
    _old_xorriso_first_on_path(tmp_path, monkeypatch)

    assert main(["--config", str(cfg), "stage", "--dry-run"]) == 0
    out = capsys.readouterr()
    assert "too old" not in out.out + out.err
    assert _sessions(cfg) == 0
