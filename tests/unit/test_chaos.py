"""Chaos & fault-injection tests for LCSAS restore resilience.

These tests intentionally corrupt, truncate, or remove data at
various layers of the restore pipeline and verify that the system
detects the damage with clear error messages rather than silently
producing incorrect output.

Test categories:
    - Truncated pack files
    - Corrupt pack contents (bit-flip)
    - Missing pack files
    - Missing index entries
    - Partial zstd decompression failures
    - Empty / malformed repository structures
    - Post-ingest completeness checks
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from lcsas.restore._aes_pure import (
    aes_ctr,
    aes_encrypt_block,
    key_schedule,
)
from lcsas.restore.executor import (
    PackCorruptionError,
    RestoreExecutor,
)
from lcsas.restore.restic_fallback import (
    IntegrityError,
    MasterKey,
    PurePythonRestorer,
    _decrypt_authenticated,
    _poly1305_mac,
)

# ═════════════════════════════════════════════════════════════════
# Test helpers — synthetic repo builder (borrowed from test_restic_fallback.py)
# ═════════════════════════════════════════════════════════════════

PASSWORD = b"chaos-test-password"

MASTER_ENCRYPT = bytes(range(32))
MASTER_MAC_K = bytes(range(16, 32))
MASTER_MAC_R = bytes([
    0x85, 0xd6, 0xbe, 0x78, 0x57, 0x55, 0x6d, 0x33,
    0x7f, 0x44, 0x52, 0xfe, 0x42, 0xd5, 0x06, 0xa8,
])


def _encrypt_data(
    encrypt_key: bytes,
    mac_k: bytes,
    mac_r: bytes,
    plaintext: bytes,
    iv: bytes | None = None,
) -> bytes:
    if iv is None:
        iv = os.urandom(16)
    ciphertext = aes_ctr(encrypt_key, iv, plaintext)
    mac_rk = key_schedule(mac_k)
    s = aes_encrypt_block(iv, mac_rk)
    tag = _poly1305_mac(mac_r, s, ciphertext)
    return iv + ciphertext + tag


def _encrypt_with_master(plaintext: bytes) -> bytes:
    return _encrypt_data(MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, plaintext)


def _make_key_file(mk: MasterKey, password: bytes, repo: Path) -> Path:
    n, r, p = 1024, 8, 1
    salt = os.urandom(64)
    derived = hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, dklen=64)
    master_json = json.dumps({
        "encrypt": base64.b64encode(mk.encrypt).decode(),
        "mac": {
            "k": base64.b64encode(mk.mac_k).decode(),
            "r": base64.b64encode(mk.mac_r).decode(),
        },
    }).encode()
    encrypted = _encrypt_data(derived[:32], derived[32:48], derived[48:64], master_json)
    key_doc = {
        "created": "2026-01-01T00:00:00Z",
        "username": "test", "hostname": "testhost",
        "kdf": "scrypt", "N": n, "r": r, "p": p,
        "salt": base64.b64encode(salt).decode(),
        "data": base64.b64encode(encrypted).decode(),
    }
    keys_dir = repo / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    kf = keys_dir / "chaoskey01"
    kf.write_text(json.dumps(key_doc))
    return kf


def _build_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build a minimal synthetic repo.

    Returns (repo_dir, password_file, pack_id).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)
    _make_key_file(mk, PASSWORD, repo)

    # Single file: hello.txt
    file_content = b"Hello from chaos tests!\n"
    file_id = hashlib.sha256(file_content).hexdigest()

    root_tree = json.dumps({
        "nodes": [{
            "name": "hello.txt",
            "type": "file",
            "mode": 0o644,
            "mtime": "2026-01-01T00:00:00.000000000Z",
            "atime": "2026-01-01T00:00:00.000000000Z",
            "ctime": "2026-01-01T00:00:00.000000000Z",
            "uid": 1000, "gid": 1000,
            "size": len(file_content),
            "content": [file_id],
        }],
    }).encode()
    root_tree_id = hashlib.sha256(root_tree).hexdigest()

    # Build pack
    blobs_info: list[dict] = []
    pack_data = bytearray()
    for content, blob_id, btype in [
        (file_content, file_id, "data"),
        (root_tree, root_tree_id, "tree"),
    ]:
        enc = _encrypt_with_master(content)
        blobs_info.append({
            "id": blob_id,
            "type": btype,
            "offset": len(pack_data),
            "length": len(enc),
        })
        pack_data.extend(enc)

    pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
    data_dir = repo / "data" / pack_id[:2]
    data_dir.mkdir(parents=True)
    (data_dir / pack_id).write_bytes(bytes(pack_data))

    # Index
    index_doc = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
    index_dir = repo / "index"
    index_dir.mkdir()
    idx_id = hashlib.sha256(index_doc).hexdigest()
    (index_dir / idx_id).write_bytes(_encrypt_with_master(index_doc))

    # Snapshot
    snap_doc = json.dumps({
        "time": "2026-01-01T01:00:00.000000000Z",
        "tree": root_tree_id,
        "paths": ["/test"],
        "hostname": "chaoshost",
        "username": "test",
    }).encode()
    snap_dir = repo / "snapshots"
    snap_dir.mkdir()
    snap_id = hashlib.sha256(snap_doc).hexdigest()
    (snap_dir / snap_id).write_bytes(_encrypt_with_master(snap_doc))

    # Config
    config_doc = json.dumps({"version": 2, "id": "testrepo01"}).encode()
    (repo / "config").write_bytes(_encrypt_with_master(config_doc))

    # Password file
    pw_file = tmp_path / "password.txt"
    pw_file.write_bytes(PASSWORD)

    return repo, pw_file, pack_id


# ═════════════════════════════════════════════════════════════════
# Truncated pack files
# ═════════════════════════════════════════════════════════════════


class TestTruncatedPacks:
    """Verify restorer detects truncated pack files."""

    def test_truncated_to_zero_bytes(self, tmp_path):
        """A pack truncated to 0 bytes should raise an error."""
        repo, pw, pack_id = _build_repo(tmp_path)
        pack_path = repo / "data" / pack_id[:2] / pack_id
        pack_path.write_bytes(b"")  # truncate to zero

        restorer = PurePythonRestorer(repo, pw)
        with pytest.raises((IntegrityError, Exception)):
            restorer.restore(target=tmp_path / "out")

    def test_truncated_to_half(self, tmp_path):
        """A pack truncated to half its size should raise an error."""
        repo, pw, pack_id = _build_repo(tmp_path)
        pack_path = repo / "data" / pack_id[:2] / pack_id
        original = pack_path.read_bytes()
        pack_path.write_bytes(original[: len(original) // 2])

        restorer = PurePythonRestorer(repo, pw)
        with pytest.raises((IntegrityError, Exception)):
            restorer.restore(target=tmp_path / "out")

    def test_truncated_missing_mac(self, tmp_path):
        """Removing the 16-byte MAC from a pack blob should fail."""
        repo, pw, pack_id = _build_repo(tmp_path)
        pack_path = repo / "data" / pack_id[:2] / pack_id
        original = pack_path.read_bytes()
        # Chop off the last 16 bytes (MAC of the last blob)
        pack_path.write_bytes(original[:-16])

        restorer = PurePythonRestorer(repo, pw)
        with pytest.raises((IntegrityError, Exception)):
            restorer.restore(target=tmp_path / "out")


# ═════════════════════════════════════════════════════════════════
# Corrupt pack contents (bit-flip)
# ═════════════════════════════════════════════════════════════════


class TestCorruptPackContents:
    """Verify restorer detects bit-flipped pack data."""

    def test_single_bit_flip_in_ciphertext(self, tmp_path):
        """Flipping one bit in the ciphertext should fail MAC check."""
        repo, pw, pack_id = _build_repo(tmp_path)
        pack_path = repo / "data" / pack_id[:2] / pack_id
        data = bytearray(pack_path.read_bytes())
        # Flip a bit in the middle of the ciphertext area
        # (after the first 16 bytes of IV)
        flip_pos = 20
        data[flip_pos] ^= 0x01
        pack_path.write_bytes(bytes(data))

        restorer = PurePythonRestorer(repo, pw)
        with pytest.raises((IntegrityError, Exception)):
            restorer.restore(target=tmp_path / "out")

    def test_bit_flip_in_mac(self, tmp_path):
        """Flipping a bit in the MAC tag should fail verification."""
        repo, pw, pack_id = _build_repo(tmp_path)
        pack_path = repo / "data" / pack_id[:2] / pack_id
        data = bytearray(pack_path.read_bytes())
        # The first blob's MAC is at offset blob_length - 16
        # Just flip a bit near the end (within the last blob's MAC)
        data[-1] ^= 0x80
        pack_path.write_bytes(bytes(data))

        restorer = PurePythonRestorer(repo, pw)
        with pytest.raises((IntegrityError, Exception)):
            restorer.restore(target=tmp_path / "out")

    def test_swapped_iv_bytes(self, tmp_path):
        """Swapping two bytes in an IV should fail decryption."""
        repo, pw, pack_id = _build_repo(tmp_path)
        pack_path = repo / "data" / pack_id[:2] / pack_id
        data = bytearray(pack_path.read_bytes())
        # Swap first two IV bytes
        data[0], data[1] = data[1], data[0]
        pack_path.write_bytes(bytes(data))

        restorer = PurePythonRestorer(repo, pw)
        with pytest.raises((IntegrityError, Exception)):
            restorer.restore(target=tmp_path / "out")


# ═════════════════════════════════════════════════════════════════
# Missing pack files
# ═════════════════════════════════════════════════════════════════


class TestMissingPacks:
    """Verify restorer detects missing pack files."""

    def test_pack_file_deleted(self, tmp_path):
        """Deleting the sole pack file should raise FileNotFoundError.

        ``interactive=False`` opts out of the #234 disc-swap prompt loop
        -- this test pins the raise-on-miss contract for non-tty callers.
        """
        repo, pw, pack_id = _build_repo(tmp_path)
        pack_path = repo / "data" / pack_id[:2] / pack_id
        pack_path.unlink()

        restorer = PurePythonRestorer(repo, pw, interactive=False)
        with pytest.raises(FileNotFoundError, match="Pack file not found"):
            restorer.restore(target=tmp_path / "out")

    def test_entire_data_dir_missing(self, tmp_path):
        """Removing data/ entirely should raise FileNotFoundError."""
        import shutil
        repo, pw, pack_id = _build_repo(tmp_path)
        shutil.rmtree(repo / "data")
        (repo / "data").mkdir()  # recreate empty

        restorer = PurePythonRestorer(repo, pw, interactive=False)
        with pytest.raises(FileNotFoundError, match="Pack file not found"):
            restorer.restore(target=tmp_path / "out")


# ═════════════════════════════════════════════════════════════════
# Missing index entries
# ═════════════════════════════════════════════════════════════════


class TestMissingIndexEntries:
    """Verify restorer detects references to blobs not in any index."""

    def test_empty_index(self, tmp_path):
        """An index with no packs should fail when looking up a blob."""
        repo, pw, pack_id = _build_repo(tmp_path)

        # Replace index file with an empty packs list
        index_dir = repo / "index"
        for f in index_dir.iterdir():
            empty_idx = json.dumps({"packs": []}).encode()
            f.write_bytes(_encrypt_with_master(empty_idx))

        restorer = PurePythonRestorer(repo, pw)
        with pytest.raises(KeyError, match="Blob not found"):
            restorer.restore(target=tmp_path / "out")

    def test_index_references_nonexistent_pack(self, tmp_path):
        """Index pointing to a pack ID that doesn't exist as a file."""
        repo, pw, pack_id = _build_repo(tmp_path)

        # Read the existing index, change the pack ID to a bogus one
        index_dir = repo / "index"
        for f in index_dir.iterdir():
            encrypted = f.read_bytes()
            plaintext = _decrypt_authenticated(
                MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, encrypted,
            )
            idx_doc = json.loads(plaintext)
            # Change pack ID to something nonexistent
            for pack in idx_doc["packs"]:
                pack["id"] = "f" * 64
            new_idx = json.dumps(idx_doc).encode()
            f.write_bytes(_encrypt_with_master(new_idx))

        restorer = PurePythonRestorer(repo, pw, interactive=False)
        with pytest.raises(FileNotFoundError, match="Pack file not found"):
            restorer.restore(target=tmp_path / "out")

    def test_corrupted_index_file(self, tmp_path):
        """An index file with garbled ciphertext should fail decryption."""
        repo, pw, pack_id = _build_repo(tmp_path)
        index_dir = repo / "index"
        for f in index_dir.iterdir():
            f.write_bytes(os.urandom(200))

        restorer = PurePythonRestorer(repo, pw)
        with pytest.raises((IntegrityError, Exception)):
            restorer.restore(target=tmp_path / "out")


# ═════════════════════════════════════════════════════════════════
# Empty / malformed repository structures
# ═════════════════════════════════════════════════════════════════


class TestMalformedRepo:
    """Verify restorer rejects broken repository structures."""

    def test_missing_keys_dir(self, tmp_path):
        """No keys/ directory should fail initialization."""
        repo, pw, _ = _build_repo(tmp_path)
        import shutil
        shutil.rmtree(repo / "keys")

        with pytest.raises((FileNotFoundError, Exception)):
            restorer = PurePythonRestorer(repo, pw)
            restorer.restore(target=tmp_path / "out")

    def test_missing_snapshots_dir(self, tmp_path):
        """No snapshots/ directory should fail to find anything to restore."""
        import shutil
        repo, pw, _ = _build_repo(tmp_path)
        shutil.rmtree(repo / "snapshots")
        (repo / "snapshots").mkdir()  # empty

        restorer = PurePythonRestorer(repo, pw)
        with pytest.raises(Exception):  # noqa: B017
            restorer.restore(target=tmp_path / "out")

    def test_missing_config_is_ok(self, tmp_path):
        """Missing config file should NOT prevent restore."""
        repo, pw, _ = _build_repo(tmp_path)
        (repo / "config").unlink()

        restorer = PurePythonRestorer(repo, pw)
        target = tmp_path / "output"
        restorer.restore(target=target)
        assert (target / "hello.txt").read_bytes() == b"Hello from chaos tests!\n"

    def test_wrong_password_detected(self, tmp_path):
        """Using the wrong password must not silently produce bad output."""
        repo, _, _ = _build_repo(tmp_path)
        bad_pw = tmp_path / "bad.txt"
        bad_pw.write_bytes(b"wrong-password")

        restorer = PurePythonRestorer(repo, bad_pw)
        with pytest.raises((IntegrityError, Exception)):
            restorer.restore(target=tmp_path / "out")


# ═════════════════════════════════════════════════════════════════
# Executor SHA-256 verification under corruption
# ═════════════════════════════════════════════════════════════════


class TestExecutorCorruptionHandling:
    """Tests for RestoreExecutor's pack verification during ingest."""

    def test_sha_mismatch_raises(self, tmp_path):
        """A pack whose content doesn't match its filename SHA-256."""
        from unittest.mock import MagicMock
        executor = RestoreExecutor(MagicMock())

        mount = tmp_path / "vol" / "data"
        mount.mkdir(parents=True)
        # Write content that won't match the SHA-256 filename
        fake_sha = "abcd" * 16  # 64 chars
        (mount / fake_sha).write_bytes(b"this content doesn't match")

        cache = tmp_path / "cache"
        cache.mkdir()

        with pytest.raises(PackCorruptionError):
            executor.ingest_volume(
                cache, tmp_path / "vol", [fake_sha],
                verify=True, collect_failures=False,
            )

    def test_sha_mismatch_collected(self, tmp_path):
        """collect_failures=True collects corruption, doesn't raise."""
        from unittest.mock import MagicMock
        executor = RestoreExecutor(MagicMock())

        mount = tmp_path / "vol" / "data"
        mount.mkdir(parents=True)
        fake_sha = "abcd" * 16
        (mount / fake_sha).write_bytes(b"mismatched content")

        cache = tmp_path / "cache"
        cache.mkdir()

        result = executor.ingest_volume(
            cache, tmp_path / "vol", [fake_sha],
            verify=True, collect_failures=True,
        )
        assert result.ingested == 0
        assert fake_sha in result.failed

    def test_good_pack_passes_verification(self, tmp_path):
        """A pack whose content matches its SHA-256 filename."""
        from unittest.mock import MagicMock
        executor = RestoreExecutor(MagicMock())

        mount = tmp_path / "vol" / "data"
        mount.mkdir(parents=True)
        content = b"valid pack content"
        real_sha = hashlib.sha256(content).hexdigest()
        (mount / real_sha).write_bytes(content)

        cache = tmp_path / "cache"
        cache.mkdir()

        result = executor.ingest_volume(
            cache, tmp_path / "vol", [real_sha],
            verify=True, collect_failures=False,
        )
        assert result.ingested == 1


# ═════════════════════════════════════════════════════════════════
# verify_cache_completeness edge cases
# ═════════════════════════════════════════════════════════════════


class TestCacheCompletenessEdgeCases:
    """Edge cases for the post-ingest completeness check."""

    def test_detects_partial_ingest_after_skip(self, tmp_path):
        """Simulates user skipping a volume — missing packs detected."""
        cache = tmp_path / "cache"
        data = cache / "data"
        present = hashlib.sha256(b"pack1").hexdigest()
        skipped = hashlib.sha256(b"pack2").hexdigest()

        d = data / present[:2]
        d.mkdir(parents=True)
        (d / present).write_bytes(b"pack1")

        missing = RestoreExecutor.verify_cache_completeness(
            cache, [present, skipped],
        )
        assert missing == [skipped]

    def test_large_number_of_packs(self, tmp_path):
        """Handles 1000 packs efficiently."""
        cache = tmp_path / "cache"
        data = cache / "data"

        sha_list = []
        for i in range(1000):
            sha = hashlib.sha256(f"pack{i}".encode()).hexdigest()
            sha_list.append(sha)
            d = data / sha[:2]
            d.mkdir(parents=True, exist_ok=True)
            (d / sha).write_bytes(f"pack{i}".encode())

        missing = RestoreExecutor.verify_cache_completeness(cache, sha_list)
        assert missing == []

    def test_mixed_present_and_missing(self, tmp_path):
        """Half present, half missing — returns correct missing set."""
        cache = tmp_path / "cache"
        data = cache / "data"

        present_shas = []
        missing_shas = []
        for i in range(20):
            sha = hashlib.sha256(f"pack{i}".encode()).hexdigest()
            if i % 2 == 0:
                d = data / sha[:2]
                d.mkdir(parents=True, exist_ok=True)
                (d / sha).write_bytes(f"pack{i}".encode())
                present_shas.append(sha)
            else:
                missing_shas.append(sha)

        all_shas = present_shas + missing_shas
        result = RestoreExecutor.verify_cache_completeness(cache, all_shas)
        assert set(result) == set(missing_shas)


# ═════════════════════════════════════════════════════════════════
# End-to-end: corrupt restorer still validates
# ═════════════════════════════════════════════════════════════════


class TestEndToEndIntegrity:
    """Prove that a successful restore means data is intact."""

    def test_good_repo_restores_correctly(self, tmp_path):
        """Baseline: an uncorrupted repo restores cleanly."""
        repo, pw, _ = _build_repo(tmp_path)
        target = tmp_path / "output"

        restorer = PurePythonRestorer(repo, pw)
        restorer.restore(target=target)

        assert (target / "hello.txt").read_bytes() == b"Hello from chaos tests!\n"

    def test_double_restore_is_idempotent(self, tmp_path):
        """Restoring twice to the same target doesn't corrupt files."""
        repo, pw, _ = _build_repo(tmp_path)
        target = tmp_path / "output"

        restorer = PurePythonRestorer(repo, pw)
        restorer.restore(target=target)
        restorer.restore(target=target)

        assert (target / "hello.txt").read_bytes() == b"Hello from chaos tests!\n"

    def test_corrupt_snapshot_detected(self, tmp_path):
        """A corrupted snapshot file should fail decryption or parsing."""
        repo, pw, _ = _build_repo(tmp_path)
        snap_dir = repo / "snapshots"
        for f in snap_dir.iterdir():
            data = bytearray(f.read_bytes())
            # Corrupt the ciphertext portion
            data[20] ^= 0xFF
            f.write_bytes(bytes(data))

        restorer = PurePythonRestorer(repo, pw)
        with pytest.raises((IntegrityError, Exception)):
            restorer.restore(target=tmp_path / "out")
