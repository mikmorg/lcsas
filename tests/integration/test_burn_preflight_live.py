"""FMT-03 integration: the format preflight passes a REAL rustic mirror.

Creates a genuine rustic repository with the rustic binary, registers it in
an LCSAS catalog, and drives ``BurnOrchestrator.stage`` end-to-end. The new
format preflight (``check_repo_recoverable``) must decode-prove the live repo
(config v1/v2 + index + one blob) and let staging proceed — proving the gate
does not false-positive against real-world rustic output.

Requires ``rustic`` (or ``restic``) and ``xorriso`` on PATH; skipped otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_connection
from lcsas.db.queries import get_unarchived_packs
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.packs.delta import DeltaAnalyzer
from lcsas.packs.scanner import scan_mirror_packs

_RESTIC_BIN = shutil.which("rustic") or shutil.which("restic") or ""

requires_restic_binary = pytest.mark.skipif(
    not _RESTIC_BIN, reason="neither rustic nor restic installed",
)
requires_xorriso = pytest.mark.skipif(
    not shutil.which("xorriso"), reason="xorriso not installed",
)
pytestmark = [pytest.mark.integration, requires_restic_binary, requires_xorriso]


def _restic(args: list[str], repo: Path, key: Path, tmpdir: Path) -> None:
    cmd = [_RESTIC_BIN, "-r", str(repo), "--password-file", str(key), *args]
    env = {**os.environ, "TMPDIR": str(tmpdir)}
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if res.returncode != 0:
        raise subprocess.CalledProcessError(
            res.returncode, cmd, output=res.stdout, stderr=res.stderr,
        )


class _RealXorriso:
    def create_iso(self, source_dir, output_iso, volume_label, **_kw):
        subprocess.run(
            ["xorriso", "-as", "mkisofs", "-r", "-J", "-joliet-long",
             "-iso-level", "3", "-V", volume_label,
             "-o", str(output_iso), str(source_dir)],
            capture_output=True, text=True, check=True,
        )
        return output_iso

    def burn_iso(self, *a, **kw):
        pass

    def verify_disc(self, *a, **kw):
        return True


class _NoOpDVDisaster:
    def augment_iso(self, *a, **kw):
        pass


def test_stage_passes_format_preflight_real_rustic(tmp_path):
    """A real rustic mirror decode-proves and stages without a FormatDriftError."""
    mirror = tmp_path / "mirror"
    repo = mirror / "family"
    repo.mkdir(parents=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    db_path = tmp_path / "archive.db"

    key = tmp_path / "key.txt"
    key.write_text("integration-preflight-password\n")

    src = tmp_path / "src"
    src.mkdir()
    for i in range(6):
        (src / f"file_{i}.bin").write_bytes(os.urandom(2048))

    _restic(["init"], repo, key, tmp_path)
    _restic(["backup", "--json", str(src)], repo, key, tmp_path)

    conn = get_connection(db_path)
    create_all(conn)
    register_repo(conn, "family", "Family", str(repo))
    scanned = scan_mirror_packs(repo).packs
    DeltaAnalyzer(conn, scanned, repo_id="family").register_new_packs()
    assert get_unarchived_packs(conn), "expected unarchived packs to stage"

    config = LCSASConfig(
        mirror_base_path=mirror,
        staging_path=staging,
        db_path=db_path,
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        label_prefix="FMTLIVE",
        metadata_reserve_bytes=50_000,
        repositories={
            "family": RepositoryConfig(
                name="family", mirror_path=repo, password_file=key,
            ),
        },
    )
    orch = BurnOrchestrator(config, conn, _RealXorriso(), _NoOpDVDisaster())

    # Must run the preflight (real config v1/v2 + index + blob decode) and
    # proceed all the way through staging + ISO creation.
    result = orch.stage()
    conn.close()

    assert result.manifests
    for iso in result.iso_paths:
        assert iso.exists(), f"ISO not produced: {iso}"
