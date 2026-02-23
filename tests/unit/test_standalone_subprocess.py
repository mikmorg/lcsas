"""Test standalone_restorer.py as a subprocess.

Proves that the auto-generated standalone restorer script can be
invoked as a subprocess using only Python 3 stdlib and correctly
restore data from a synthetic restic repository.

This catches packaging/concatenation regressions — the script must
parse correctly as Python, locate all functions it needs, and
produce correct output without any LCSAS package imports.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.restore._aes_pure import (
    aes_ctr,
    aes_encrypt_block,
    key_schedule,
)
from lcsas.restore.restic_fallback import (
    MasterKey,
    _poly1305_mac,
)
from lcsas.restore.standalone_builder import build_standalone

# ── Test helpers (same synthetic repo builder as test_chaos.py) ──

PASSWORD = b"standalone-subprocess-test-pw"
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
) -> bytes:
    iv = os.urandom(16)
    ciphertext = aes_ctr(encrypt_key, iv, plaintext)
    mac_rk = key_schedule(mac_k)
    s = aes_encrypt_block(iv, mac_rk)
    tag = _poly1305_mac(mac_r, s, ciphertext)
    return iv + ciphertext + tag


def _encrypt_with_master(plaintext: bytes) -> bytes:
    return _encrypt_data(MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, plaintext)


def _build_repo_and_script(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a synthetic repo + the standalone_restorer.py script.

    Returns (repo_dir, password_file, script_path).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)

    # Key file
    n, r, p = 1024, 8, 1
    salt = os.urandom(64)
    derived = hashlib.scrypt(PASSWORD, salt=salt, n=n, r=r, p=p, dklen=64)
    master_json = json.dumps({
        "encrypt": base64.b64encode(mk.encrypt).decode(),
        "mac": {
            "k": base64.b64encode(mk.mac_k).decode(),
            "r": base64.b64encode(mk.mac_r).decode(),
        },
    }).encode()
    enc_master = _encrypt_data(derived[:32], derived[32:48], derived[48:64], master_json)
    key_doc = {
        "created": "2026-01-01T00:00:00Z",
        "username": "test", "hostname": "testhost",
        "kdf": "scrypt", "N": n, "r": r, "p": p,
        "salt": base64.b64encode(salt).decode(),
        "data": base64.b64encode(enc_master).decode(),
    }
    keys_dir = repo / "keys"
    keys_dir.mkdir()
    (keys_dir / "testkey01").write_text(json.dumps(key_doc))

    # Data blobs
    file_content = b"Standalone subprocess test data!\n"
    file_id = hashlib.sha256(file_content).hexdigest()

    root_tree = json.dumps({
        "nodes": [{
            "name": "standalone_test.txt",
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

    # Pack
    blobs_info: list[dict] = []
    pack_data = bytearray()
    for content, blob_id, btype in [
        (file_content, file_id, "data"),
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

    # Index
    idx_doc = json.dumps({"packs": [{"id": pack_id, "blobs": blobs_info}]}).encode()
    idx_dir = repo / "index"
    idx_dir.mkdir()
    idx_id = hashlib.sha256(idx_doc).hexdigest()
    (idx_dir / idx_id).write_bytes(_encrypt_with_master(idx_doc))

    # Snapshot
    snap_doc = json.dumps({
        "time": "2026-01-01T01:00:00.000000000Z",
        "tree": root_tree_id,
        "paths": ["/test"],
        "hostname": "testhost",
        "username": "test",
    }).encode()
    snap_dir = repo / "snapshots"
    snap_dir.mkdir()
    snap_id = hashlib.sha256(snap_doc).hexdigest()
    (snap_dir / snap_id).write_bytes(_encrypt_with_master(snap_doc))

    # Config
    config_doc = json.dumps({"version": 2, "id": "standalone_test"}).encode()
    (repo / "config").write_bytes(_encrypt_with_master(config_doc))

    # Password file
    pw_file = tmp_path / "password.txt"
    pw_file.write_bytes(PASSWORD)

    # Generate standalone_restorer.py
    script = tmp_path / "standalone_restorer.py"
    script.write_text(build_standalone())
    os.chmod(str(script), 0o755)

    return repo, pw_file, script


# =====================================================================
# Tests
# =====================================================================


class TestStandaloneSubprocess:
    """Run standalone_restorer.py as a subprocess against a synthetic repo."""

    def test_restore_via_subprocess(self, tmp_path):
        """The standalone script restores data correctly as a subprocess."""
        repo, pw_file, script = _build_repo_and_script(tmp_path)
        target = tmp_path / "output"
        target.mkdir()

        result = subprocess.run(
            [
                sys.executable, str(script),
                "--repo", str(repo),
                "--password-file", str(pw_file),
                "--target", str(target),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"Standalone restorer failed:\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        restored = target / "standalone_test.txt"
        assert restored.is_file(), f"Expected file not found. Dir contents: {list(target.rglob('*'))}"
        assert restored.read_bytes() == b"Standalone subprocess test data!\n"

    def test_list_snapshots_via_subprocess(self, tmp_path):
        """--list-snapshots should print snapshot info and exit 0."""
        repo, pw_file, script = _build_repo_and_script(tmp_path)

        result = subprocess.run(
            [
                sys.executable, str(script),
                "--repo", str(repo),
                "--password-file", str(pw_file),
                "--target", "/unused",
                "--list-snapshots",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"--list-snapshots failed:\n{result.stderr}"
        )
        # Output should mention the hostname
        assert "testhost" in result.stdout

    def test_info_via_subprocess(self, tmp_path):
        """--info should print repo info and exit 0."""
        repo, pw_file, script = _build_repo_and_script(tmp_path)

        result = subprocess.run(
            [
                sys.executable, str(script),
                "--repo", str(repo),
                "--password-file", str(pw_file),
                "--target", "/unused",
                "--info",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"--info failed:\n{result.stderr}"

    def test_wrong_password_fails(self, tmp_path):
        """Wrong password should cause non-zero exit."""
        repo, _, script = _build_repo_and_script(tmp_path)
        bad_pw = tmp_path / "bad_pw.txt"
        bad_pw.write_bytes(b"wrong-password")
        target = tmp_path / "output"
        target.mkdir()

        result = subprocess.run(
            [
                sys.executable, str(script),
                "--repo", str(repo),
                "--password-file", str(bad_pw),
                "--target", str(target),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0

    def test_missing_repo_fails(self, tmp_path):
        """Non-existent repo path should cause an error."""
        _, pw_file, script = _build_repo_and_script(tmp_path)

        result = subprocess.run(
            [
                sys.executable, str(script),
                "--repo", str(tmp_path / "nonexistent"),
                "--password-file", str(pw_file),
                "--target", str(tmp_path / "out"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0

    def test_script_has_no_lcsas_imports(self, tmp_path):
        """The generated script must not contain any 'from lcsas' imports."""
        _, _, script = _build_repo_and_script(tmp_path)
        content = script.read_text()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "from lcsas" not in stripped, (
                f"Standalone script still has lcsas import: {stripped}"
            )
            assert "import lcsas" not in stripped, (
                f"Standalone script still has lcsas import: {stripped}"
            )
