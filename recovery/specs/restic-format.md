# Restic Repository Format Specification

> Bundled with LCSAS archive volumes for long-term survivability.
> This document enables a future programmer to write a compatible
> decoder if the `rustic` and `restic` binaries are no longer available.
>
> Sources: [restic design documentation](https://restic.readthedocs.io/en/latest/100_references.html),
> [restic source code](https://github.com/restic/restic) (BSD-2-Clause license).
>
> Last updated: 2026-02-21

---

## 1. Overview

A restic repository stores deduplicated, encrypted backup data.  Files
are split into variable-length **blobs** (using content-defined chunking),
which are grouped into **pack files**.  The repository is entirely
self-contained — given the repository directory and the correct
password, all data can be recovered.

LCSAS archives distribute repository data across optical discs but
preserve the repository structure exactly.  Each disc contains:
- Pack files in `data/` (the encrypted blobs)
- Repository metadata in `metadata/<repo_id>/` (index, snapshots, keys, config)

---

## 2. Repository Directory Structure

```
repository/
├── config                    # Repository configuration (encrypted JSON)
├── keys/                     # Password-protected master key files
│   └── <key_id>              # JSON: encrypted master key
├── index/                    # Pack-to-blob mapping files
│   └── <index_id>            # Encrypted JSON: lists all blobs in packs
├── snapshots/                # Snapshot manifest files
│   └── <snapshot_id>         # Encrypted JSON: backup metadata
├── data/                     # Pack files containing encrypted blobs
│   ├── 00/                   # Two-level hex prefix subdirectories
│   │   ├── 00aabbccdd...     # Pack file (SHA-256 hash = filename)
│   │   └── ...
│   ├── 01/
│   └── ...
└── locks/                    # Advisory lock files (not archived)
```

---

## 3. Encryption

### 3.1 Key Derivation

The user provides a password (or password file).  Key derivation uses
**scrypt** (RFC 7914) to derive a 64-byte key from the password:

```
derived_key = scrypt(password, salt, N=2^15, r=8, p=1, dkLen=64)
```

- First 32 bytes → AES-256 encryption key
- Last 32 bytes → HMAC-SHA-256 authentication key

These derived keys decrypt the **master key** stored in the key file.
The master key is then used for all subsequent encryption/decryption.

### 3.2 Key File Format

Each file in `keys/` is a JSON document:

```json
{
  "created": "2026-01-15T10:30:00.000000000Z",
  "username": "user",
  "hostname": "host",
  "kdf": "scrypt",
  "N": 32768,
  "r": 8,
  "p": 1,
  "salt": "<base64-encoded 64 bytes>",
  "data": "<base64-encoded encrypted master key>"
}
```

The `data` field contains the master key, encrypted with the
password-derived key using AES-256-CTR + Poly1305-AES MAC.

### 3.3 Master Key

The decrypted master key JSON contains:

```json
{
  "encrypt": "<base64, 32 bytes — AES-256 key>",
  "mac": {
    "k": "<base64, 16 bytes — Poly1305 key r>",
    "r": "<base64, 16 bytes — Poly1305 key s>"
  }
}
```

This master key encrypts/decrypts all other repository data:
config, snapshots, index files, and pack file blobs.

### 3.4 Authenticated Encryption

All encrypted data uses the same scheme:

1. Generate a random 16-byte IV (initialization vector)
2. Encrypt plaintext with **AES-256-CTR** using the master `encrypt` key
3. Compute **Poly1305-AES** MAC over the ciphertext
4. Output: `IV (16 bytes) || ciphertext || MAC (16 bytes)`

Total overhead per encrypted blob: 32 bytes (16 IV + 16 MAC).

---

## 4. Pack File Format

Pack files contain one or more encrypted blobs concatenated together,
followed by a header that describes the contents.

### 4.1 Binary Layout

```
┌─────────────────────────────────────────────────┐
│ Encrypted Blob 1                                │
│ (IV ∥ ciphertext ∥ MAC)                         │
├─────────────────────────────────────────────────┤
│ Encrypted Blob 2                                │
├─────────────────────────────────────────────────┤
│ ...                                             │
├─────────────────────────────────────────────────┤
│ Encrypted Blob N                                │
├─────────────────────────────────────────────────┤
│ Encrypted Header                                │
│ (IV ∥ encrypted header entries ∥ MAC)           │
├─────────────────────────────────────────────────┤
│ Header Length (4 bytes, little-endian uint32)    │
└─────────────────────────────────────────────────┘
```

### 4.2 Reading a Pack File

1. Read the last 4 bytes → `header_length` (little-endian uint32)
2. Read `header_length` bytes before those 4 bytes → encrypted header
3. Decrypt the header using the master key
4. Parse the plaintext header as a sequence of entries

### 4.3 Header Entry Format

Each header entry is 37 bytes:

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 1 | Type | `0` = data blob, `1` = tree blob |
| 1 | 4 | Length | Compressed, encrypted blob size (uint32 LE) |
| 5 | 32 | ID | SHA-256 hash of the plaintext blob content |

The entries are listed in the same order as the blobs appear in the
pack file.  The offset of each blob can be computed by summing the
lengths of all preceding blobs.

### 4.4 Blob Types

- **Data blob** (type 0): A chunk of file content.  After decryption
  and decompression, this is a raw byte range from a file.
- **Tree blob** (type 1): A JSON document describing a directory tree
  node (files, subdirectories, metadata).

### 4.5 Compression

Restic 0.14+ / Rustic support optional **zstd** compression.  When
compression is enabled:

- Blobs are compressed with zstd before encryption
- The repository `config` file indicates the compression mode
- After decrypting a blob, check if it starts with the zstd magic
  bytes (`0x28 0xB5 0x2F 0xFD`); if so, decompress with zstd

---

## 5. Index Files

Each file in `index/` is an encrypted JSON document.  Decrypted, it
maps blob IDs to their pack file locations:

```json
{
  "supersedes": ["<index_id>", ...],
  "packs": [
    {
      "id": "<pack_sha256_hex>",
      "blobs": [
        {
          "id": "<blob_sha256_hex>",
          "type": "data",
          "offset": 0,
          "length": 4096,
          "uncompressed_length": 8192
        },
        ...
      ]
    },
    ...
  ]
}
```

- `id`: SHA-256 of the decrypted, uncompressed blob content
- `type`: `"data"` or `"tree"`
- `offset`: byte position within the pack file
- `length`: encrypted blob size (including IV + MAC overhead)
- `uncompressed_length`: present if compression is used

Index files marked by `supersedes` replace older index files.

---

## 6. Snapshot Files

Each file in `snapshots/` is an encrypted JSON document.  Decrypted:

```json
{
  "time": "2026-01-15T10:30:00.000000000Z",
  "parent": "<parent_snapshot_id>",
  "tree": "<tree_blob_id>",
  "paths": ["/home/user/photos"],
  "hostname": "nas",
  "username": "root",
  "uid": 0,
  "gid": 0,
  "tags": ["family", "photos"],
  "program_version": "rustic 0.9.1"
}
```

The `tree` field is the SHA-256 ID of the root **tree blob**.  To
restore a snapshot:

1. Decrypt the snapshot JSON → get the root `tree` blob ID
2. Look up the `tree` blob in the index → find its pack file + offset
3. Read and decrypt the tree blob from the pack file
4. The tree blob JSON lists files (data blob references) and
   subdirectories (more tree blob references)
5. Recursively resolve all tree blobs
6. For each file: concatenate and decrypt its data blobs in order

---

## 7. Tree Blob Format

A decrypted tree blob is a JSON document describing a directory:

```json
{
  "nodes": [
    {
      "name": "photo.jpg",
      "type": "file",
      "mode": 420,
      "mtime": "2025-12-25T08:00:00.000000000Z",
      "atime": "2026-01-15T10:00:00.000000000Z",
      "ctime": "2025-12-25T08:00:00.000000000Z",
      "uid": 1000,
      "gid": 1000,
      "user": "user",
      "group": "user",
      "inode": 12345678,
      "device_id": 64769,
      "size": 5242880,
      "links": 1,
      "content": [
        "<data_blob_id_1>",
        "<data_blob_id_2>"
      ]
    },
    {
      "name": "subdir",
      "type": "dir",
      "subtree": "<tree_blob_id>"
    }
  ]
}
```

- `type`: `"file"`, `"dir"`, `"symlink"`, `"dev"`, `"chardev"`, `"fifo"`, `"socket"`
- `content`: ordered list of data blob IDs whose concatenation produces the file
- `subtree`: tree blob ID for subdirectory contents

---

## 8. Config File

The `config` file at the repository root is encrypted with the master
key.  Decrypted:

```json
{
  "version": 2,
  "id": "<repository_id_hex>",
  "chunker_polynomial": "<hex>"
}
```

- `version`: repository format version (1 or 2)
- `id`: unique identifier for this repository
- `chunker_polynomial`: Rabin fingerprint polynomial for CDC

---

## 9. Content-Defined Chunking (CDC)

Files are split into variable-length chunks using a **Rabin
fingerprint** rolling hash:

- Minimum chunk size: 512 KiB
- Maximum chunk size: 8 MiB
- Average chunk size: ~1 MiB (determined by the polynomial)
- Chunk boundaries are determined by the rolling hash matching a
  specific bit pattern

This ensures that insertions/deletions in a file only affect nearby
chunks, enabling efficient deduplication across backups.

---

## 10. Restore Procedure (Manual)

If no restic/rustic binary is available, a programmer can restore data
by implementing these steps:

1. **Parse a key file** (`keys/<id>`):
   - Read the JSON
   - Derive the key-encryption-key from the password using scrypt
     with the parameters in the key file (N, r, p, salt)
   - Decrypt the `data` field using AES-256-CTR + Poly1305
   - Parse the resulting JSON to get the master `encrypt` and `mac` keys

2. **Decrypt the config** to verify the repository ID and version

3. **Decrypt all index files** → build a mapping of
   `blob_id → (pack_file, offset, length)`

4. **Decrypt the most recent snapshot** (highest `time` value)
   → get the root tree blob ID

5. **Resolve the tree recursively**:
   - For each tree blob: look up its pack location in the index,
     read from the pack file, decrypt, parse JSON
   - For each file node: look up each content blob, decrypt,
     optionally decompress (zstd), concatenate → write file
   - For each directory node: recurse into subtree

6. **Verify** with SHA-256: the blob ID equals the SHA-256 of the
   decrypted (and decompressed) blob content

### Required Cryptographic Primitives

- **scrypt** (RFC 7914) — key derivation
- **AES-256-CTR** — symmetric encryption
- **Poly1305-AES** — message authentication
- **SHA-256** — content hashing / blob IDs
- **zstd** (optional) — decompression

All of these have multiple open-source implementations in every major
programming language.

---

## 11. LCSAS Disc Layout

On an LCSAS archive disc, the repository is split across multiple
volumes.  The layout per disc is:

```
disc_root/
├── data/                    # Pack files from one or more repos
│   ├── <sha256_hash>        # Flat layout (full hash = filename)
│   └── ...
├── metadata/                # Full repo metadata (ALL repos)
│   └── <repo_id>/
│       ├── config
│       ├── index/
│       ├── keys/
│       └── snapshots/
├── catalog.db               # SQLite: which packs are on which discs
├── volume_info.json         # Machine-readable disc manifest
└── RESTORE_INSTRUCTIONS.txt # Human-readable recovery guide
```

To reconstruct a repository from discs:

1. Copy `metadata/<repo_id>/*` from any disc → temporary directory
2. Copy `data/*` from ALL discs → `temporary/data/<prefix>/<sha>`
   (where `<prefix>` is the first 2 hex chars of the filename)
3. Point rustic/restic at the temporary directory as a repository

This is exactly what `restore.sh` on the meta-volume automates.
