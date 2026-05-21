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

    # ── Pack file: data + sub_tree + root_tree blobs + header + footer ──
    # Restic pack format (v1):
    #   [blob 1 ciphertext]...[encrypted header][4-byte LE header length]
    # Header is per-blob descriptors:
    #   type:1   (0=data, 1=tree, 2=data+compressed, 3=tree+compressed)
    #   length:4 (LE)
    #   id:32
    pack_body = (data_enc + sub_tree_enc + tree_enc
                 + broken_tree_enc + bad_hex_tree_enc + bad_subdir_tree_enc)
    off_data       = 0
    off_sub        = len(data_enc)
    off_tree       = off_sub + len(sub_tree_enc)
    off_broken     = off_tree + len(tree_enc)
    off_bad_hex    = off_broken + len(broken_tree_enc)
    off_bad_subdir = off_bad_hex + len(bad_hex_tree_enc)
    offsets = {
        "data":       (off_data,       len(data_enc)),
        "sub":        (off_sub,        len(sub_tree_enc)),
        "tree":       (off_tree,       len(tree_enc)),
        "broken":     (off_broken,     len(broken_tree_enc)),
        "bad_hex":    (off_bad_hex,    len(bad_hex_tree_enc)),
        "bad_subdir": (off_bad_subdir, len(bad_subdir_tree_enc)),
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

    # Make broken-tree IDs available via globals so main() can stuff
    # them into the manifest.
    global BROKEN_TREE_ID, BAD_HEX_TREE_ID, BAD_SUBDIR_TREE_ID
    BROKEN_TREE_ID = broken_tree_blob_id.hex()
    BAD_HEX_TREE_ID = bad_hex_tree_blob_id.hex()
    BAD_SUBDIR_TREE_ID = bad_subdir_tree_blob_id.hex()

    return pack_id_hex, data_blob_id.hex(), tree_blob_id.hex(), tree_blob_id.hex()


BROKEN_TREE_ID = ""
BAD_HEX_TREE_ID = ""
BAD_SUBDIR_TREE_ID = ""


def make_snapshot(repo_dir: Path, tree_id_hex: str) -> str:
    """Write two snapshot files (different timestamps) so the snapshot
    sort loop runs at least one swap.  Returns the latest's hex id."""
    snap_dir = repo_dir / "snapshots"
    snap_dir.mkdir()

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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("out", type=Path, help="output repo directory")
    p.add_argument("--clean", action="store_true",
                    help="delete out_dir if it exists")
    args = p.parse_args()

    if args.out.exists():
        if args.clean:
            shutil.rmtree(args.out)
        else:
            print(f"ERROR: {args.out} exists (use --clean to wipe)", file=sys.stderr)
            return 1
    args.out.mkdir(parents=True)

    # keys/ — multiple key files.  The first is a stub that won't decrypt
    # (forces the loader to try the next one).  The second is the real
    # one.  Filenames are crafted so the stub sorts FIRST and the real
    # key sorts SECOND, exercising the realloc + sort loop in
    # lcsas_repo_load_keys_dir.
    keys_dir = args.out / "keys"
    keys_dir.mkdir()
    # Stub key: a file that decrypts-FAIL (MAC mismatch).  The filename
    # starts with "0000..." so it sorts ahead of the real key.
    stub_key_id = "0" * 64
    (keys_dir / stub_key_id).write_text(json.dumps({
        "created": "2026-05-21T00:00:00Z",
        "kdf": "scrypt",
        "N": N, "r": R, "p": P,
        "salt": base64.b64encode(SALT_KEY).decode(),
        # Garbage data that won't decrypt.
        "data": base64.b64encode(b"\x00" * 64).decode(),
    }))
    # Real key.  Filename starts with "eb15..." (sorts after "0000...").
    key_id = hashlib.sha256(SALT_KEY + b"k0").hexdigest()
    make_key_file(keys_dir / key_id)
    # A third stub between them to force at least one realloc-growth
    # iteration in the sort loop swap branch.
    middle_stub_id = "7" + "0" * 63
    (keys_dir / middle_stub_id).write_text(json.dumps({
        "created": "2026-05-21T00:00:00Z",
        "kdf": "scrypt",
        "N": N, "r": R, "p": P,
        "salt": base64.b64encode(SALT_KEY).decode(),
        "data": base64.b64encode(b"\x00" * 64).decode(),
    }))

    # pack + index
    pack_id, data_blob_id, tree_blob_id, _snap_tree = make_pack_and_index(args.out)

    # snapshot
    snap_id = make_snapshot(args.out, tree_blob_id)

    # Manifest (for test consumption)
    manifest = {
        "password": PASSWORD.decode(),
        "key_file": key_id,
        "pack_id": pack_id,
        "data_blob_id": data_blob_id,
        "tree_blob_id": tree_blob_id,
        "broken_tree_id": BROKEN_TREE_ID,
        "bad_hex_tree_id": BAD_HEX_TREE_ID,
        "bad_subdir_tree_id": BAD_SUBDIR_TREE_ID,
        "snapshot_id": snap_id,
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
