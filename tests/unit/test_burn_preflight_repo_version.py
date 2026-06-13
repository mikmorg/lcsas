"""FMT-03: burn-time gate against rustic writer / pinned-reader format drift.

These tests build **synthetic restic repositories** with the same
authenticated-encryption scheme real rustic uses (reusing the crypto helpers
proven by ``test_restic_fallback``), then drive the format preflight both
directly and through ``BurnOrchestrator.stage`` to prove:

  - a v3-config mirror is refused BEFORE any ISO is mastered;
  - an unknown compression-framing byte in a sampled file is refused;
  - healthy v1 and v2 mirrors pass (the decode proof reaches the blob check);
  - a repo with no password_file is refused unless the override is set.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.format_preflight import (
    SUPPORTED_REPO_VERSIONS,
    FormatDriftError,
    check_repo_recoverable,
)
from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_memory_connection
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all

# Reuse the audited crypto helpers from the fallback test module.
from tests.unit.test_restic_fallback import (
    MASTER_ENCRYPT,
    MASTER_MAC_K,
    MASTER_MAC_R,
    PASSWORD,
    MasterKey,
    _encrypt_with_master,
    _make_key_file,
)


def _build_repo(
    repo_dir: Path,
    *,
    version: int = 2,
    bad_compression: bool = False,
) -> tuple[str, bytes]:
    """Build a minimal but real restic repo and return (pack_sha256, content).

    The pack file is named by the SHA-256 of its (encrypted) bytes — exactly
    as rustic stores it — so the catalog pack hash registered against this
    repo also satisfies the staging hash check.

    Args:
        version: value written into the (encrypted) ``config`` file.
        bad_compression: if True, prefix the index plaintext with an unknown
            compression-type byte (0x03) so the decode proof must fail.
    """
    repo_dir.mkdir(parents=True, exist_ok=True)

    mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)
    _make_key_file(mk, PASSWORD, repo_dir)

    # One data blob.
    blob_content = b"FMT-03 decode-proof blob\n"
    blob_id = hashlib.sha256(blob_content).hexdigest()
    encrypted_blob = _encrypt_with_master(blob_content)

    pack_data = bytes(encrypted_blob)
    pack_id = hashlib.sha256(pack_data).hexdigest()
    data_dir = repo_dir / "data" / pack_id[:2]
    data_dir.mkdir(parents=True)
    (data_dir / pack_id).write_bytes(pack_data)

    # Index mapping the blob → its pack location.
    index_doc = json.dumps({
        "packs": [{
            "id": pack_id,
            "blobs": [{
                "id": blob_id,
                "type": "data",
                "offset": 0,
                "length": len(encrypted_blob),
            }],
        }],
    }).encode()
    index_dir = repo_dir / "index"
    index_dir.mkdir()
    index_plain = (b"\x03" + index_doc) if bad_compression else index_doc
    index_id = hashlib.sha256(index_plain).hexdigest()
    (index_dir / index_id).write_bytes(_encrypt_with_master(index_plain))

    # snapshots dir (PurePythonRestorer tolerates it being empty).
    (repo_dir / "snapshots").mkdir()

    # Encrypted config carrying the format version.
    config_doc = json.dumps({
        "version": version,
        "id": "fmt03-test-repo-id-0123456789abcdef",
        "chunker_polynomial": "3DA3358B4DC173",
    }).encode()
    (repo_dir / "config").write_bytes(_encrypt_with_master(config_doc))

    return pack_id, pack_data


def _password_file(tmp_path: Path) -> Path:
    pw = tmp_path / "repo.key"
    pw.write_bytes(PASSWORD)
    return pw


# ── Direct check_repo_recoverable() tests ────────────────────────────


@pytest.mark.parametrize("version", [1, 2])
def test_supported_version_passes(tmp_path, version):
    """v1 and v2 mirrors decode-prove cleanly (config + index + blob)."""
    repo = tmp_path / "repo"
    _build_repo(repo, version=version)
    # Must not raise.
    check_repo_recoverable(repo, _password_file(tmp_path))


def test_v3_config_refused(tmp_path):
    """A v3-config repo is refused with the supported-versions message."""
    repo = tmp_path / "repo"
    _build_repo(repo, version=3)
    with pytest.raises(FormatDriftError) as exc:
        check_repo_recoverable(repo, _password_file(tmp_path))
    msg = str(exc.value)
    assert "version 3" in msg
    assert "1-2" in msg


def test_unknown_compression_byte_refused(tmp_path):
    """An unknown compression-type byte in the index aborts the decode proof."""
    repo = tmp_path / "repo"
    _build_repo(repo, version=2, bad_compression=True)
    with pytest.raises(FormatDriftError) as exc:
        check_repo_recoverable(repo, _password_file(tmp_path))
    assert "cannot decode" in str(exc.value)


def test_missing_password_file_refused(tmp_path):
    """A configured-but-absent password_file fails loud, not with a stray IOError."""
    repo = tmp_path / "repo"
    _build_repo(repo, version=2)
    with pytest.raises(FormatDriftError):
        check_repo_recoverable(repo, tmp_path / "nope.key")


def test_wrong_password_refused(tmp_path):
    """A password that cannot unlock the master key is refused."""
    repo = tmp_path / "repo"
    _build_repo(repo, version=2)
    bad_pw = tmp_path / "bad.key"
    bad_pw.write_bytes(b"not-the-password")
    with pytest.raises(FormatDriftError) as exc:
        check_repo_recoverable(repo, bad_pw)
    assert "could not be unlocked" in str(exc.value)


def test_empty_repo_version_only(tmp_path):
    """An index-less repo passes on version alone (decode proof skipped)."""
    repo = tmp_path / "repo"
    _build_repo(repo, version=2)
    # Remove the index entirely → no decode proof possible, but version is OK.
    for f in (repo / "index").iterdir():
        f.unlink()
    (repo / "index").rmdir()
    check_repo_recoverable(repo, _password_file(tmp_path))


def test_supported_versions_contract():
    """The frozen contract is exactly (1, 2)."""
    assert SUPPORTED_REPO_VERSIONS == (1, 2)


# ── Orchestrator-level tests (abort BEFORE ISO creation) ─────────────


def _orch_env(tmp_path, *, version, password_file, bad_compression=False,
              allow_override=False):
    """Wire a BurnOrchestrator over a real synthetic repo + one unarchived pack."""
    mirror = tmp_path / "mirror"
    staging = tmp_path / "staging"
    staging.mkdir(parents=True)
    repo_dir = mirror / "family"
    pack_id, pack_data = _build_repo(
        repo_dir, version=version, bad_compression=bad_compression,
    )

    db_path = tmp_path / "archive.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Real on-disk DB so holographic injection (if reached) can copy it.
    from lcsas.db.connection import get_connection
    disk = get_connection(db_path)
    create_all(disk)
    disk.close()

    conn = get_memory_connection()
    create_all(conn)
    register_repo(conn, "family", "Family", str(repo_dir))
    # The catalog pack hash IS the pack-file name, so staging can find +
    # hash-verify it if the preflight passes.
    register_pack(conn, sha256=pack_id, size_bytes=len(pack_data),
                  repo_id="family")

    config = LCSASConfig(
        mirror_base_path=mirror,
        staging_path=staging,
        db_path=db_path,
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        metadata_reserve_bytes=1000,
        label_prefix="TEST",
        repositories={
            "family": RepositoryConfig(
                name="family", mirror_path=repo_dir,
                password_file=password_file,
            ),
        },
        allow_unverified_repo_format=allow_override,
    )

    xorriso = MagicMock()

    def _fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _fake_create_iso
    dvdisaster = MagicMock()
    orch = BurnOrchestrator(config, conn, xorriso, dvdisaster)
    return orch, xorriso


def test_stage_aborts_on_v3_before_iso(tmp_path):
    """`stage()` against a v3 mirror raises and never calls create_iso."""
    orch, xorriso = _orch_env(
        tmp_path, version=3, password_file=_password_file(tmp_path),
    )
    with pytest.raises(FormatDriftError):
        orch.stage()
    xorriso.create_iso.assert_not_called()


def test_stage_aborts_on_unknown_compression_before_iso(tmp_path):
    """An unknown compression byte aborts staging before any ISO is mastered."""
    orch, xorriso = _orch_env(
        tmp_path, version=2, password_file=_password_file(tmp_path),
        bad_compression=True,
    )
    with pytest.raises(FormatDriftError):
        orch.stage()
    xorriso.create_iso.assert_not_called()


def test_stage_passes_on_v2_reaches_iso(tmp_path):
    """A healthy v2 mirror passes the preflight and proceeds to ISO creation."""
    orch, xorriso = _orch_env(
        tmp_path, version=2, password_file=_password_file(tmp_path),
    )
    result = orch.stage()
    assert result.manifests
    xorriso.create_iso.assert_called()


def test_stage_aborts_when_no_password_file(tmp_path):
    """No password_file → refusal, no ISO, unless override is set."""
    orch, xorriso = _orch_env(
        tmp_path, version=2, password_file=None,
    )
    with pytest.raises(FormatDriftError) as exc:
        orch.stage()
    assert "no password_file" in str(exc.value)
    xorriso.create_iso.assert_not_called()


def test_stage_override_allows_no_password_file(tmp_path):
    """With allow_unverified_repo_format, a password-less repo still burns."""
    orch, xorriso = _orch_env(
        tmp_path, version=2, password_file=None, allow_override=True,
    )
    result = orch.stage()
    assert result.manifests
    xorriso.create_iso.assert_called()
