#!/usr/bin/env python3
"""Generate a minimal but valid restic-format fixture for C unit tests.

Produces a directory containing:
  keys/<hex>           — encrypted master key (scrypt + AES-CTR + Poly1305)
  index/<hex>          — encrypted index JSON listing one data + one tree blob
  snapshots/<hex>      — encrypted snapshot pointing at the tree
  data/<XX>/<hex>      — pack file with the two encrypted blobs + header

The fixture is deterministic given a fixed password and seed: the test
binaries embed expected hex IDs and assert on them.

Run:
    python3 recovery/tests/fixtures/gen_fixture.py recovery/tests/fixtures/repo

Defaults:
    password     = "test"
    master keys  = static (see below) — gives a reproducible fixture
    salt + IVs   = all-zero (deterministic) — fine for fixture, NOT for
                   production
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from lcsas.restore._aes_pure import aes_ctr, aes_encrypt_block, key_schedule
from lcsas.restore.restic_fallback import _poly1305_mac


PASSWORD = b"test"

# Static "master key" — same for every regenerated fixture.
MASTER_ENCRYPT = bytes(range(32))                       # 00..1F
MASTER_MAC_K   = bytes(range(0x20, 0x30))               # 20..2F
MASTER_MAC_R   = bytes(range(0x30, 0x40))               # 30..3F

# Static salt/IVs — fine for fixture (NOT production). All zeros for
# reproducibility, except for one byte to keep them distinct so the
# index-file IV != key-file IV.
SALT_KEY   = b"\x00" * 16
IV_KEYFILE = b"\x00" * 16
IV_INDEX   = b"\x01" + b"\x00" * 15
IV_SNAP    = b"\x02" + b"\x00" * 15
IV_DATA    = b"\x03" + b"\x00" * 15  # data blob in pack
IV_TREE    = b"\x04" + b"\x00" * 15  # tree blob in pack
IV_HEADER  = b"\x05" + b"\x00" * 15  # pack header
IV_XATTR   = b"\x16" + b"\x00" * 15  # xattr content blob in pack
IV_HLINK   = b"\x17" + b"\x00" * 15  # hardlink content blob in pack
IV_TZ      = b"\x18" + b"\x00" * 15  # trailing-zeros data blob in pack

N, R, P = 16384, 8, 1  # smaller-than-default scrypt params to keep
                        # gen + test fast (still safe for fixture use)


def encrypt_authenticated(
    encrypt_key: bytes, mac_k: bytes, mac_r: bytes, iv: bytes, plaintext: bytes
) -> bytes:
    """Encrypt plaintext with AES-CTR + Poly1305 in the restic format.

    Output: IV (16) || ciphertext || MAC (16)
    """
    ciphertext = aes_ctr(encrypt_key, iv, plaintext)
    mac_rk = key_schedule(mac_k)
    s = aes_encrypt_block(iv, mac_rk)
    tag = _poly1305_mac(mac_r, s, ciphertext)
    return iv + ciphertext + tag


def derive_kek(password: bytes, salt: bytes) -> tuple[bytes, bytes, bytes]:
    """scrypt(password, salt) → (encrypt, mac_k, mac_r) triple."""
    derived = hashlib.scrypt(
        password, salt=salt, n=N, r=R, p=P, dklen=64,
        maxmem=max(128 * R * (N + P + 2) * 2, 2**25),
    )
    return derived[:32], derived[32:48], derived[48:64]


def make_key_file(out_path: Path) -> None:
    """Write one key file at out_path with master key encrypted by PASSWORD."""
    kek_enc, kek_mk, kek_mr = derive_kek(PASSWORD, SALT_KEY)

    master_json = json.dumps({
        "encrypt": base64.b64encode(MASTER_ENCRYPT).decode(),
        "mac": {
            "k": base64.b64encode(MASTER_MAC_K).decode(),
            "r": base64.b64encode(MASTER_MAC_R).decode(),
        },
    }).encode()

    encrypted = encrypt_authenticated(
        kek_enc, kek_mk, kek_mr, IV_KEYFILE, master_json
    )

    doc = {
        "created": "2026-05-21T00:00:00Z",
        "username": "test",
        "hostname": "test",
        "kdf": "scrypt",
        "N": N, "r": R, "p": P,
        "salt": base64.b64encode(SALT_KEY).decode(),
        "data": base64.b64encode(encrypted).decode(),
    }
    out_path.write_text(json.dumps(doc, indent=2))


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def make_pack_and_index(
    repo_dir: Path,
) -> tuple[str, str, str, str]:
    """Build a pack file with several blobs covering tree.c walk branches.

    Layout:
      data blob 1: file content "hello from lcsas-restore fixture\n"
      sub_tree:    nested tree with one empty file
      root_tree:   top-level with one file + one dir + one symlink

    Returns (pack_id_hex, data_blob_id_hex, root_tree_id_hex, root_tree_id_hex).
    """
    # ── Data blob (zstd-compressed file payload).
    #
    # Restic v2 stores pack blobs as zstd-compressed *inside* the
    # encrypted layer (no v2 prefix byte — the decrypted payload
    # starts directly with the zstd magic 0x28 b5 2f fd).  Exercises
    # the inline-decompression branch in repo.c read_blob.
    #
    # blob_id = sha256(*uncompressed* plaintext).
    import zstandard
    data_plain = b"hello from lcsas-restore fixture\n"
    data_blob_id = sha256(data_plain)
    data_compressed = zstandard.ZstdCompressor().compress(data_plain)
    data_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_DATA, data_compressed
    )

    # ── Xattr content blob (covers apply_node_xattrs lines 330-397) ──
    # A small file payload for the node that carries extended_attributes.
    xattr_plain = b"xattr test content"
    xattr_blob_id = sha256(xattr_plain)
    xattr_compressed = zstandard.ZstdCompressor().compress(xattr_plain)
    xattr_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_XATTR, xattr_compressed
    )

    # ── Hardlink content blob (covers hardlink success branch 541-563) ─
    # A small file payload shared by two file nodes with inode 9001.
    hlink_plain = b"hardlink content"
    hlink_blob_id = sha256(hlink_plain)
    hlink_compressed = zstandard.ZstdCompressor().compress(hlink_plain)
    hlink_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_HLINK, hlink_compressed
    )

    # ── Trailing-zeros data blob (Issue #264: cover tree.c:263) ──────
    #
    # write_blob_sparse line 263 is the loop-exit `return 0` that fires
    # only when the buffer ENDS with a zero run.  The existing data blob
    # ends with '\n' so the early-exit at line 247 fires first.
    #
    # Content: 64 non-zero bytes then 8192 zero bytes.
    #   - non-zero prefix  → written via lcsas_write_exact
    #   - zero run (8192 ≥ 4096 HOLE_MIN)  → lseek (lines 254/255)
    #   - zend == len  → loop exits, line 263 is reached
    #
    # Stored as a plain (non-zstd) data blob (no uncompressed_length
    # hint in the index) so repo.c takes the probe-size branch.
    trailing_zeros_plain = b"\xff" * 64 + b"\x00" * 8192
    trailing_zeros_blob_id = sha256(trailing_zeros_plain)
    trailing_zeros_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_TZ, trailing_zeros_plain
    )

    # ── Sub-tree (nested directory contents) ──────────────────────
    sub_tree_doc = {
        "nodes": [
            {
                "name": "empty.txt",
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "atime": "2026-05-21T00:00:00Z",
                "ctime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "user": "test", "group": "test",
                "inode": 2, "device_id": 0,
                "size": 0,
                "links": 1,
                "content": [],  # empty content array
            },
            {
                # File node with NO "content" field at all.  Hits the
                # `content_idx < 0` branch in restore_file_node (tree.c
                # ~line 128-131) which closes fd and returns 0.
                "name": "no_content.txt",
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "size": 0,
            },
        ]
    }
    # Sub-tree is also zstd-compressed but the index entry will
    # OMIT uncompressed_length — this exercises the probe-size branch
    # (lcsas_zstd_decode with out=NULL) in repo.c read_blob.
    sub_tree_plain = json.dumps(sub_tree_doc).encode()
    sub_tree_blob_id = sha256(sub_tree_plain)
    sub_tree_compressed = zstandard.ZstdCompressor().compress(sub_tree_plain)
    sub_tree_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x06" + b"\x00" * 15, sub_tree_compressed
    )

    # ── Root tree: file + dir + symlink + unsupported node ────────
    tree_doc = {
        "nodes": [
            {
                "name": "hello.txt",
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "atime": "2026-05-21T00:00:00Z",
                "ctime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "user": "test", "group": "test",
                "inode": 1, "device_id": 0,
                "size": len(data_plain),
                "links": 1,
                "content": [data_blob_id.hex()],
            },
            {
                "name": "subdir",
                "type": "dir",
                "mode": 493,           # 0o755
                "mtime": "2026-05-21T00:00:00Z",
                "atime": "2026-05-21T00:00:00Z",
                "ctime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "user": "test", "group": "test",
                "inode": 3, "device_id": 0,
                "subtree": sub_tree_blob_id.hex(),
            },
            {
                "name": "link.txt",
                "type": "symlink",
                "mode": 511,           # 0o777
                "mtime": "2026-05-21T00:00:00Z",
                "atime": "2026-05-21T00:00:00Z",
                "ctime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "user": "test", "group": "test",
                "inode": 4, "device_id": 0,
                "linktarget": "hello.txt",
            },
            {
                "name": "device.dev",
                "type": "chardev",     # unsupported -> exercises "skip" path
                "mode": 384,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 0, "gid": 0,
                "user": "root", "group": "root",
                "inode": 5, "device_id": 0,
            },
            {
                "name": "../escape",   # unsafe name -> exercises skip
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "size": 0,
                "content": [],
            },
            {
                "name": "foo/bar",     # slash in name -> rejected by tree.c
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "size": 0,
                "content": [],
            },
            {
                "name": "evil_link",
                "type": "symlink",
                "linktarget": "../../../etc/passwd",  # escapes target_root
                "mode": 511,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
            },
            {
                # Symlink with a linktarget > 1024 bytes: exercises the
                # lcsas_json_decode_string overflow branch in tree.c.
                # The decode fails (return -1) and the loop `continue`s
                # without restoring the node.
                "name": "long_target",
                "type": "symlink",
                "linktarget": "/" + ("x" * 2048),
                "mode": 511,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
            },
            {
                # File with extended_attributes — exercises apply_node_xattrs
                # (tree.c lines 330-397).  The value is base64("test").
                "name": "xattr_test_file",
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "atime": "2026-05-21T00:00:00Z",
                "ctime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "user": "test", "group": "test",
                "inode": 8000, "device_id": 0,
                "size": len(xattr_plain),
                "links": 1,
                "content": [xattr_blob_id.hex()],
                "extended_attributes": [
                    {"name": "user.lcsas-test", "value": "dGVzdA=="},
                ],
            },
            {
                # First node of a hardlink pair — exercises hardlink
                # success branch in restore_file_node (tree.c 541-563).
                # Both this node and hardlink_b share inode 9001.
                "name": "hardlink_a",
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "atime": "2026-05-21T00:00:00Z",
                "ctime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "user": "test", "group": "test",
                "inode": 9001, "device_id": 0,
                "size": len(hlink_plain),
                "links": 2,
                "content": [hlink_blob_id.hex()],
            },
            {
                # Second node of the hardlink pair.  Same inode → link()
                # is called instead of writing content a second time.
                "name": "hardlink_b",
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "atime": "2026-05-21T00:00:00Z",
                "ctime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "user": "test", "group": "test",
                "inode": 9001, "device_id": 0,
                "size": len(hlink_plain),
                "links": 2,
                "content": [hlink_blob_id.hex()],
            },
            {
                # File whose content ends with a zero run >= 4096 bytes.
                # Forces write_blob_sparse to lseek past the hole (lines
                # 254/255) and then fall off the bottom of the while loop
                # at line 263 — the line Issue #264 targets.
                "name": "trailing_zeros.bin",
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "atime": "2026-05-21T00:00:00Z",
                "ctime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "user": "test", "group": "test",
                "inode": 6, "device_id": 0,
                "size": len(trailing_zeros_plain),
                "links": 1,
                "content": [trailing_zeros_blob_id.hex()],
            },
        ]
    }
    tree_plain = json.dumps(tree_doc).encode()
    tree_blob_id = sha256(tree_plain)
    tree_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_TREE, tree_plain
    )

    # ── Broken tree: contains a file whose content references a blob
    # NOT in the index.  test_repo calls lcsas_tree_restore on this
    # blob and expects rc != 0 — exercises restore_file_node's
    # blob-not-in-index error branch in tree.c.
    broken_tree_doc = {
        "nodes": [
            {
                "name": "missing_content.txt",
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "size": 10,
                "content": ["f" * 64],   # valid hex, but not in index
            }
        ]
    }
    broken_tree_plain = json.dumps(broken_tree_doc).encode()
    broken_tree_blob_id = sha256(broken_tree_plain)
    broken_tree_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x0b" + b"\x00" * 15, broken_tree_plain
    )

    # Second broken tree: file content has a non-hex char (passes the
    # size check but fails hex_decode).
    bad_hex_tree_doc = {
        "nodes": [
            {
                "name": "bad_hex.txt",
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "size": 10,
                "content": ["g" * 64],   # 64 chars, but 'g' isn't hex
            }
        ]
    }
    bad_hex_tree_plain = json.dumps(bad_hex_tree_doc).encode()
    bad_hex_tree_blob_id = sha256(bad_hex_tree_plain)
    bad_hex_tree_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x0c" + b"\x00" * 15, bad_hex_tree_plain
    )

    # Missing-pack fictional blobs.  These index entries reference a
    # pack_id that does NOT exist on disk.  Used to exercise:
    #   - tree.c:157  (restore_file_node read_blob fail)
    #   - tree.c:201  (lcsas_tree_restore read_blob fail for tree blob)
    #   - repo.c:833-834 ("pack not found" diagnostic in read_blob)
    MISSING_PACK_ID = b"\xff" * 32
    MISSING_DATA_BLOB_ID = b"\xee" * 32
    MISSING_TREE_BLOB_ID = b"\xdd" * 32

    # Tree blob whose content references the missing data blob.  Real
    # encrypted tree (in the real pack), but its file node points at a
    # blob whose pack file is absent → restore_file_node hits 157.
    missing_content_tree_doc = {
        "nodes": [
            {
                "name": "needs_missing_blob.txt",
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "size": 100,
                "content": [MISSING_DATA_BLOB_ID.hex()],
            }
        ]
    }
    missing_content_tree_plain = json.dumps(missing_content_tree_doc).encode()
    missing_content_tree_blob_id = sha256(missing_content_tree_plain)
    missing_content_tree_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x15" + b"\x00" * 15, missing_content_tree_plain
    )

    # Third broken tree: a root tree whose dir node points at the
    # broken subtree above.  When lcsas_tree_restore recurses into the
    # subdir, the recursive call fails → goto out branch (tree.c ~282).
    bad_subdir_tree_doc = {
        "nodes": [
            {
                "name": "bad_subdir",
                "type": "dir",
                "mode": 493,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "subtree": broken_tree_blob_id.hex(),
            }
        ]
    }
    bad_subdir_tree_plain = json.dumps(bad_subdir_tree_doc).encode()
    bad_subdir_tree_blob_id = sha256(bad_subdir_tree_plain)
    bad_subdir_tree_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x0d" + b"\x00" * 15, bad_subdir_tree_plain
    )

    # Tree where "nodes" is a string instead of array — exercises
    # tree.c line 213 (nodes_arr type-check fails → rc=0 goto out).
    wrong_nodes_doc = {"nodes": "not-an-array"}
    wrong_nodes_plain = json.dumps(wrong_nodes_doc).encode()
    wrong_nodes_blob_id = sha256(wrong_nodes_plain)
    wrong_nodes_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x0f" + b"\x00" * 15, wrong_nodes_plain
    )

    # Tree with a node name > 1024 chars — exercises tree.c line 240
    # (lcsas_json_decode_string returns -1, continue).
    long_name_doc = {
        "nodes": [
            {
                "name": "a" * 2048,    # exceeds 1024-byte name_buf
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "size": 0,
                "content": [],
            }
        ]
    }
    long_name_plain = json.dumps(long_name_doc).encode()
    long_name_blob_id = sha256(long_name_plain)
    long_name_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x11" + b"\x00" * 15, long_name_plain
    )

    # Tree with a node type > 32 chars — exercises tree.c line 243.
    long_type_doc = {
        "nodes": [
            {
                "name": "ok_name.txt",
                "type": "y" * 64,    # exceeds 32-byte type_buf
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "size": 0,
                "content": [],
            }
        ]
    }
    long_type_plain = json.dumps(long_type_doc).encode()
    long_type_blob_id = sha256(long_type_plain)
    long_type_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x12" + b"\x00" * 15, long_type_plain
    )

    # ── Pack file: data + sub_tree + root_tree blobs + header + footer ──
    # Restic pack format (v1):
    #   [blob 1 ciphertext]...[encrypted header][4-byte LE header length]
    # Header is per-blob descriptors:
    #   type:1   (0=data, 1=tree, 2=data+compressed, 3=tree+compressed)
    #   length:4 (LE)
    #   id:32
    pack_body = (data_enc + sub_tree_enc + tree_enc
                 + broken_tree_enc + bad_hex_tree_enc + bad_subdir_tree_enc
                 + wrong_nodes_enc + long_name_enc + long_type_enc
                 + missing_content_tree_enc
                 + xattr_enc + hlink_enc + trailing_zeros_enc)
    off_data            = 0
    off_sub             = len(data_enc)
    off_tree            = off_sub + len(sub_tree_enc)
    off_broken          = off_tree + len(tree_enc)
    off_bad_hex         = off_broken + len(broken_tree_enc)
    off_bad_subdir      = off_bad_hex + len(bad_hex_tree_enc)
    off_wrong_nodes     = off_bad_subdir + len(bad_subdir_tree_enc)
    off_long_name       = off_wrong_nodes + len(wrong_nodes_enc)
    off_long_type       = off_long_name + len(long_name_enc)
    off_missing_content = off_long_type + len(long_type_enc)
    off_xattr           = off_missing_content + len(missing_content_tree_enc)
    off_hlink           = off_xattr + len(xattr_enc)
    off_trailing_zeros  = off_hlink + len(hlink_enc)
    offsets = {
        "data":             (off_data,            len(data_enc)),
        "sub":              (off_sub,             len(sub_tree_enc)),
        "tree":             (off_tree,            len(tree_enc)),
        "broken":           (off_broken,          len(broken_tree_enc)),
        "bad_hex":          (off_bad_hex,         len(bad_hex_tree_enc)),
        "bad_subdir":       (off_bad_subdir,      len(bad_subdir_tree_enc)),
        "wrong_nodes":      (off_wrong_nodes,     len(wrong_nodes_enc)),
        "long_name":        (off_long_name,       len(long_name_enc)),
        "long_type":        (off_long_type,       len(long_type_enc)),
        "missing_content":  (off_missing_content, len(missing_content_tree_enc)),
        "xattr":            (off_xattr,           len(xattr_enc)),
        "hlink":            (off_hlink,           len(hlink_enc)),
        "trailing_zeros":   (off_trailing_zeros,  len(trailing_zeros_enc)),
    }

    # Header: per-blob descriptors
    header = b""
    for blob_type, blob_id, (off, ln) in [
        (0, data_blob_id,            offsets["data"]),
        (1, sub_tree_blob_id,        offsets["sub"]),
        (1, tree_blob_id,            offsets["tree"]),
        (1, broken_tree_blob_id,     offsets["broken"]),
        (1, bad_hex_tree_blob_id,    offsets["bad_hex"]),
        (1, bad_subdir_tree_blob_id, offsets["bad_subdir"]),
        (1, wrong_nodes_blob_id,     offsets["wrong_nodes"]),
        (1, long_name_blob_id,       offsets["long_name"]),
        (1, long_type_blob_id,       offsets["long_type"]),
        (1, missing_content_tree_blob_id, offsets["missing_content"]),
        (0, xattr_blob_id,            offsets["xattr"]),
        (0, hlink_blob_id,            offsets["hlink"]),
        (0, trailing_zeros_blob_id,   offsets["trailing_zeros"]),
    ]:
        header += struct.pack("<BI", blob_type, ln) + blob_id
    header_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_HEADER, header
    )

    # Pack file
    pack_bytes = pack_body + header_enc + struct.pack("<I", len(header_enc))
    pack_id = sha256(pack_bytes)
    pack_id_hex = pack_id.hex()

    # Write pack file to data/XX/<hex>
    data_dir = repo_dir / "data" / pack_id_hex[:2]
    data_dir.mkdir(parents=True)
    (data_dir / pack_id_hex).write_bytes(pack_bytes)

    # ── Index file ────────────────────────────────────────────────
    # Restic index JSON: {"supersedes": [], "packs": [{"id": "...", "blobs": [...]}]}
    index_doc = {
        "supersedes": [],
        "packs": [
            {
                "id": pack_id_hex,
                "blobs": [
                    {
                        # Compressed data blob.  uncompressed_length hint
                        # included so repo.c read_blob takes the
                        # "loc->uncompressed_length > 0" branch.
                        "id": data_blob_id.hex(),
                        "type": "data",
                        "offset": offsets["data"][0],
                        "length": offsets["data"][1],
                        "uncompressed_length": len(data_plain),
                    },
                    {
                        "id": sub_tree_blob_id.hex(),
                        "type": "tree",
                        "offset": offsets["sub"][0],
                        "length": offsets["sub"][1],
                    },
                    {
                        "id": tree_blob_id.hex(),
                        "type": "tree",
                        "offset": offsets["tree"][0],
                        "length": offsets["tree"][1],
                    },
                    {
                        "id": broken_tree_blob_id.hex(),
                        "type": "tree",
                        "offset": offsets["broken"][0],
                        "length": offsets["broken"][1],
                    },
                    {
                        "id": bad_hex_tree_blob_id.hex(),
                        "type": "tree",
                        "offset": offsets["bad_hex"][0],
                        "length": offsets["bad_hex"][1],
                    },
                    {
                        "id": bad_subdir_tree_blob_id.hex(),
                        "type": "tree",
                        "offset": offsets["bad_subdir"][0],
                        "length": offsets["bad_subdir"][1],
                    },
                    {
                        "id": wrong_nodes_blob_id.hex(),
                        "type": "tree",
                        "offset": offsets["wrong_nodes"][0],
                        "length": offsets["wrong_nodes"][1],
                    },
                    {
                        "id": long_name_blob_id.hex(),
                        "type": "tree",
                        "offset": offsets["long_name"][0],
                        "length": offsets["long_name"][1],
                    },
                    {
                        "id": long_type_blob_id.hex(),
                        "type": "tree",
                        "offset": offsets["long_type"][0],
                        "length": offsets["long_type"][1],
                    },
                    {
                        "id": missing_content_tree_blob_id.hex(),
                        "type": "tree",
                        "offset": offsets["missing_content"][0],
                        "length": offsets["missing_content"][1],
                    },
                    {
                        # Xattr content blob — referenced by xattr_test_file
                        # node.  uncompressed_length included so read_blob
                        # takes the "loc->uncompressed_length > 0" branch.
                        "id": xattr_blob_id.hex(),
                        "type": "data",
                        "offset": offsets["xattr"][0],
                        "length": offsets["xattr"][1],
                        "uncompressed_length": len(xattr_plain),
                    },
                    {
                        # Hardlink content blob — referenced by hardlink_a
                        # and hardlink_b nodes (same inode 9001).
                        "id": hlink_blob_id.hex(),
                        "type": "data",
                        "offset": offsets["hlink"][0],
                        "length": offsets["hlink"][1],
                        "uncompressed_length": len(hlink_plain),
                    },
                    {
                        # Trailing-zeros data blob (Issue #264).
                        # No uncompressed_length so repo.c takes the
                        # probe-size branch (exercises a different read_blob
                        # path than the zstd-compressed data blob above).
                        "id": trailing_zeros_blob_id.hex(),
                        "type": "data",
                        "offset": offsets["trailing_zeros"][0],
                        "length": offsets["trailing_zeros"][1],
                    },
                ],
            },
            {
                # Fictional pack — does NOT exist on disk.  The two
                # blob entries below reference this pack so read_blob
                # can never find them.  Used to exercise tree.c:157,
                # tree.c:201, and repo.c:833-834.
                "id": MISSING_PACK_ID.hex(),
                "blobs": [
                    {
                        "id": MISSING_DATA_BLOB_ID.hex(),
                        "type": "data",
                        "offset": 0,
                        "length": 100,
                    },
                    {
                        "id": MISSING_TREE_BLOB_ID.hex(),
                        "type": "tree",
                        "offset": 100,
                        "length": 200,
                    },
                ],
            }
        ],
    }
    # v2-zstd format: prefix byte (0x02) || zstd-compressed JSON.
    # This exercises the v2-prefix-strip + zstd-decompress path in repo.c.
    import zstandard
    index_plain = json.dumps(index_doc).encode()
    index_zstd = zstandard.ZstdCompressor().compress(index_plain)
    index_v2 = b"\x02" + index_zstd
    index_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_INDEX, index_v2
    )
    index_id = sha256(index_enc)
    index_dir = repo_dir / "index"
    index_dir.mkdir()
    (index_dir / index_id.hex()).write_bytes(index_enc)

    # Second "old" index file — superseded by the new one above.
    # Exercises the supersedes-dedup branch in lcsas_repo_load_index
    # (repo.c lines 491-512).  The blob list contains a phantom blob
    # that would conflict if not dropped.
    old_index_doc = {
        "supersedes": [],
        "packs": [
            {
                "id": "00" * 32,
                "blobs": [
                    {
                        "id": "ff" * 32,
                        "type": "data",
                        "offset": 0,
                        "length": 100,
                    }
                ],
            }
        ],
    }
    old_index_plain = json.dumps(old_index_doc).encode()
    # Use v1 (no prefix) for the old file, to also exercise that branch.
    old_index_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x07" + b"\x00" * 15, old_index_plain
    )
    old_index_id = sha256(old_index_enc)
    (index_dir / old_index_id.hex()).write_bytes(old_index_enc)

    # Now write a THIRD index file whose `supersedes` lists the old one.
    # Make this one minimal — supersedes the dead index. The dead index
    # blobs should NOT appear in the merged blob index.
    new_index_doc = {
        "supersedes": [old_index_id.hex()],
        "packs": [],
    }
    new_index_plain = json.dumps(new_index_doc).encode()
    new_index_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x08" + b"\x00" * 15, new_index_plain
    )
    new_index_id = sha256(new_index_enc)
    (index_dir / new_index_id.hex()).write_bytes(new_index_enc)

    # FOURTH index file: v2-zstd format with a CORRUPTED zstd frame.
    # The decrypted payload starts with 0x02 + ZSTD_MAGIC + garbage,
    # so strip_v2 detects zstd and lcsas_zstd_decode(probe) returns -1.
    # Exercises repo.c lines 339-347 (zstd frame error path in
    # decrypt_repo_file). Result: decrypt_repo_file returns NULL and
    # load_index skips this file (graceful).
    bad_zstd_payload = b"\x02" + b"\x28\xb5\x2f\xfd" + b"\xff" * 64
    bad_zstd_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x09" + b"\x00" * 15, bad_zstd_payload
    )
    bad_zstd_id = sha256(bad_zstd_enc)
    (index_dir / bad_zstd_id.hex()).write_bytes(bad_zstd_enc)

    # FIFTH index file: v2-plain format (prefix byte 0x01 followed by
    # plain JSON, NOT zstd-compressed).  Exercises repo.c lines
    # 258-261 in lcsas_repo_strip_v2_prefix — the branch that strips
    # the single prefix byte without invoking zstd_decode.
    v2plain_doc = {
        "supersedes": [],
        "packs": [],
    }
    v2plain_payload = b"\x01" + json.dumps(v2plain_doc).encode()
    v2plain_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x13" + b"\x00" * 15, v2plain_payload
    )
    v2plain_id = sha256(v2plain_enc)
    (index_dir / v2plain_id.hex()).write_bytes(v2plain_enc)

    # SIXTH index file: a malformed pack_id (non-hex chars).
    # Exercises repo.c line 598 (lcsas_hex_decode failure on pack_id
    # → continue).  The packs[] entry is structurally valid JSON but
    # the id field contains "g" which is not a hex digit.
    bad_pack_id_doc = {
        "supersedes": [],
        "packs": [
            {
                "id": "g" * 64,    # 64 chars, non-hex
                "blobs": [],
            }
        ],
    }
    bad_pack_id_plain = json.dumps(bad_pack_id_doc).encode()
    bad_pack_id_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x14" + b"\x00" * 15, bad_pack_id_plain
    )
    bad_pack_id_id = sha256(bad_pack_id_enc)
    (index_dir / bad_pack_id_id.hex()).write_bytes(bad_pack_id_enc)

    # Make broken-tree IDs available via globals so main() can stuff
    # them into the manifest.
    global BROKEN_TREE_ID, BAD_HEX_TREE_ID, BAD_SUBDIR_TREE_ID
    global WRONG_NODES_ID, LONG_NAME_ID, LONG_TYPE_ID
    global MISSING_CONTENT_TREE_ID, MISSING_TREE_ID
    global XATTR_BLOB_ID, HLINK_BLOB_ID, TRAILING_ZEROS_BLOB_ID
    BROKEN_TREE_ID = broken_tree_blob_id.hex()
    BAD_HEX_TREE_ID = bad_hex_tree_blob_id.hex()
    BAD_SUBDIR_TREE_ID = bad_subdir_tree_blob_id.hex()
    WRONG_NODES_ID = wrong_nodes_blob_id.hex()
    LONG_NAME_ID = long_name_blob_id.hex()
    LONG_TYPE_ID = long_type_blob_id.hex()
    MISSING_CONTENT_TREE_ID = missing_content_tree_blob_id.hex()
    MISSING_TREE_ID = MISSING_TREE_BLOB_ID.hex()
    XATTR_BLOB_ID = xattr_blob_id.hex()
    HLINK_BLOB_ID = hlink_blob_id.hex()
    TRAILING_ZEROS_BLOB_ID = trailing_zeros_blob_id.hex()

    return pack_id_hex, data_blob_id.hex(), tree_blob_id.hex(), tree_blob_id.hex()


BROKEN_TREE_ID = ""
BAD_HEX_TREE_ID = ""
BAD_SUBDIR_TREE_ID = ""
BROKEN_SNAP_ID = ""
WRONG_NODES_ID = ""
LONG_NAME_ID = ""
LONG_TYPE_ID = ""
MISSING_CONTENT_TREE_ID = ""
MISSING_TREE_ID = ""
XATTR_BLOB_ID = ""
HLINK_BLOB_ID = ""
TRAILING_ZEROS_BLOB_ID = ""


def make_snapshot(repo_dir: Path, tree_id_hex: str,
                  broken_tree_hex: str = "") -> str:
    """Write two snapshot files (different timestamps) so the snapshot
    sort loop runs at least one swap.  Optionally also writes a
    "broken" snapshot pointing at a tree that fails restore.

    Returns the latest GOOD snapshot's hex id."""
    snap_dir = repo_dir / "snapshots"
    snap_dir.mkdir()

    if broken_tree_hex:
        # Snapshot pointing at the broken tree — used by tests that
        # exercise main.c's "tree restore failed" path under --target.
        broken_doc = {
            "time": "2026-04-01T00:00:00Z",
            "tree": broken_tree_hex,
            "paths": ["/will-fail"],
            "hostname": "test",
            "username": "test",
            "uid": 1000, "gid": 1000,
            "tags": [],
            "id": "1" * 64,
        }
        broken_plain = json.dumps(broken_doc).encode()
        broken_enc = encrypt_authenticated(
            MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
            b"\x0e" + b"\x00" * 15, broken_plain
        )
        broken_id = sha256(broken_enc)
        (snap_dir / broken_id.hex()).write_bytes(broken_enc)
        # Persist for manifest.
        global BROKEN_SNAP_ID
        BROKEN_SNAP_ID = broken_id.hex()

    # OLDER snapshot — same tree, earlier timestamp.  After sort it
    # should appear FIRST (ascending by time string).
    old_doc = {
        "time": "2025-01-01T00:00:00Z",
        "tree": tree_id_hex,
        "paths": ["/test-old"],
        "hostname": "test",
        "username": "test",
        "uid": 1000, "gid": 1000,
        "tags": [],
        "id": "0" * 64,
    }
    old_plain = json.dumps(old_doc).encode()
    old_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R,
        b"\x0a" + b"\x00" * 15, old_plain
    )
    old_id = sha256(old_enc)
    (snap_dir / old_id.hex()).write_bytes(old_enc)

    # NEWER snapshot — pointed at the same tree.  After sort it should
    # appear LAST.  We write this one SECOND in directory order
    # arbitrarily; depending on hash collision order in readdir, the
    # sort loop's swap branch will fire.
    new_doc = {
        "time": "2026-05-21T00:00:00Z",
        "tree": tree_id_hex,
        "paths": ["/test"],
        "hostname": "test",
        "username": "test",
        "uid": 1000, "gid": 1000,
        "tags": [],
        "id": "0" * 64,
    }
    new_plain = json.dumps(new_doc).encode()
    new_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_SNAP, new_plain
    )
    new_id = sha256(new_enc)
    (snap_dir / new_id.hex()).write_bytes(new_enc)
    return new_id.hex()


def make_stress_fixture(
    repo_dir: Path,
    n_orphans: int,
    n_files: int,
    n_subdirs: int,
) -> tuple[str, str, str]:
    """Build a stress-test pack + index covering scaling characterisation.

    Layout:
      - 1 real data blob (small payload "hello\\n")
      - n_files file nodes split across n_subdirs sub-tree blobs, each
        file content references the real data blob
      - 1 root tree with n_subdirs dir nodes pointing at the sub-trees
      - 1 encrypted index file containing the real blob descriptors AND
        n_orphans random-id orphan blob entries (never referenced from
        any tree — they inflate the index for lookup-time scaling stress)

    Returns (pack_id_hex, data_blob_id_hex, root_tree_id_hex).

    Notes on token-budget safety: the tree.c JSON parser allocates 65536
    tokens.  Each file node uses ~15-20 tokens, so each sub-tree should
    hold no more than ~3000 files.  Caller should pick n_subdirs >=
    n_files/3000.
    """
    import os as _os
    import zstandard

    if n_files > 0 and n_subdirs < 1:
        raise ValueError("n_subdirs must be >= 1 when n_files > 0")

    # ── Single real data blob ─────────────────────────────────────
    data_plain = b"hello from petabyte stress fixture\n"
    data_blob_id = sha256(data_plain)
    data_compressed = zstandard.ZstdCompressor().compress(data_plain)
    data_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_DATA, data_compressed
    )

    # ── Sub-tree blobs ────────────────────────────────────────────
    sub_tree_records = []  # list of (blob_id, encrypted_bytes)
    files_per_subdir = (n_files + max(n_subdirs, 1) - 1) // max(n_subdirs, 1)
    for s in range(n_subdirs):
        start = s * files_per_subdir
        end = min(start + files_per_subdir, n_files)
        if start >= end:
            break
        nodes = []
        for i in range(start, end):
            nodes.append({
                "name": "file_{:06d}.txt".format(i),
                "type": "file",
                "mode": 420,
                "mtime": "2026-05-21T00:00:00Z",
                "uid": 1000, "gid": 1000,
                "size": len(data_plain),
                "content": [data_blob_id.hex()],
            })
        sub_doc = {"nodes": nodes}
        sub_plain = json.dumps(sub_doc).encode()
        sub_blob_id = sha256(sub_plain)
        sub_compressed = zstandard.ZstdCompressor().compress(sub_plain)
        sub_iv = bytes([0x10 + (s & 0xEF)]) + b"\x00" * 15
        sub_enc = encrypt_authenticated(
            MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, sub_iv, sub_compressed
        )
        sub_tree_records.append((sub_blob_id, sub_enc))

    # ── Root tree ─────────────────────────────────────────────────
    root_nodes = []
    for s, (sub_blob_id, _) in enumerate(sub_tree_records):
        root_nodes.append({
            "name": "dir_{:03d}".format(s),
            "type": "dir",
            "mode": 493,
            "mtime": "2026-05-21T00:00:00Z",
            "uid": 1000, "gid": 1000,
            "subtree": sub_blob_id.hex(),
        })
    root_doc = {"nodes": root_nodes}
    root_plain = json.dumps(root_doc).encode()
    root_blob_id = sha256(root_plain)
    root_compressed = zstandard.ZstdCompressor().compress(root_plain)
    root_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_TREE, root_compressed
    )

    # ── Assemble pack body ────────────────────────────────────────
    blob_offsets = []  # list of (offset, length, blob_id, type)
    pack_body = b""
    blob_offsets.append((0, len(data_enc), data_blob_id, 0))
    pack_body += data_enc
    for sub_blob_id, sub_enc in sub_tree_records:
        blob_offsets.append((len(pack_body), len(sub_enc), sub_blob_id, 1))
        pack_body += sub_enc
    blob_offsets.append((len(pack_body), len(root_enc), root_blob_id, 1))
    pack_body += root_enc

    # Pack header: per-blob descriptors
    header = b""
    for off, ln, blob_id, blob_type in blob_offsets:
        header += struct.pack("<BI", blob_type, ln) + blob_id
    header_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_HEADER, header
    )
    pack_bytes = pack_body + header_enc + struct.pack("<I", len(header_enc))
    pack_id = sha256(pack_bytes)
    pack_id_hex = pack_id.hex()

    data_dir = repo_dir / "data" / pack_id_hex[:2]
    data_dir.mkdir(parents=True)
    (data_dir / pack_id_hex).write_bytes(pack_bytes)

    # ── Index files ───────────────────────────────────────────────
    # The C JSON parser allocates a fixed token buffer per index file
    # (32768 tokens in lcsas_repo_load_index → ~3500 blob entries max
    # per file).  Split orphans across multiple index files; the loader
    # already merges them.
    index_dir = repo_dir / "index"
    index_dir.mkdir()

    # First index file: all the real blob descriptors (small).
    real_blobs_json = []
    for off, ln, blob_id, blob_type in blob_offsets:
        entry = {
            "id": blob_id.hex(),
            "type": "data" if blob_type == 0 else "tree",
            "offset": off,
            "length": ln,
        }
        if blob_type == 0:
            entry["uncompressed_length"] = len(data_plain)
        real_blobs_json.append(entry)

    def _write_index(blobs_list: list, iv_byte: int) -> None:
        idx_doc = {
            "supersedes": [],
            "packs": [{"id": pack_id_hex, "blobs": blobs_list}],
        }
        idx_plain = json.dumps(idx_doc).encode()
        idx_zstd = zstandard.ZstdCompressor().compress(idx_plain)
        idx_v2 = b"\x02" + idx_zstd
        iv = bytes([iv_byte & 0xFF]) + b"\x00" * 15
        idx_enc = encrypt_authenticated(
            MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, iv, idx_v2
        )
        idx_id = sha256(idx_enc)
        (index_dir / idx_id.hex()).write_bytes(idx_enc)

    _write_index(real_blobs_json, 0x01)

    # Orphan entries: split across ceil(n_orphans / ORPHANS_PER_FILE)
    # additional index files.  3000 entries per file stays comfortably
    # under the 32k-token JSON-parser ceiling in lcsas_repo_load_index.
    ORPHANS_PER_FILE = 3000
    if n_orphans > 0:
        remaining = n_orphans
        file_idx = 0
        while remaining > 0:
            chunk = min(remaining, ORPHANS_PER_FILE)
            chunk_blobs = []
            for _ in range(chunk):
                chunk_blobs.append({
                    "id": _os.urandom(32).hex(),
                    "type": "data",
                    "offset": 0,
                    "length": len(data_enc),
                })
            _write_index(chunk_blobs, 0x40 + (file_idx & 0xBF))
            file_idx += 1
            remaining -= chunk

    return pack_id_hex, data_blob_id.hex(), root_blob_id.hex()


def make_stress_snapshot(repo_dir: Path, tree_id_hex: str) -> str:
    """Write a single snapshot for stress mode (no sort-loop testing)."""
    snap_doc = {
        "time": "2026-05-21T00:00:00Z",
        "tree": tree_id_hex,
        "paths": ["/stress"],
        "hostname": "test",
        "username": "test",
        "uid": 1000, "gid": 1000,
        "tags": [],
        "id": "0" * 64,
    }
    snap_plain = json.dumps(snap_doc).encode()
    snap_enc = encrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, IV_SNAP, snap_plain
    )
    snap_id = sha256(snap_enc)
    snap_dir = repo_dir / "snapshots"
    snap_dir.mkdir()
    (snap_dir / snap_id.hex()).write_bytes(snap_enc)
    return snap_id.hex()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("out", type=Path, help="output repo directory")
    p.add_argument("--clean", action="store_true",
                    help="delete out_dir if it exists")
    p.add_argument("--stress", nargs=3, metavar=("N_ORPHANS", "N_FILES", "N_SUBDIRS"),
                    type=int, default=None,
                    help="generate stress-test fixture instead of default. "
                         "N_ORPHANS = orphan blob index entries; "
                         "N_FILES = real files in restored tree; "
                         "N_SUBDIRS = sub-tree blobs (N_FILES / N_SUBDIRS "
                         "must be < ~3000 due to JSON token budget)")
    args = p.parse_args()

    if args.out.exists():
        if args.clean:
            shutil.rmtree(args.out)
        else:
            print(f"ERROR: {args.out} exists (use --clean to wipe)", file=sys.stderr)
            return 1
    args.out.mkdir(parents=True)

    # keys/ — one real key file in stress mode (no stub-key sort
    # exercises wanted), or the full multi-key setup in default mode.
    keys_dir = args.out / "keys"
    keys_dir.mkdir()
    key_id = hashlib.sha256(SALT_KEY + b"k0").hexdigest()
    make_key_file(keys_dir / key_id)

    if args.stress is None:
        # Default mode keeps the multi-key sort-loop exercise.
        stub_key_id = "0" * 64
        (keys_dir / stub_key_id).write_text(json.dumps({
            "created": "2026-05-21T00:00:00Z",
            "kdf": "scrypt",
            "N": N, "r": R, "p": P,
            "salt": base64.b64encode(SALT_KEY).decode(),
            "data": base64.b64encode(b"\x00" * 64).decode(),
        }))
        middle_stub_id = "7" + "0" * 63
        (keys_dir / middle_stub_id).write_text(json.dumps({
            "created": "2026-05-21T00:00:00Z",
            "kdf": "scrypt",
            "N": N, "r": R, "p": P,
            "salt": base64.b64encode(SALT_KEY).decode(),
            "data": base64.b64encode(b"\x00" * 64).decode(),
        }))

    if args.stress is not None:
        n_orphans, n_files, n_subdirs = args.stress
        pack_id, data_blob_id, tree_blob_id = make_stress_fixture(
            args.out, n_orphans, n_files, n_subdirs
        )
        snap_id = make_stress_snapshot(args.out, tree_blob_id)
        manifest = {
            "password": PASSWORD.decode(),
            "key_file": key_id,
            "pack_id": pack_id,
            "data_blob_id": data_blob_id,
            "tree_blob_id": tree_blob_id,
            "snapshot_id": snap_id,
            "n_orphan_blobs": n_orphans,
            "n_files": n_files,
            "n_subdirs": n_subdirs,
            "stress": True,
            "master_encrypt_hex": MASTER_ENCRYPT.hex(),
            "master_mac_k_hex": MASTER_MAC_K.hex(),
            "master_mac_r_hex": MASTER_MAC_R.hex(),
        }
    else:
        pack_id, data_blob_id, tree_blob_id, _snap_tree = make_pack_and_index(args.out)
        snap_id = make_snapshot(args.out, tree_blob_id, BROKEN_TREE_ID)
        manifest = {
            "password": PASSWORD.decode(),
            "key_file": key_id,
            "pack_id": pack_id,
            "data_blob_id": data_blob_id,
            "tree_blob_id": tree_blob_id,
            "broken_tree_id": BROKEN_TREE_ID,
            "bad_hex_tree_id": BAD_HEX_TREE_ID,
            "bad_subdir_tree_id": BAD_SUBDIR_TREE_ID,
            "wrong_nodes_tree_id": WRONG_NODES_ID,
            "long_name_tree_id": LONG_NAME_ID,
            "long_type_tree_id": LONG_TYPE_ID,
            "missing_content_tree_id": MISSING_CONTENT_TREE_ID,
            "missing_tree_id": MISSING_TREE_ID,
            "xattr_blob_id": XATTR_BLOB_ID,
            "hlink_blob_id": HLINK_BLOB_ID,
            "trailing_zeros_blob_id": TRAILING_ZEROS_BLOB_ID,
            "snapshot_id": snap_id,
            "broken_snapshot_id": BROKEN_SNAP_ID,
            "master_encrypt_hex": MASTER_ENCRYPT.hex(),
            "master_mac_k_hex": MASTER_MAC_K.hex(),
            "master_mac_r_hex": MASTER_MAC_R.hex(),
        }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"Fixture written to {args.out}")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
