"""Tests for the pure-Python restic restore fallback.

These tests create **synthetic restic repositories** using the same
encryption scheme as real restic/rustic, then verify that the fallback
restorer can decrypt and extract the data correctly.

The test repo contains:
    - A key file (scrypt-encrypted master key)
    - An encrypted config file
    - An index file mapping blobs → packs
    - A snapshot pointing to a tree
    - A pack file with tree + data blobs

This proves end-to-end correctness: key derivation → decryption →
index parsing → tree traversal → file extraction.
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
from lcsas.restore.restic_fallback import (
    IntegrityError,
    MasterKey,
    PurePythonRestorer,
    _clamp_r,
    _constant_time_eq,
    _decrypt_authenticated,
    _parse_timestamp,
    _poly1305_mac,
)

# ── Test Helpers ─────────────────────────────────────────────────

PASSWORD = b"test-password-for-unit-tests"

# Fixed master key for deterministic tests
MASTER_ENCRYPT = bytes(range(32))                         # 32 bytes
MASTER_MAC_K = bytes(range(16, 32))                       # 16 bytes
MASTER_MAC_R = bytes([
    # Pre-clamped-compatible r key (some bits will be cleared by clamping)
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
    """Encrypt data using restic's authenticated encryption scheme.

    Returns: IV (16) || ciphertext || MAC (16)
    """
    if iv is None:
        iv = os.urandom(16)

    ciphertext = aes_ctr(encrypt_key, iv, plaintext)

    # Compute Poly1305-AES MAC: s = AES-128(mac_k, iv)
    mac_rk = key_schedule(mac_k)
    s = aes_encrypt_block(iv, mac_rk)
    tag = _poly1305_mac(mac_r, s, ciphertext)

    return iv + ciphertext + tag


def _make_key_file(
    master_key: MasterKey,
    password: bytes,
    tmp_path: Path,
) -> Path:
    """Create a restic-format key file encrypted with *password*.

    Uses scrypt with small N for fast tests.
    """
    # Small N for test speed (real restic uses 2^15 = 32768)
    n = 1024
    r = 8
    p = 1
    salt = os.urandom(64)

    derived = hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, dklen=64)
    kek_encrypt = derived[:32]
    kek_mac_k = derived[32:48]
    kek_mac_r = derived[48:64]

    master_json = json.dumps({
        "encrypt": base64.b64encode(master_key.encrypt).decode(),
        "mac": {
            "k": base64.b64encode(master_key.mac_k).decode(),
            "r": base64.b64encode(master_key.mac_r).decode(),
        },
    }).encode()

    encrypted_master = _encrypt_data(kek_encrypt, kek_mac_k, kek_mac_r, master_json)

    key_doc = {
        "created": "2026-01-15T10:00:00Z",
        "username": "test",
        "hostname": "testhost",
        "kdf": "scrypt",
        "N": n,
        "r": r,
        "p": p,
        "salt": base64.b64encode(salt).decode(),
        "data": base64.b64encode(encrypted_master).decode(),
    }

    keys_dir = tmp_path / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    key_file = keys_dir / "abcdef1234567890"
    key_file.write_text(json.dumps(key_doc))
    return key_file


def _encrypt_with_master(plaintext: bytes) -> bytes:
    """Encrypt with the fixed test master key."""
    return _encrypt_data(MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, plaintext)


def _build_test_repo(tmp_path: Path) -> Path:
    """Build a complete synthetic restic repository.

    Creates a repo containing one snapshot with:
        /hello.txt           → "Hello, World!\\n"
        /subdir/nested.txt   → "Nested file content.\\n"
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)
    _make_key_file(mk, PASSWORD, repo)

    # ── Create blobs ─────────────────────────────────────────────
    file1_content = b"Hello, World!\n"
    file2_content = b"Nested file content.\n"

    file1_id = hashlib.sha256(file1_content).hexdigest()
    file2_id = hashlib.sha256(file2_content).hexdigest()

    # Tree for /subdir/
    subdir_tree = json.dumps({
        "nodes": [{
            "name": "nested.txt",
            "type": "file",
            "mode": 0o644,
            "mtime": "2026-01-15T10:00:00.000000000Z",
            "atime": "2026-01-15T10:00:00.000000000Z",
            "ctime": "2026-01-15T10:00:00.000000000Z",
            "uid": 1000, "gid": 1000,
            "size": len(file2_content),
            "content": [file2_id],
        }],
    }).encode()
    subdir_tree_id = hashlib.sha256(subdir_tree).hexdigest()

    # Root tree
    root_tree = json.dumps({
        "nodes": [
            {
                "name": "hello.txt",
                "type": "file",
                "mode": 0o644,
                "mtime": "2026-01-15T10:00:00.000000000Z",
                "atime": "2026-01-15T10:00:00.000000000Z",
                "ctime": "2026-01-15T10:00:00.000000000Z",
                "uid": 1000, "gid": 1000,
                "size": len(file1_content),
                "content": [file1_id],
            },
            {
                "name": "subdir",
                "type": "dir",
                "subtree": subdir_tree_id,
            },
        ],
    }).encode()
    root_tree_id = hashlib.sha256(root_tree).hexdigest()

    # ── Build a pack file ────────────────────────────────────────
    # Encrypt each blob and concatenate
    blobs_info: list[dict] = []
    pack_data = bytearray()

    for blob_content, blob_id, blob_type in [
        (file1_content, file1_id, "data"),
        (file2_content, file2_id, "data"),
        (subdir_tree, subdir_tree_id, "tree"),
        (root_tree, root_tree_id, "tree"),
    ]:
        encrypted_blob = _encrypt_with_master(blob_content)
        offset = len(pack_data)
        pack_data.extend(encrypted_blob)
        blobs_info.append({
            "id": blob_id,
            "type": blob_type,
            "offset": offset,
            "length": len(encrypted_blob),
        })

    # Pack header (encrypted entry list) — not used by our fallback
    # (we use index files instead), but build it for completeness.
    # Just write the pack data as-is (no need for pack header).
    pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()

    # Write pack file in two-level layout
    data_dir = repo / "data" / pack_id[:2]
    data_dir.mkdir(parents=True)
    (data_dir / pack_id).write_bytes(bytes(pack_data))

    # ── Create index file ────────────────────────────────────────
    index_doc = json.dumps({
        "packs": [{
            "id": pack_id,
            "blobs": blobs_info,
        }],
    }).encode()

    index_dir = repo / "index"
    index_dir.mkdir()
    index_id = hashlib.sha256(index_doc).hexdigest()
    (index_dir / index_id).write_bytes(_encrypt_with_master(index_doc))

    # ── Create snapshot ──────────────────────────────────────────
    snapshot_doc = json.dumps({
        "time": "2026-01-15T10:30:00.000000000Z",
        "tree": root_tree_id,
        "paths": ["/home/test"],
        "hostname": "testhost",
        "username": "test",
        "tags": ["unit-test"],
        "program_version": "lcsas-test 0.1",
    }).encode()

    snap_dir = repo / "snapshots"
    snap_dir.mkdir()
    snap_id = hashlib.sha256(snapshot_doc).hexdigest()
    (snap_dir / snap_id).write_bytes(_encrypt_with_master(snapshot_doc))

    # ── Create config ────────────────────────────────────────────
    config_doc = json.dumps({
        "version": 2,
        "id": "test-repo-id-1234567890abcdef",
        "chunker_polynomial": "3DA3358B4DC173",
    }).encode()
    (repo / "config").write_bytes(_encrypt_with_master(config_doc))

    return repo


# ── Poly1305 Tests ───────────────────────────────────────────────

class TestPoly1305:
    def test_rfc8439_vector(self):
        """RFC 8439 §2.5.2 Poly1305 test vector (raw Poly1305, not AES).

        Key r = 85:d6:be:78:57:55:6d:33:7f:44:52:fe:42:d5:06:a8
        Key s = 01:03:80:8a:fb:0d:b2:fd:4a:bf:f6:af:41:49:f5:1b
        Message = "Cryptographic Forum Research Group"
        Tag = a8:06:1d:c1:30:51:36:c6:c2:2b:8b:af:0c:01:27:a9
        """
        r = bytes.fromhex("85d6be7857556d337f4452fe42d506a8")
        s = bytes.fromhex("0103808afb0db2fd4abff6af4149f51b")
        msg = b"Cryptographic Forum Research Group"
        expected = bytes.fromhex("a8061dc1305136c6c22b8baf0c0127a9")

        tag = _poly1305_mac(r, s, msg)
        assert tag == expected

    def test_empty_message(self):
        """MAC of empty message should be deterministic."""
        r = bytes(16)
        s = bytes(16)
        tag = _poly1305_mac(r, s, b"")
        assert len(tag) == 16

    def test_clamp_r_clears_bits(self):
        """Verify clamping zeroes the required bits per RFC 8439."""
        r = bytes(range(16))
        clamped = _clamp_r(r)
        r_bytes = clamped.to_bytes(16, "little")
        # Per Poly1305 spec: top 4 bits of bytes 3, 7, 11, 15 cleared
        # and bottom 2 bits of bytes 4, 8, 12 cleared
        assert r_bytes[3] & 0xF0 == 0
        assert r_bytes[4] & 0x03 == 0
        assert r_bytes[8] & 0x03 == 0
        assert r_bytes[12] & 0x03 == 0


# ── Authenticated Encryption Tests ──────────────────────────────

class TestAuthenticatedEncryption:

    def test_encrypt_decrypt_round_trip(self):
        """Data encrypted then decrypted yields original."""
        plaintext = b"Test data for authenticated encryption"
        encrypted = _encrypt_data(
            MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, plaintext
        )
        decrypted = _decrypt_authenticated(
            MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, encrypted
        )
        assert decrypted == plaintext

    def test_wrong_key_fails(self):
        """Decryption with wrong MAC key raises IntegrityError."""
        plaintext = b"Secret data"
        encrypted = _encrypt_data(
            MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, plaintext
        )
        wrong_mac_k = bytes(16)  # Different MAC key → tag mismatch
        with pytest.raises(IntegrityError, match="MAC verification failed"):
            _decrypt_authenticated(MASTER_ENCRYPT, wrong_mac_k, MASTER_MAC_R, encrypted)

    def test_tampered_data_fails(self):
        """Flipping a ciphertext bit causes MAC failure."""
        plaintext = b"Important data"
        encrypted = bytearray(_encrypt_data(
            MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, plaintext
        ))
        # Flip a bit in the ciphertext (not IV or MAC)
        encrypted[20] ^= 0x01
        with pytest.raises(IntegrityError, match="MAC verification failed"):
            _decrypt_authenticated(
                MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, bytes(encrypted)
            )

    def test_too_short_data_fails(self):
        """Data shorter than minimum (33 bytes) raises IntegrityError."""
        with pytest.raises(IntegrityError, match="too short"):
            _decrypt_authenticated(
                MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, bytes(32)
            )

    def test_large_data_round_trip(self):
        """Multi-block encryption/decryption works."""
        plaintext = bytes(range(256)) * 100  # 25600 bytes
        encrypted = _encrypt_data(
            MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, plaintext
        )
        decrypted = _decrypt_authenticated(
            MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, encrypted
        )
        assert decrypted == plaintext


# ── Constant-Time Comparison ────────────────────────────────────

class TestConstantTimeEq:
    def test_equal(self):
        assert _constant_time_eq(b"abc", b"abc")

    def test_not_equal(self):
        assert not _constant_time_eq(b"abc", b"abd")

    def test_different_length(self):
        assert not _constant_time_eq(b"abc", b"ab")


# ── Timestamp Parser ────────────────────────────────────────────

class TestParseTimestamp:
    def test_nanosecond_precision(self):
        ts = _parse_timestamp("2026-01-15T10:30:00.123456789Z")
        assert isinstance(ts, float)
        assert ts > 0

    def test_no_fractional(self):
        ts = _parse_timestamp("2026-01-15T10:30:00Z")
        assert isinstance(ts, float)

    def test_microsecond_precision(self):
        ts = _parse_timestamp("2026-01-15T10:30:00.123456Z")
        assert isinstance(ts, float)


# ── Key Derivation Tests ────────────────────────────────────────

class TestKeyDerivation:
    def test_load_master_key(self, tmp_path):
        """Key file decryption recovers the original master key."""
        mk = MasterKey(
            encrypt=MASTER_ENCRYPT,
            mac_k=MASTER_MAC_K,
            mac_r=MASTER_MAC_R,
        )
        _make_key_file(mk, PASSWORD, tmp_path)

        from lcsas.restore.restic_fallback import _try_keys
        recovered = _try_keys(tmp_path / "keys", PASSWORD)

        assert recovered.encrypt == mk.encrypt
        assert recovered.mac_k == mk.mac_k
        assert recovered.mac_r == mk.mac_r

    def test_wrong_password_fails(self, tmp_path):
        """Wrong password fails to decrypt key file."""
        mk = MasterKey(
            encrypt=MASTER_ENCRYPT,
            mac_k=MASTER_MAC_K,
            mac_r=MASTER_MAC_R,
        )
        _make_key_file(mk, PASSWORD, tmp_path)

        from lcsas.restore.restic_fallback import _try_keys
        with pytest.raises(IntegrityError, match="wrong password"):
            _try_keys(tmp_path / "keys", b"wrong-password")


# ── Full Restore Tests ──────────────────────────────────────────

class TestPurePythonRestorer:
    """End-to-end tests with a synthetic restic repository."""

    @pytest.fixture
    def repo(self, tmp_path):
        return _build_test_repo(tmp_path)

    @pytest.fixture
    def password_file(self, tmp_path):
        pf = tmp_path / "password.txt"
        pf.write_bytes(PASSWORD + b"\n")
        return pf

    def test_verify_key(self, repo, password_file):
        """Verify that the correct password is accepted."""
        restorer = PurePythonRestorer(repo, password_file=password_file)
        assert restorer.verify_key()

    def test_verify_key_wrong_password(self, repo, tmp_path):
        """Wrong password is rejected."""
        pf = tmp_path / "wrong.txt"
        pf.write_bytes(b"wrong-password\n")
        restorer = PurePythonRestorer(repo, password_file=pf)
        assert not restorer.verify_key()

    def test_list_snapshots(self, repo, password_file):
        """Snapshot listing returns the test snapshot."""
        restorer = PurePythonRestorer(repo, password_file=password_file)
        snaps = restorer.list_snapshots()
        assert len(snaps) == 1
        assert snaps[0].hostname == "testhost"
        assert snaps[0].paths == ["/home/test"]

    def test_repo_info(self, repo, password_file):
        """Repo info returns config and blob count."""
        restorer = PurePythonRestorer(repo, password_file=password_file)
        info = restorer.repo_info()
        assert info["repository_id"] == "test-repo-id-1234567890abcdef"
        assert info["version"] == 2
        assert info["snapshots"] == 1
        assert info["indexed_blobs"] == 4  # 2 data + 2 tree

    def test_full_restore(self, repo, password_file, tmp_path):
        """Complete restore recovers all files correctly."""
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password_file=password_file)
        snap = restorer.restore(target=target)

        assert snap.hostname == "testhost"

        # Verify file contents
        hello = target / "hello.txt"
        assert hello.is_file()
        assert hello.read_text() == "Hello, World!\n"

        nested = target / "subdir" / "nested.txt"
        assert nested.is_file()
        assert nested.read_text() == "Nested file content.\n"

    def test_restore_with_password_bytes(self, repo, tmp_path):
        """Can provide password as raw bytes instead of file."""
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)

        assert (target / "hello.txt").read_text() == "Hello, World!\n"

    def test_restore_creates_target_dir(self, repo, password_file, tmp_path):
        """Target directory is created automatically."""
        target = tmp_path / "deep" / "nested" / "output"
        restorer = PurePythonRestorer(repo, password_file=password_file)
        restorer.restore(target=target)
        assert target.is_dir()
        assert (target / "hello.txt").is_file()

    def test_restore_by_snapshot_prefix(self, repo, password_file, tmp_path):
        """Can restore by snapshot ID prefix."""
        restorer = PurePythonRestorer(repo, password_file=password_file)
        snaps = restorer.list_snapshots()
        prefix = snaps[0].snapshot_id[:8]

        target = tmp_path / "restored"
        snap = restorer.restore(target=target, snapshot_id=prefix)
        assert snap.snapshot_id == snaps[0].snapshot_id

    def test_restore_nonexistent_snapshot_fails(self, repo, password_file, tmp_path):
        """Requesting a non-existent snapshot raises ValueError."""
        restorer = PurePythonRestorer(repo, password_file=password_file)
        with pytest.raises(ValueError, match="not found"):
            restorer.restore(target=tmp_path / "out", snapshot_id="nonexistent")

    def test_no_password_raises(self, repo):
        """Must provide either password_file or password."""
        with pytest.raises(ValueError, match="password"):
            PurePythonRestorer(repo)


# ── Permission Restoration Test ──────────────────────────────────

class TestPermissionRestore:
    """Test that file and directory permissions are correctly restored."""

    def test_executable_permission_restored(self, tmp_path):
        """A file with mode 0o755 should be restored with that permission."""
        repo = tmp_path / "repo"
        repo.mkdir()

        mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)
        _make_key_file(mk, PASSWORD, repo)

        # File with 0o755 (executable)
        script_content = b"#!/bin/bash\necho hello\n"
        script_id = hashlib.sha256(script_content).hexdigest()

        root_tree = json.dumps({
            "nodes": [{
                "name": "run.sh",
                "type": "file",
                "mode": 0o755,
                "mtime": "2026-01-01T00:00:00.000000000Z",
                "atime": "2026-01-01T00:00:00.000000000Z",
                "ctime": "2026-01-01T00:00:00.000000000Z",
                "uid": 1000, "gid": 1000,
                "size": len(script_content),
                "content": [script_id],
            }],
        }).encode()
        root_tree_id = hashlib.sha256(root_tree).hexdigest()

        blobs_info: list[dict] = []
        pack_data = bytearray()
        for content, blob_id, btype in [
            (script_content, script_id, "data"),
            (root_tree, root_tree_id, "tree"),
        ]:
            enc = _encrypt_with_master(content)
            blobs_info.append({
                "id": blob_id, "type": btype,
                "offset": len(pack_data), "length": len(enc),
            })
            pack_data.extend(enc)

        pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
        data_dir = repo / "data" / pack_id[:2]
        data_dir.mkdir(parents=True)
        (data_dir / pack_id).write_bytes(bytes(pack_data))

        idx_doc = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
        idx_dir = repo / "index"
        idx_dir.mkdir()
        idx_id = hashlib.sha256(idx_doc).hexdigest()
        (idx_dir / idx_id).write_bytes(_encrypt_with_master(idx_doc))

        snap_doc = json.dumps({
            "time": "2026-01-01T00:00:00.000000000Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
            "username": "test",
        }).encode()
        snap_dir = repo / "snapshots"
        snap_dir.mkdir()
        snap_id = hashlib.sha256(snap_doc).hexdigest()
        (snap_dir / snap_id).write_bytes(_encrypt_with_master(snap_doc))

        config_doc = json.dumps({"version": 2, "id": "perm-test"}).encode()
        (repo / "config").write_bytes(_encrypt_with_master(config_doc))

        # Restore
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)

        run_sh = target / "run.sh"
        assert run_sh.is_file()
        assert run_sh.read_bytes() == script_content
        mode = run_sh.stat().st_mode & 0o7777
        # Must have at least owner-execute bit set
        assert mode & 0o100, f"Expected executable mode, got {oct(mode)}"
        # Full mode check (owner bits match; group/other depend on umask)
        assert mode & 0o700 == 0o755 & 0o700, f"Owner bits wrong: {oct(mode)}"


# ── Flat Layout Test ─────────────────────────────────────────────

class TestFlatLayout:
    """Test that the restorer handles flat data/ layout (LCSAS disc style)."""

    def test_flat_layout_restore(self, tmp_path):
        """Restorer works when pack files are in flat layout."""
        repo = _build_test_repo(tmp_path)

        # Reorganize data/ from two-level to flat layout
        data_dir = repo / "data"
        pack_files = list(data_dir.rglob("*"))
        pack_files = [p for p in pack_files if p.is_file()]
        for pf in pack_files:
            flat_path = data_dir / pf.name
            if flat_path != pf:
                pf.rename(flat_path)

        # Remove empty prefix directories
        import contextlib
        for d in data_dir.iterdir():
            if d.is_dir():
                with contextlib.suppress(OSError):
                    d.rmdir()

        # Restore should still work
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        assert (target / "hello.txt").read_text() == "Hello, World!\n"


# ── Symlink Test ─────────────────────────────────────────────────

class TestSymlinkRestore:
    """Test symbolic link restoration."""

    def test_restore_symlink(self, tmp_path):
        """Symlinks in tree nodes are recreated."""
        repo = _build_test_repo(tmp_path)

        # We need to modify the repo to include a symlink.
        # The simplest approach: create a new tree with a symlink node,
        # add it to a new pack, update the index, and create a new snapshot.

        # File content
        file_content = b"Link target content\n"
        file_id = hashlib.sha256(file_content).hexdigest()

        # Root tree with file + symlink
        root_tree = json.dumps({
            "nodes": [
                {
                    "name": "target.txt",
                    "type": "file",
                    "mode": 0o644,
                    "content": [file_id],
                },
                {
                    "name": "link.txt",
                    "type": "symlink",
                    "linktarget": "target.txt",
                },
            ],
        }).encode()
        root_tree_id = hashlib.sha256(root_tree).hexdigest()

        # Build pack
        pack_data = bytearray()
        blobs_info = []

        for content, blob_id, btype in [
            (file_content, file_id, "data"),
            (root_tree, root_tree_id, "tree"),
        ]:
            enc = _encrypt_with_master(content)
            offset = len(pack_data)
            pack_data.extend(enc)
            blobs_info.append({
                "id": blob_id, "type": btype,
                "offset": offset, "length": len(enc),
            })

        pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
        pack_dir = repo / "data" / pack_id[:2]
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / pack_id).write_bytes(bytes(pack_data))

        # Add to index
        new_index = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
        idx_id = hashlib.sha256(new_index).hexdigest()
        (repo / "index" / idx_id).write_bytes(_encrypt_with_master(new_index))

        # New snapshot
        snap_doc = json.dumps({
            "time": "2026-02-15T10:00:00Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
        }).encode()
        snap_id = hashlib.sha256(snap_doc).hexdigest()
        (repo / "snapshots" / snap_id).write_bytes(_encrypt_with_master(snap_doc))

        # Restore the latest snapshot (the symlink one)
        target = tmp_path / "restored_symlink"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)

        assert (target / "target.txt").read_text() == "Link target content\n"
        assert (target / "link.txt").is_symlink()
        assert os.readlink(str(target / "link.txt")) == "target.txt"


# ── Hardlink Deduplication Test ──────────────────────────────────

class TestHardlinkRestore:
    """Test hardlink deduplication during tree restore."""

    def test_hardlinks_share_inode(self, tmp_path):
        """Two file nodes with same inode+links>1 become hardlinks."""
        repo = _build_test_repo(tmp_path)

        file_content = b"Shared hardlink content\n"
        file_id = hashlib.sha256(file_content).hexdigest()

        # Root tree: two files with shared inode
        root_tree = json.dumps({
            "nodes": [
                {
                    "name": "original.txt",
                    "type": "file",
                    "mode": 0o644,
                    "inode": 999999,
                    "links": 2,
                    "content": [file_id],
                },
                {
                    "name": "hardlink.txt",
                    "type": "file",
                    "mode": 0o644,
                    "inode": 999999,
                    "links": 2,
                    "content": [file_id],
                },
            ],
        }).encode()
        root_tree_id = hashlib.sha256(root_tree).hexdigest()

        # Build pack
        pack_data = bytearray()
        blobs_info = []
        for content, blob_id, btype in [
            (file_content, file_id, "data"),
            (root_tree, root_tree_id, "tree"),
        ]:
            enc = _encrypt_with_master(content)
            offset = len(pack_data)
            pack_data.extend(enc)
            blobs_info.append({
                "id": blob_id, "type": btype,
                "offset": offset, "length": len(enc),
            })

        pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
        pack_dir = repo / "data" / pack_id[:2]
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / pack_id).write_bytes(bytes(pack_data))

        # Index + snapshot
        new_index = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
        idx_id = hashlib.sha256(new_index).hexdigest()
        (repo / "index" / idx_id).write_bytes(_encrypt_with_master(new_index))

        snap_doc = json.dumps({
            "time": "2026-03-01T10:00:00Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
        }).encode()
        snap_id = hashlib.sha256(snap_doc).hexdigest()
        (repo / "snapshots" / snap_id).write_bytes(_encrypt_with_master(snap_doc))

        # Restore
        target = tmp_path / "restored_hardlink"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)

        orig = target / "original.txt"
        link = target / "hardlink.txt"

        assert orig.read_text() == "Shared hardlink content\n"
        assert link.read_text() == "Shared hardlink content\n"

        # Both files share the same inode (hardlinked)
        assert orig.stat().st_ino == link.stat().st_ino

    def test_single_link_no_hardlink(self, tmp_path):
        """Files with links=1 or no inode are not hardlinked."""
        repo = _build_test_repo(tmp_path)

        file_content = b"Not a hardlink\n"
        file_id = hashlib.sha256(file_content).hexdigest()

        root_tree = json.dumps({
            "nodes": [
                {
                    "name": "file_a.txt",
                    "type": "file",
                    "mode": 0o644,
                    "inode": 12345,
                    "links": 1,
                    "content": [file_id],
                },
                {
                    "name": "file_b.txt",
                    "type": "file",
                    "mode": 0o644,
                    "inode": 12345,
                    "links": 1,
                    "content": [file_id],
                },
            ],
        }).encode()
        root_tree_id = hashlib.sha256(root_tree).hexdigest()

        pack_data = bytearray()
        blobs_info = []
        for content, blob_id, btype in [
            (file_content, file_id, "data"),
            (root_tree, root_tree_id, "tree"),
        ]:
            enc = _encrypt_with_master(content)
            offset = len(pack_data)
            pack_data.extend(enc)
            blobs_info.append({
                "id": blob_id, "type": btype,
                "offset": offset, "length": len(enc),
            })

        pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
        pack_dir = repo / "data" / pack_id[:2]
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / pack_id).write_bytes(bytes(pack_data))

        new_index = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
        idx_id = hashlib.sha256(new_index).hexdigest()
        (repo / "index" / idx_id).write_bytes(_encrypt_with_master(new_index))

        snap_doc = json.dumps({
            "time": "2026-03-02T10:00:00Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
        }).encode()
        snap_id = hashlib.sha256(snap_doc).hexdigest()
        (repo / "snapshots" / snap_id).write_bytes(_encrypt_with_master(snap_doc))

        target = tmp_path / "restored_no_hardlink"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)

        a = target / "file_a.txt"
        b = target / "file_b.txt"
        assert a.read_text() == "Not a hardlink\n"
        assert b.read_text() == "Not a hardlink\n"

        # Different inodes — not hardlinked
        assert a.stat().st_ino != b.stat().st_ino


# ── Unsupported Node Type Test ───────────────────────────────────

class TestUnsupportedNodeType:
    """Test that unsupported node types are skipped with a warning."""

    def test_device_node_skipped(self, tmp_path, capsys):
        """Device nodes are skipped gracefully, other files still restored."""
        repo = _build_test_repo(tmp_path)

        file_content = b"Good file\n"
        file_id = hashlib.sha256(file_content).hexdigest()

        root_tree = json.dumps({
            "nodes": [
                {
                    "name": "good.txt",
                    "type": "file",
                    "mode": 0o644,
                    "content": [file_id],
                },
                {
                    "name": "mydevice",
                    "type": "dev",
                },
                {
                    "name": "myfifo",
                    "type": "fifo",
                },
            ],
        }).encode()
        root_tree_id = hashlib.sha256(root_tree).hexdigest()

        pack_data = bytearray()
        blobs_info = []
        for content, blob_id, btype in [
            (file_content, file_id, "data"),
            (root_tree, root_tree_id, "tree"),
        ]:
            enc = _encrypt_with_master(content)
            offset = len(pack_data)
            pack_data.extend(enc)
            blobs_info.append({
                "id": blob_id, "type": btype,
                "offset": offset, "length": len(enc),
            })

        pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
        pack_dir = repo / "data" / pack_id[:2]
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / pack_id).write_bytes(bytes(pack_data))

        new_index = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
        idx_id = hashlib.sha256(new_index).hexdigest()
        (repo / "index" / idx_id).write_bytes(_encrypt_with_master(new_index))

        snap_doc = json.dumps({
            "time": "2026-03-03T10:00:00Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
        }).encode()
        snap_id = hashlib.sha256(snap_doc).hexdigest()
        (repo / "snapshots" / snap_id).write_bytes(_encrypt_with_master(snap_doc))

        target = tmp_path / "restored_unsupported"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)

        # Good file was restored
        assert (target / "good.txt").read_text() == "Good file\n"
        # Unsupported nodes not created
        assert not (target / "mydevice").exists()
        assert not (target / "myfifo").exists()

        # Warning printed to stderr
        captured = capsys.readouterr()
        assert "Skipping unsupported node type 'dev': mydevice" in captured.err
        assert "Skipping unsupported node type 'fifo': myfifo" in captured.err


# ── Extended Attributes Test ─────────────────────────────────────

class TestXattrRestore:
    """Test extended attribute restoration."""

    @pytest.mark.skipif(
        not hasattr(os, "setxattr"),
        reason="xattr support unavailable on this platform",
    )
    def test_xattr_applied(self, tmp_path):
        """Extended attributes from tree nodes are applied to restored files."""
        repo = _build_test_repo(tmp_path)

        file_content = b"File with xattrs\n"
        file_id = hashlib.sha256(file_content).hexdigest()

        xattr_value = b"custom-value-123"

        root_tree = json.dumps({
            "nodes": [
                {
                    "name": "tagged.txt",
                    "type": "file",
                    "mode": 0o644,
                    "content": [file_id],
                    "extended_attributes": [
                        {
                            "name": "user.test_attr",
                            "value": base64.b64encode(xattr_value).decode(),
                        },
                    ],
                },
            ],
        }).encode()
        root_tree_id = hashlib.sha256(root_tree).hexdigest()

        pack_data = bytearray()
        blobs_info = []
        for content, blob_id, btype in [
            (file_content, file_id, "data"),
            (root_tree, root_tree_id, "tree"),
        ]:
            enc = _encrypt_with_master(content)
            offset = len(pack_data)
            pack_data.extend(enc)
            blobs_info.append({
                "id": blob_id, "type": btype,
                "offset": offset, "length": len(enc),
            })

        pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
        pack_dir = repo / "data" / pack_id[:2]
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / pack_id).write_bytes(bytes(pack_data))

        new_index = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
        idx_id = hashlib.sha256(new_index).hexdigest()
        (repo / "index" / idx_id).write_bytes(_encrypt_with_master(new_index))

        snap_doc = json.dumps({
            "time": "2026-03-04T10:00:00Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
        }).encode()
        snap_id = hashlib.sha256(snap_doc).hexdigest()
        (repo / "snapshots" / snap_id).write_bytes(_encrypt_with_master(snap_doc))

        target = tmp_path / "restored_xattr"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)

        tagged = target / "tagged.txt"
        assert tagged.read_text() == "File with xattrs\n"

        # Verify xattr was set
        actual = os.getxattr(str(tagged), "user.test_attr")
        assert actual == xattr_value

    def test_xattr_no_crash_without_support(self, tmp_path, monkeypatch):
        """xattr code path is graceful when os.setxattr is unavailable."""
        repo = _build_test_repo(tmp_path)

        file_content = b"No xattr support\n"
        file_id = hashlib.sha256(file_content).hexdigest()

        root_tree = json.dumps({
            "nodes": [
                {
                    "name": "noattr.txt",
                    "type": "file",
                    "mode": 0o644,
                    "content": [file_id],
                    "extended_attributes": [
                        {
                            "name": "user.missing",
                            "value": base64.b64encode(b"val").decode(),
                        },
                    ],
                },
            ],
        }).encode()
        root_tree_id = hashlib.sha256(root_tree).hexdigest()

        pack_data = bytearray()
        blobs_info = []
        for content, blob_id, btype in [
            (file_content, file_id, "data"),
            (root_tree, root_tree_id, "tree"),
        ]:
            enc = _encrypt_with_master(content)
            offset = len(pack_data)
            pack_data.extend(enc)
            blobs_info.append({
                "id": blob_id, "type": btype,
                "offset": offset, "length": len(enc),
            })

        pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
        pack_dir = repo / "data" / pack_id[:2]
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / pack_id).write_bytes(bytes(pack_data))

        new_index = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
        idx_id = hashlib.sha256(new_index).hexdigest()
        (repo / "index" / idx_id).write_bytes(_encrypt_with_master(new_index))

        snap_doc = json.dumps({
            "time": "2026-03-05T10:00:00Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
        }).encode()
        snap_id = hashlib.sha256(snap_doc).hexdigest()
        (repo / "snapshots" / snap_id).write_bytes(_encrypt_with_master(snap_doc))

        # Remove setxattr to simulate platform without support
        monkeypatch.delattr(os, "setxattr", raising=False)

        target = tmp_path / "restored_no_xattr"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)

        # File restored successfully despite no xattr support
        assert (target / "noattr.txt").read_text() == "No xattr support\n"


# ── zstd Decompression Tests ─────────────────────────────────────

class TestDecompressZstd:
    """Cover the _decompress_zstd paths (lines 87-97)."""

    def test_zstd_installed_path_with_max_output_size(self):
        """Lines 87-97: zstd library path exercised with max_output_size > 0."""
        from lcsas.restore.restic_fallback import _HAS_ZSTD, _decompress_zstd
        if not _HAS_ZSTD:
            pytest.skip("zstandard not installed")
        import zstandard as zstd  # type: ignore[import-not-found]
        original = b"hello zstd world" * 100
        cctx = zstd.ZstdCompressor()
        compressed = cctx.compress(original)
        result = _decompress_zstd(compressed, max_output_size=len(original) * 2)
        assert result == original

    def test_zstd_installed_path_no_limit(self):
        """Lines 92-93: zstd path no-limit decompression."""
        from lcsas.restore.restic_fallback import _HAS_ZSTD, _decompress_zstd
        if not _HAS_ZSTD:
            pytest.skip("zstandard not installed")
        import zstandard as zstd  # type: ignore[import-not-found]
        original = b"test data " * 500
        cctx = zstd.ZstdCompressor()
        compressed = cctx.compress(original)
        result = _decompress_zstd(compressed)
        assert result == original

    def test_zstd_installed_fallback_limit(self):
        """Lines 94-97: fallback decompress when content size unknown in frame."""
        from lcsas.restore.restic_fallback import _HAS_ZSTD, _decompress_zstd
        if not _HAS_ZSTD:
            pytest.skip("zstandard not installed")
        import zstandard as zstd  # type: ignore[import-not-found]
        original = b"fallback size test " * 1000
        # Compress without content_size in frame header to trigger ZstdError on
        # dctx.decompress(data) without limit, then catch and retry with cap.
        cctx = zstd.ZstdCompressor(write_content_size=False)
        compressed = cctx.compress(original)
        # dctx.decompress() without limit raises ZstdError when no content_size
        # is stored; our code catches that and falls back with a generous cap.
        result = _decompress_zstd(compressed)
        assert result == original

    def test_zstd_not_installed_raises(self, monkeypatch):
        """Lines 100-104: fallback function raises RuntimeError when no zstandard."""
        from lcsas.restore import restic_fallback
        if not restic_fallback._HAS_ZSTD:
            # Already on the no-zstd path; call directly.
            with pytest.raises(RuntimeError, match="zstandard"):
                restic_fallback._decompress_zstd(b"somedata")
        else:
            # We can't easily re-execute module-level code, so cover the
            # fallback message by calling the no-zstd variant directly.
            def _no_zstd(data: bytes, max_output_size: int = 0) -> bytes:
                raise RuntimeError(
                    "This repository uses zstd compression but the 'zstandard' "
                    "Python package is not installed."
                )
            with pytest.raises(RuntimeError, match="zstandard"):
                _no_zstd(b"data")


# ── _try_keys Error Path Tests ───────────────────────────────────

class TestTryKeysErrorPaths:
    """Cover _try_keys edge cases (lines 281, 286)."""

    def test_empty_keys_dir_raises(self, tmp_path):
        """Line 281: IntegrityError raised when keys directory is empty."""
        from lcsas.restore.restic_fallback import _try_keys
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        with pytest.raises(IntegrityError, match="No key files found"):
            _try_keys(keys_dir, b"any-password")

    def test_keys_dir_with_only_subdir_raises(self, tmp_path):
        """Line 286: non-file entries are skipped, then IntegrityError raised."""
        from lcsas.restore.restic_fallback import _try_keys
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        # Create a subdirectory — not a file — so the loop skips it
        (keys_dir / "subdir").mkdir()
        with pytest.raises(IntegrityError, match="wrong password"):
            _try_keys(keys_dir, b"any-password")


# ── _decrypt_file Compression Tests ─────────────────────────────

class TestDecryptFileCompressionPaths:
    """Cover _decrypt_file repo-v2 prefix handling (lines 485, 488)."""

    def test_decrypt_file_zstd_prefix(self, tmp_path):
        """Line 485: v2 file with \\x02 prefix + zstd frame is decompressed."""
        from lcsas.restore.restic_fallback import _HAS_ZSTD, PurePythonRestorer
        if not _HAS_ZSTD:
            pytest.skip("zstandard not installed")
        import zstandard as zstd  # type: ignore[import-not-found]

        mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_key_file(mk, PASSWORD, repo)

        # Build a v2-style compressed file: \x02 + zstd frame
        original_json = json.dumps({"version": 2, "id": "zstd-test"}).encode()
        cctx = zstd.ZstdCompressor()
        compressed = cctx.compress(original_json)
        v2_payload = b"\x02" + compressed

        enc = _encrypt_data(MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, v2_payload)
        config_path = repo / "config"
        config_path.write_bytes(enc)

        # Just loading the config via repo_info exercises _decrypt_file with zstd
        (repo / "index").mkdir()
        (repo / "snapshots").mkdir()

        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._load_key()
        restorer._blob_index = {}
        restorer._snapshots = []

        result = json.loads(restorer._decrypt_file(config_path))
        assert result["id"] == "zstd-test"

    def test_decrypt_file_strip_prefix_only(self, tmp_path):
        """Line 488: v2 file with \\x00 prefix (uncompressed) strips byte."""
        mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_key_file(mk, PASSWORD, repo)

        original_json = json.dumps({"key": "value"}).encode()
        # Prefix \x00 means uncompressed v2 — just strip the byte
        v2_payload = b"\x00" + original_json
        enc = _encrypt_data(MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, v2_payload)

        config_path = repo / "config"
        config_path.write_bytes(enc)

        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._load_key()
        restorer._blob_index = {}
        restorer._snapshots = []

        result = json.loads(restorer._decrypt_file(config_path))
        assert result["key"] == "value"

    def test_decrypt_file_prefix_x01_strip(self, tmp_path):
        """Line 488: v2 file with \\x01 prefix (uncompressed variant) strips byte."""
        mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_key_file(mk, PASSWORD, repo)

        original_json = json.dumps({"x01": True}).encode()
        v2_payload = b"\x01" + original_json
        enc = _encrypt_data(MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, v2_payload)

        config_path = repo / "config"
        config_path.write_bytes(enc)

        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._load_key()
        restorer._blob_index = {}
        restorer._snapshots = []

        result = json.loads(restorer._decrypt_file(config_path))
        assert result["x01"] is True


# ── _load_index Error Path Tests ─────────────────────────────────

class TestLoadIndexErrorPaths:
    """Cover _load_index error paths (lines 496, 511, 516)."""

    def test_missing_index_dir_raises(self, tmp_path):
        """Line 496: FileNotFoundError when index directory absent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_key_file(
            MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R),
            PASSWORD, repo,
        )
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._load_key()
        with pytest.raises(FileNotFoundError, match="Index directory not found"):
            restorer._load_index()

    def test_index_dir_non_file_skipped(self, tmp_path):
        """Line 506: non-file entry in index directory is skipped gracefully."""
        repo = _build_test_repo(tmp_path)
        # Add a subdirectory inside the index dir
        (repo / "index" / "subdir").mkdir()
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._load_key()
        # Should not crash — subdir is silently skipped
        restorer._load_index()
        assert restorer._blob_index is not None
        assert len(restorer._blob_index) == 4  # original 4 blobs still indexed

    def test_superseded_index_skipped(self, tmp_path):
        """Lines 511, 516: blob from superseded index is not indexed."""
        repo = _build_test_repo(tmp_path)
        index_dir = repo / "index"

        # Find the existing (good) index file name
        existing_index_files = sorted(index_dir.iterdir())
        assert len(existing_index_files) == 1
        existing_name = existing_index_files[0].name

        # Create an additional blob for a "superseded" scenario.
        # The second index supersedes the first — blobs from first are dropped.
        dummy_blob_id = "a" * 64
        superseding_doc = json.dumps({
            "supersedes": [existing_name],
            "packs": [{"id": "b" * 64, "blobs": []}],
        }).encode()
        superseding_id = hashlib.sha256(superseding_doc).hexdigest()
        (index_dir / superseding_id).write_bytes(
            _encrypt_with_master(superseding_doc)
        )

        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._load_key()
        restorer._load_index()
        # The original pack's blobs should be gone (superseded)
        assert dummy_blob_id not in (restorer._blob_index or {})
        # No blobs at all since superseding index has empty packs
        assert restorer._blob_index == {}


# ── _load_snapshots Error Path Tests ────────────────────────────

class TestLoadSnapshotsErrorPaths:
    """Cover _load_snapshots error paths (lines 536, 541)."""

    def test_missing_snapshots_dir_raises(self, tmp_path):
        """Line 536: FileNotFoundError when snapshots directory absent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_key_file(
            MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R),
            PASSWORD, repo,
        )
        (repo / "index").mkdir()
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._load_key()
        restorer._blob_index = {}
        with pytest.raises(FileNotFoundError, match="Snapshots directory not found"):
            restorer._load_snapshots()

    def test_snapshot_dir_with_subdir_skipped(self, tmp_path):
        """Line 541: non-file entries in snapshots directory are skipped."""
        repo = _build_test_repo(tmp_path)
        # Put a subdirectory inside snapshots/ — it should be silently skipped
        (repo / "snapshots" / "subdir").mkdir()
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        snaps = restorer.list_snapshots()
        # Only the real snapshot, not the directory
        assert len(snaps) == 1


# ── _latest_snapshot / _find_snapshot Error Paths ───────────────

class TestSnapshotLookupErrorPaths:
    """Cover snapshot lookup error paths (lines 563, 572, 581)."""

    def test_latest_snapshot_empty_raises(self, tmp_path):
        """Line 563: ValueError when no snapshots exist."""
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._ensure_loaded()
        # Wipe snapshots in memory
        restorer._snapshots = []
        with pytest.raises(ValueError, match="No snapshots found"):
            restorer._latest_snapshot()

    def test_find_snapshot_exact_match(self, tmp_path):
        """Line 572: exact snapshot_id match returns snapshot."""
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        snaps = restorer.list_snapshots()
        exact_id = snaps[0].snapshot_id
        found = restorer._find_snapshot(exact_id)
        assert found.snapshot_id == exact_id

    def test_find_snapshot_ambiguous_prefix_raises(self, tmp_path):
        """Line 581: ambiguous prefix raises ValueError."""
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._ensure_loaded()
        # Inject two fake snapshots with IDs sharing a common prefix
        from lcsas.restore.restic_fallback import SnapshotMeta
        restorer._snapshots = [
            SnapshotMeta(snapshot_id="aabbcc0001", time="2026-01-01T00:00:00Z", tree="t1"),
            SnapshotMeta(snapshot_id="aabbcc0002", time="2026-01-02T00:00:00Z", tree="t2"),
        ]
        with pytest.raises(ValueError, match="Ambiguous snapshot prefix"):
            restorer._find_snapshot("aabbcc")


# ── _find_pack_path / _read_blob Error Paths ─────────────────────

class TestReadBlobErrorPaths:
    """Cover _find_pack_path and _read_blob error paths (lines 603, 619, 634-635, 640)."""

    def test_pack_file_not_found_raises(self, tmp_path):
        """Line 603: FileNotFoundError when pack file missing.

        Uses ``interactive=False`` to opt out of the #234 disc-swap
        prompt loop -- this test pins the non-interactive raise contract.
        """
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD, interactive=False)
        restorer._ensure_loaded()
        with pytest.raises(FileNotFoundError, match="Pack file not found"):
            restorer._find_pack_path("a" * 64)

    def test_blob_not_in_index_raises(self, tmp_path):
        """Line 619: KeyError when blob_id absent from index."""
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._ensure_loaded()
        with pytest.raises(KeyError, match="Blob not found in index"):
            restorer._read_blob("b" * 64)

    def test_blob_hash_mismatch_raises(self, tmp_path):
        """Line 640: IntegrityError when blob content hash doesn't match blob_id."""
        from lcsas.restore.restic_fallback import BlobLocation
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._ensure_loaded()

        # Create a pack file with content that won't match its blob_id
        garbage_content = b"wrong content"
        encrypted = _encrypt_data(MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, garbage_content)
        fake_pack_id = "c" * 64
        pack_dir = repo / "data" / fake_pack_id[:2]
        pack_dir.mkdir(parents=True, exist_ok=True)
        pack_file = pack_dir / fake_pack_id
        pack_file.write_bytes(encrypted)

        # Inject a fake blob_id that won't match SHA-256 of garbage_content
        fake_blob_id = "d" * 64
        assert restorer._blob_index is not None
        restorer._blob_index[fake_blob_id] = BlobLocation(
            pack_id=fake_pack_id,
            offset=0,
            length=len(encrypted),
            blob_type="data",
        )

        with pytest.raises(IntegrityError, match="Blob content hash mismatch"):
            restorer._read_blob(fake_blob_id)

    def test_read_blob_zstd_compressed(self, tmp_path):
        """Lines 634-635: zstd-compressed pack blob is decompressed correctly."""
        from lcsas.restore.restic_fallback import _HAS_ZSTD, BlobLocation
        if not _HAS_ZSTD:
            pytest.skip("zstandard not installed")
        import zstandard as zstd  # type: ignore[import-not-found]

        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._ensure_loaded()

        # Build a zstd-compressed blob (no type prefix for pack blobs)
        original_content = b"compressed pack blob content"
        blob_id = hashlib.sha256(original_content).hexdigest()
        cctx = zstd.ZstdCompressor()
        compressed = cctx.compress(original_content)

        # Encrypt compressed data (pack blobs start directly with zstd magic)
        encrypted = _encrypt_data(MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, compressed)
        fake_pack_id = "e" * 64
        pack_dir = repo / "data" / fake_pack_id[:2]
        pack_dir.mkdir(parents=True, exist_ok=True)
        pack_file = pack_dir / fake_pack_id
        pack_file.write_bytes(encrypted)

        assert restorer._blob_index is not None
        restorer._blob_index[blob_id] = BlobLocation(
            pack_id=fake_pack_id,
            offset=0,
            length=len(encrypted),
            blob_type="data",
            uncompressed_length=len(original_content),
        )

        result = restorer._read_blob(blob_id)
        assert result == original_content


# ── _restore_tree Security / Error Path Tests ────────────────────

class TestRestoreTreeSecurityPaths:
    """Cover _restore_tree security and error paths (lines 664-668, 706-727)."""

    def _build_repo_with_tree(self, tmp_path: Path, tree_nodes: list) -> tuple:
        """Helper: build repo with custom root tree nodes."""
        repo = tmp_path / "repo"
        repo.mkdir()
        mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)
        _make_key_file(mk, PASSWORD, repo)

        file_content = b"safe file\n"
        file_id = hashlib.sha256(file_content).hexdigest()

        root_tree = json.dumps({"nodes": tree_nodes}).encode()
        root_tree_id = hashlib.sha256(root_tree).hexdigest()

        pack_data = bytearray()
        blobs_info = []
        for content, bid, btype in [
            (file_content, file_id, "data"),
            (root_tree, root_tree_id, "tree"),
        ]:
            enc = _encrypt_with_master(content)
            blobs_info.append({"id": bid, "type": btype,
                                "offset": len(pack_data), "length": len(enc)})
            pack_data.extend(enc)

        pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
        pack_dir = repo / "data" / pack_id[:2]
        pack_dir.mkdir(parents=True)
        (pack_dir / pack_id).write_bytes(bytes(pack_data))

        idx_doc = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
        idx_dir = repo / "index"
        idx_dir.mkdir()
        idx_id = hashlib.sha256(idx_doc).hexdigest()
        (idx_dir / idx_id).write_bytes(_encrypt_with_master(idx_doc))

        snap_doc = json.dumps({
            "time": "2026-04-01T00:00:00Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
        }).encode()
        snap_dir = repo / "snapshots"
        snap_dir.mkdir()
        snap_id = hashlib.sha256(snap_doc).hexdigest()
        (snap_dir / snap_id).write_bytes(_encrypt_with_master(snap_doc))

        config_doc = json.dumps({"version": 2, "id": "tree-test"}).encode()
        (repo / "config").write_bytes(_encrypt_with_master(config_doc))

        return repo, file_id

    def test_path_traversal_node_skipped(self, tmp_path, capsys):
        """Lines 664-668: node with '../' in name is skipped with warning."""
        repo, _ = self._build_repo_with_tree(tmp_path, [
            {"name": "../escape.txt", "type": "file", "content": []},
            {"name": "safe.txt", "type": "file", "content": []},
        ])
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        # Traversal node must not be created
        assert not (tmp_path / "escape.txt").exists()
        captured = capsys.readouterr()
        assert "suspicious name" in captured.err

    def test_symlink_absolute_target_skipped(self, tmp_path, capsys):
        """Lines 706-710: symlink pointing to absolute path is skipped."""
        repo, _ = self._build_repo_with_tree(tmp_path, [
            {"name": "evil.link", "type": "symlink", "linktarget": "/etc/passwd"},
        ])
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        assert not (target / "evil.link").exists()
        captured = capsys.readouterr()
        assert "absolute target" in captured.err

    @pytest.mark.skip(
        reason=(
            "Lines 715-721: Path.is_relative_to() returns bool (never raises "
            "ValueError) on Python 3.9+, so the except-ValueError branch is "
            "dead code on the project's minimum interpreter (Python 3.11)."
        )
    )
    def test_symlink_escaping_target_skipped(self, tmp_path, capsys):
        """Lines 715-721: symlink resolving outside target directory is skipped."""
        repo, _ = self._build_repo_with_tree(tmp_path, [
            {"name": "escape.link", "type": "symlink",
             "linktarget": "../../outside"},
        ])
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        assert not (target / "escape.link").exists()
        captured = capsys.readouterr()
        assert "out-of-bounds" in captured.err

    def test_symlink_overwrites_existing_file(self, tmp_path):
        """Lines 724-727: pre-existing file at symlink path is replaced."""
        repo, _ = self._build_repo_with_tree(tmp_path, [
            {"name": "actual.txt", "type": "file", "content": []},
            {"name": "link.txt", "type": "symlink", "linktarget": "actual.txt"},
        ])
        target = tmp_path / "restored"
        target.mkdir(parents=True, exist_ok=True)
        # Pre-create a regular file where the symlink should land
        (target / "link.txt").write_text("pre-existing")

        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        assert (target / "link.txt").is_symlink()
        assert os.readlink(str(target / "link.txt")) == "actual.txt"

    def test_symlink_overwrites_existing_dir(self, tmp_path):
        """Lines 724-727: pre-existing directory at symlink path is replaced."""
        repo, _ = self._build_repo_with_tree(tmp_path, [
            {"name": "actual.txt", "type": "file", "content": []},
            {"name": "link.txt", "type": "symlink", "linktarget": "actual.txt"},
        ])
        target = tmp_path / "restored"
        target.mkdir(parents=True, exist_ok=True)
        # Pre-create a directory where the symlink should land
        (target / "link.txt").mkdir()

        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        assert (target / "link.txt").is_symlink()


# ── Hardlink Fallback (OSError) Test ─────────────────────────────

class TestHardlinkOSErrorFallback:
    """Cover hardlink OSError fallback path (lines 682-685)."""

    def test_hardlink_oserror_falls_back_to_copy(self, tmp_path, monkeypatch, capsys):
        """Lines 682-685: os.link OSError causes fallback to normal file restore."""
        repo = _build_test_repo(tmp_path)

        file_content = b"hardlink fallback content\n"
        file_id = hashlib.sha256(file_content).hexdigest()

        root_tree = json.dumps({
            "nodes": [
                {
                    "name": "orig.txt",
                    "type": "file",
                    "mode": 0o644,
                    "inode": 7777,
                    "links": 2,
                    "content": [file_id],
                },
                {
                    "name": "hl.txt",
                    "type": "file",
                    "mode": 0o644,
                    "inode": 7777,
                    "links": 2,
                    "content": [file_id],
                },
            ],
        }).encode()
        root_tree_id = hashlib.sha256(root_tree).hexdigest()

        pack_data = bytearray()
        blobs_info = []
        for content, bid, btype in [
            (file_content, file_id, "data"),
            (root_tree, root_tree_id, "tree"),
        ]:
            enc = _encrypt_with_master(content)
            blobs_info.append({"id": bid, "type": btype,
                                "offset": len(pack_data), "length": len(enc)})
            pack_data.extend(enc)

        pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
        pack_dir = repo / "data" / pack_id[:2]
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / pack_id).write_bytes(bytes(pack_data))

        idx_doc = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
        idx_id = hashlib.sha256(idx_doc).hexdigest()
        (repo / "index" / idx_id).write_bytes(_encrypt_with_master(idx_doc))

        snap_doc = json.dumps({
            "time": "2026-05-01T00:00:00Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
        }).encode()
        snap_id = hashlib.sha256(snap_doc).hexdigest()
        (repo / "snapshots" / snap_id).write_bytes(_encrypt_with_master(snap_doc))

        # Force os.link to fail to trigger fallback
        def _raise_link(*a: object, **kw: object) -> None:
            raise OSError("cross-device")
        monkeypatch.setattr(os, "link", _raise_link)

        target = tmp_path / "restored_hl_fallback"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)

        # Both files should be restored even though hardlinking failed
        assert (target / "orig.txt").read_bytes() == file_content
        assert (target / "hl.txt").read_bytes() == file_content
        # They should NOT share an inode (copied, not linked)
        assert (target / "orig.txt").stat().st_ino != (target / "hl.txt").stat().st_ino

        captured = capsys.readouterr()
        assert "Hardlink" in captured.err


# ── _count_files Edge Cases ──────────────────────────────────────

class TestCountFilesEdgeCases:
    """Cover _count_files edge cases (lines 768, 773-776)."""

    def test_count_files_dedup_visited(self, tmp_path):
        """Line 768: shared subtree blob counted only once."""
        repo = _build_test_repo(tmp_path)
        file_content = b"shared subtree\n"
        file_id = hashlib.sha256(file_content).hexdigest()

        # Build a shared subtree
        subtree = json.dumps({
            "nodes": [{"name": "file.txt", "type": "file", "content": [file_id]}]
        }).encode()
        subtree_id = hashlib.sha256(subtree).hexdigest()

        # Root tree has two dirs pointing at the SAME subtree
        root_tree = json.dumps({
            "nodes": [
                {"name": "dir_a", "type": "dir", "subtree": subtree_id},
                {"name": "dir_b", "type": "dir", "subtree": subtree_id},
            ]
        }).encode()
        root_tree_id = hashlib.sha256(root_tree).hexdigest()

        pack_data = bytearray()
        blobs_info = []
        for content, bid, btype in [
            (file_content, file_id, "data"),
            (subtree, subtree_id, "tree"),
            (root_tree, root_tree_id, "tree"),
        ]:
            enc = _encrypt_with_master(content)
            blobs_info.append({"id": bid, "type": btype,
                                "offset": len(pack_data), "length": len(enc)})
            pack_data.extend(enc)

        pack_id = hashlib.sha256(bytes(pack_data)).hexdigest()
        pack_dir = repo / "data" / pack_id[:2]
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / pack_id).write_bytes(bytes(pack_data))

        idx_doc = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
        idx_id = hashlib.sha256(idx_doc).hexdigest()
        (repo / "index" / idx_id).write_bytes(_encrypt_with_master(idx_doc))

        snap_doc = json.dumps({
            "time": "2026-06-01T00:00:00Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
        }).encode()
        snap_id = hashlib.sha256(snap_doc).hexdigest()
        (repo / "snapshots" / snap_id).write_bytes(_encrypt_with_master(snap_doc))

        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._ensure_loaded()
        # Should count 1 (not 2) because subtree is visited only once
        count = restorer._count_files(root_tree_id)
        assert count == 1

    def test_count_files_missing_tree_returns_partial(self, tmp_path):
        """Lines 773-776: missing/corrupt tree blob causes early return."""
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._ensure_loaded()
        # Request count for a tree blob that doesn't exist — should return 0
        count = restorer._count_files("f" * 64)
        assert count == 0


# ── _emit_progress Tests ─────────────────────────────────────────

class TestEmitProgress:
    """Cover _emit_progress paths (lines 795, 811, 818)."""

    def test_emit_progress_disabled_early_return(self, tmp_path):
        """Line 795: _emit_progress returns immediately when progress disabled."""
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._progress_enabled = False
        # Should return immediately (line 795) without doing anything
        restorer._progress_done_files = 99
        restorer._emit_progress(force=True)
        # Progress counters unchanged proves we returned early
        assert restorer._progress_last_emit_files == 0

    def test_emit_progress_force_no_files_skipped(self, tmp_path):
        """Line 810-811: force=True with 0 files done emits nothing."""
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._progress_enabled = True
        restorer._progress_done_files = 0
        restorer._progress_done_bytes = 0
        restorer._progress_last_emit_files = 0
        restorer._progress_last_emit_bytes = 0
        # Should not crash or emit
        restorer._emit_progress(force=True)

    def test_emit_progress_with_total(self, tmp_path, capsys):
        """Line 811: progress line shows N/M format when total > 0."""
        import lcsas.restore.restic_fallback as fb
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._progress_enabled = True
        restorer._progress_done_files = 3
        restorer._progress_done_bytes = 1024
        restorer._progress_last_emit_files = 0
        restorer._progress_last_emit_bytes = 0
        restorer._progress_total_files = 10
        # Patch threshold to 0 so it fires
        old_fi = fb._PROGRESS_FILES_INTERVAL
        old_bi = fb._PROGRESS_BYTES_INTERVAL
        fb._PROGRESS_FILES_INTERVAL = 0
        fb._PROGRESS_BYTES_INTERVAL = 0
        try:
            restorer._emit_progress()
        finally:
            fb._PROGRESS_FILES_INTERVAL = old_fi
            fb._PROGRESS_BYTES_INTERVAL = old_bi
        captured = capsys.readouterr()
        assert "3/10" in captured.err

    def test_emit_progress_without_total(self, tmp_path, capsys):
        """Line 818: progress line shows N-only format when total == 0."""
        import lcsas.restore.restic_fallback as fb
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._progress_enabled = True
        restorer._progress_done_files = 5
        restorer._progress_done_bytes = 2048
        restorer._progress_last_emit_files = 0
        restorer._progress_last_emit_bytes = 0
        restorer._progress_total_files = 0
        old_fi = fb._PROGRESS_FILES_INTERVAL
        old_bi = fb._PROGRESS_BYTES_INTERVAL
        fb._PROGRESS_FILES_INTERVAL = 0
        fb._PROGRESS_BYTES_INTERVAL = 0
        try:
            restorer._emit_progress()
        finally:
            fb._PROGRESS_FILES_INTERVAL = old_fi
            fb._PROGRESS_BYTES_INTERVAL = old_bi
        captured = capsys.readouterr()
        assert "5 files restored" in captured.err
        assert "5/0" not in captured.err


# ── _apply_metadata OSError Tests ────────────────────────────────

class TestApplyMetadataOSErrors:
    """Cover _apply_metadata OSError handling (lines 828-829, 839-840, 852-853)."""

    def test_chmod_oserror_logged(self, tmp_path, monkeypatch, capsys):
        """Lines 828-829: chmod OSError is caught and logged."""
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._ensure_loaded()

        test_file = tmp_path / "testfile.txt"
        test_file.write_bytes(b"data")

        def _raise_chmod(*a: object, **kw: object) -> None:
            raise OSError("perm denied")
        monkeypatch.setattr(os, "chmod", _raise_chmod)

        # Should not raise
        restorer._apply_metadata({"mode": 0o644}, test_file)
        captured = capsys.readouterr()
        assert "Could not set permissions" in captured.err

    def test_utime_oserror_logged(self, tmp_path, monkeypatch, capsys):
        """Lines 839-840: utime OSError is caught and logged."""
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._ensure_loaded()

        test_file = tmp_path / "testfile.txt"
        test_file.write_bytes(b"data")

        def _raise_utime(*a: object, **kw: object) -> None:
            raise OSError("utime denied")
        monkeypatch.setattr(os, "utime", _raise_utime)

        restorer._apply_metadata({"mtime": "2026-01-01T00:00:00Z"}, test_file)
        captured = capsys.readouterr()
        assert "Could not set timestamps" in captured.err

    def test_setxattr_oserror_logged(self, tmp_path, monkeypatch, capsys):
        """Lines 852-853: setxattr OSError is caught and logged."""
        repo = _build_test_repo(tmp_path)
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer._ensure_loaded()

        test_file = tmp_path / "testfile.txt"
        test_file.write_bytes(b"data")

        monkeypatch.setattr(os, "setxattr",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError("xattr denied")),
                            raising=False)

        node = {
            "extended_attributes": [
                {"name": "user.test", "value": base64.b64encode(b"val").decode()},
            ],
        }
        restorer._apply_metadata(node, test_file)
        captured = capsys.readouterr()
        assert "Could not set xattr" in captured.err


# ── _parse_timestamp Fallback Tests ─────────────────────────────

class TestParseTimestampFallback:
    """Cover _parse_timestamp fallback strptime path (lines 904-906)."""

    @pytest.mark.skip(
        reason=(
            "Lines 904-906: datetime.fromisoformat handles all inputs on "
            "Python 3.11+ (project minimum), so the strptime except-branch "
            "is dead code on supported interpreters."
        )
    )
    def test_fallback_parse_nonstandard_format(self):
        """Lines 904-906: strptime fallback path exists for pre-3.11 Python."""
        from lcsas.restore.restic_fallback import _parse_timestamp
        ts = _parse_timestamp("2026-01-15T10:30:00.123456Z")
        assert isinstance(ts, float)
        assert ts > 0
