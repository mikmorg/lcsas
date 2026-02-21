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
import struct
from pathlib import Path

import pytest

from lcsas.restore._aes_pure import (
    aes_ctr,
    aes_encrypt_block,
    key_schedule,
)
from lcsas.restore.restic_fallback import (
    BlobLocation,
    IntegrityError,
    MasterKey,
    PurePythonRestorer,
    SnapshotMeta,
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
        for d in data_dir.iterdir():
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass

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

        mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)

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
