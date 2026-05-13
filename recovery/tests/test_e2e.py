"""End-to-end test: build a synthetic restic v1 repo with the Python
fallback's crypto helpers, then run lcsas-restore against it and verify
the extracted files match the originals byte-for-byte.

This is the killer validation: if it passes, the entire C89 pipeline
(scrypt -> AEAD -> JSON -> index -> tree -> file materialization) is
correct end-to-end.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

# Reuse the project's vetted crypto helpers.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from lcsas.restore._aes_pure import aes_ctr, aes_encrypt_block, key_schedule  # noqa: E402
from lcsas.restore.restic_fallback import _clamp_r, _poly1305_mac  # noqa: E402

BINARY = Path(__file__).resolve().parents[1] / "build" / "lcsas-restore"


def aead_encrypt(encrypt_key: bytes, mac_k: bytes, mac_r: bytes,
                 plaintext: bytes) -> bytes:
    """Encrypt under the restic AEAD scheme: IV(16) || ct || MAC(16)."""
    iv = os.urandom(16)
    ct = aes_ctr(encrypt_key, iv, plaintext)
    rk = key_schedule(mac_k)
    s = aes_encrypt_block(iv, rk)
    mac = _poly1305_mac(mac_r, s, ct)
    return iv + ct + mac


def build_repo(repo_dir: Path, password: str,
               files: dict[str, bytes],
               v2: bool = False,
               split_packs: int = 1) -> str:
    """Build a synthetic restic repo (v1 by default) containing the given files.

    With v2=True, blob plaintexts are zstd-compressed before encryption
    (matching real restic v2 inline-blob layout: raw zstd frame, no
    type prefix).  Repository files (index/snapshots/config) get the
    v2 type-prefix byte and may also be compressed.

    With split_packs >= 2, data blobs are partitioned into that many
    pack files (round-robin).  The tree blob always goes in the last
    pack.  This is used by test_multidisc.py to simulate an archive
    that spans multiple physical discs.

    Returns the snapshot ID (hex).
    """
    try:
        import zstandard as zstd_lib
    except ImportError:
        zstd_lib = None
    if v2 and zstd_lib is None:
        raise RuntimeError("v2 fixtures require the `zstandard` python module")
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "keys").mkdir()
    (repo_dir / "index").mkdir()
    (repo_dir / "snapshots").mkdir()
    (repo_dir / "data").mkdir()

    # ── Master key ──
    master_encrypt = os.urandom(32)
    master_mac_k = os.urandom(16)
    master_mac_r = os.urandom(16)
    master_doc = {
        "encrypt": base64.b64encode(master_encrypt).decode(),
        "mac": {
            "k": base64.b64encode(master_mac_k).decode(),
            "r": base64.b64encode(master_mac_r).decode(),
        },
    }
    master_json = json.dumps(master_doc).encode()

    # ── KEK from scrypt ──
    salt = os.urandom(32)
    N, r, p = 16, 1, 1   # tiny params for test speed; lcsas-restore reads from JSON
    derived = hashlib.scrypt(password.encode(), salt=salt, n=N, r=r, p=p, dklen=64)
    kek_encrypt = derived[:32]
    kek_mac_k = derived[32:48]
    kek_mac_r = derived[48:64]
    encrypted_master = aead_encrypt(kek_encrypt, kek_mac_k, kek_mac_r, master_json)

    key_doc = {
        "hostname": "test",
        "username": "test",
        "kdf": "scrypt",
        "N": N, "r": r, "p": p,
        "salt": base64.b64encode(salt).decode(),
        "data": base64.b64encode(encrypted_master).decode(),
    }
    key_file_id = hashlib.sha256(encrypted_master).hexdigest()
    (repo_dir / "keys" / key_file_id).write_text(json.dumps(key_doc))

    # ── Build one pack containing all data blobs + a tree blob ──
    pack_blobs = []      # list[(type, plaintext, encrypted_bytes, offset, length)]
    node_entries = []    # list[dict] for tree blob

    for name, content in files.items():
        node_entry = {
            "name": name,
            "type": "file",
            "mode": 0o644,
            "mtime": "2026-01-15T10:30:00.000000000Z",
            "uid": 1000,
            "gid": 1000,
            "size": len(content),
            "content": [],
        }
        if content:
            blob_id = hashlib.sha256(content).digest()
            if v2:
                stored_plain = zstd_lib.ZstdCompressor().compress(content)
            else:
                stored_plain = content
            encrypted = aead_encrypt(master_encrypt, master_mac_k, master_mac_r,
                                     stored_plain)
            pack_blobs.append(
                ("data", content, encrypted, blob_id, len(content)))
            node_entry["content"] = [blob_id.hex()]
        node_entries.append(node_entry)

    tree_doc = {"nodes": node_entries}
    tree_plain = json.dumps(tree_doc).encode()
    tree_id = hashlib.sha256(tree_plain).digest()
    if v2:
        tree_stored = zstd_lib.ZstdCompressor().compress(tree_plain)
    else:
        tree_stored = tree_plain
    tree_encrypted = aead_encrypt(master_encrypt, master_mac_k, master_mac_r,
                                  tree_stored)
    pack_blobs.append(("tree", tree_plain, tree_encrypted, tree_id,
                       len(tree_plain)))

    # Partition the blobs into split_packs groups (round-robin for data
    # blobs; the tree blob always lands in the last pack).
    if split_packs < 1:
        split_packs = 1
    pack_groups = [[] for _ in range(split_packs)]
    data_idx = 0
    tree_entry = None
    for entry in pack_blobs:
        if entry[0] == "tree":
            tree_entry = entry
            continue
        pack_groups[data_idx % split_packs].append(entry)
        data_idx += 1
    if tree_entry is not None:
        pack_groups[-1].append(tree_entry)

    # Each pack is laid out as:
    #   encrypted blobs concatenated, then encrypted header,
    #   then u32 LE header length.
    packs_for_index = []
    for group in pack_groups:
        if not group:
            continue
        pack_body = b""
        blob_entries_for_index = []
        header_entries = b""
        for btype, plain, enc, blob_id, ulen in group:
            offset = len(pack_body)
            length = len(enc)
            pack_body += enc
            entry = {
                "id": blob_id.hex(),
                "type": btype,
                "offset": offset,
                "length": length,
            }
            if v2:
                entry["uncompressed_length"] = ulen
            blob_entries_for_index.append(entry)
            type_byte = b"\x00" if btype == "data" else b"\x01"
            header_entries += type_byte + struct.pack("<I", length) + blob_id

        header_enc = aead_encrypt(master_encrypt, master_mac_k, master_mac_r,
                                  header_entries)
        full_pack = pack_body + header_enc + struct.pack("<I", len(header_enc))
        pack_id = hashlib.sha256(full_pack).hexdigest()
        # Flat layout (data/<id>).
        (repo_dir / "data" / pack_id).write_bytes(full_pack)
        packs_for_index.append({"id": pack_id, "blobs": blob_entries_for_index})

    def _encode_repo_file(plain: bytes) -> bytes:
        """Encode a top-level repo file (index/snapshot/config).

        v1: encrypt plaintext.
        v2: prefix 0x00 (uncompressed) or 0x01/0x02 (zstd) before encrypt.
        """
        if not v2:
            return aead_encrypt(master_encrypt, master_mac_k, master_mac_r, plain)
        # Always compress repo files in our v2 fixtures.
        comp = zstd_lib.ZstdCompressor().compress(plain)
        return aead_encrypt(master_encrypt, master_mac_k, master_mac_r,
                            b"\x02" + comp)

    # ── Index ──
    # Reference every pack we wrote; with split_packs=1 this is a
    # single-entry list, matching the previous behaviour.
    index_doc = {"packs": packs_for_index}
    index_plain = json.dumps(index_doc).encode()
    index_enc = _encode_repo_file(index_plain)
    index_id = hashlib.sha256(index_enc).hexdigest()
    (repo_dir / "index" / index_id).write_bytes(index_enc)

    # ── Snapshot ──
    snap_doc = {
        "time": "2026-01-15T10:30:00.000000000Z",
        "tree": tree_id.hex(),
        "paths": ["/home/test"],
        "hostname": "test",
        "username": "test",
        "tags": [],
    }
    snap_plain = json.dumps(snap_doc).encode()
    snap_enc = _encode_repo_file(snap_plain)
    snap_id = hashlib.sha256(snap_enc).hexdigest()
    (repo_dir / "snapshots" / snap_id).write_bytes(snap_enc)

    return snap_id


def _run_one(label: str, password: str, files: dict[str, bytes],
             v2: bool) -> int:
    tmp = Path(tempfile.mkdtemp(prefix=f"lcsas_e2e_{label}_"))
    try:
        repo = tmp / "repo"
        target = tmp / "out"
        pwfile = tmp / "pw"
        pwfile.write_text(password + "\n")
        snap_id = build_repo(repo, password, files, v2=v2)
        print(f"[{label}] built synthetic repo at {repo}, snap={snap_id[:12]}")
        result = subprocess.run(
            [str(BINARY),
             "--repo", str(repo),
             "--password-file", str(pwfile),
             "--target", str(target),
             "--snapshot", "latest",
             "--verbose"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"FAIL [{label}]: lcsas-restore exit={result.returncode}",
                  file=sys.stderr)
            print("STDOUT:", result.stdout, file=sys.stderr)
            print("STDERR:", result.stderr, file=sys.stderr)
            return 1
        for name, content in files.items():
            restored = target / name
            if not restored.exists():
                print(f"FAIL [{label}]: {name} not restored", file=sys.stderr)
                print("STDERR:", result.stderr, file=sys.stderr)
                return 1
            if restored.read_bytes() != content:
                print(f"FAIL [{label}]: {name} content mismatch",
                      file=sys.stderr)
                return 1
        print(f"[{label}] OK")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if not BINARY.exists():
        print(f"FAIL: {BINARY} not built", file=sys.stderr)
        return 1

    files = {
        "hello.txt":  b"Hello, LCSAS recovery!\n",
        "ascii.txt":  bytes(range(32, 127)) * 4,
        "binary.bin": os.urandom(8192),
        "empty.txt":  b"",
        "compressible.txt": b"banana " * 4096,
    }
    password = "correct-horse-battery-staple"

    fails = 0
    fails += _run_one("v1", password, files, v2=False)

    try:
        import zstandard  # noqa: F401
        fails += _run_one("v2-zstd", password, files, v2=True)
    except ImportError:
        print("SKIP v2-zstd (no zstandard module installed)", file=sys.stderr)

    if fails == 0:
        print("test_e2e: OK")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
