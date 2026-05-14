"""Pure-Python restic repository restore — last-resort fallback.

This module implements a *complete* restic repository reader using only
Python 3 standard-library primitives (plus the vendored ``_aes_pure``
module).  It exists so that data can be recovered even when the
``rustic`` and ``restic`` binaries cannot execute — e.g., on a future
CPU architecture or after ABI changes have rendered all bundled native
tools unusable.

**Performance:** Expect roughly ~1 MB/s on modern hardware.  This is
acceptable for a manual emergency restore of the most critical files.

Crypto stack (all self-contained — no pip packages required):

    ========================  ================================
    Primitive                 Source
    ========================  ================================
    scrypt (KDF)              ``hashlib.scrypt`` (stdlib ≥ 3.6)
    AES-256-CTR               ``lcsas.restore._aes_pure``
    AES-128-ECB (Poly1305)    ``lcsas.restore._aes_pure``
    Poly1305-AES (MAC)        Implemented below
    SHA-256                   ``hashlib`` (stdlib)
    zstd (optional decomp.)   ``zstandard`` if installed
    ========================  ================================

Usage::

    from lcsas.restore.restic_fallback import PurePythonRestorer

    restorer = PurePythonRestorer(
        repo_path=Path("/mnt/restore_cache"),
        password_file=Path("/keys/family.key"),
    )
    restorer.restore(target=Path("/home/user/restored"))

See ``docs/RESTIC_FORMAT_SPEC.md`` for the full format reference.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from lcsas.restore._aes_pure import (
    aes_ctr,
    aes_encrypt_block,
    key_schedule,
)

# ── Optional zstd ────────────────────────────────────────────────

_ZSTD_MAGIC = b"\x28\xB5\x2F\xFD"

try:
    # zstandard is an optional dependency; the ignores cover the case
    # when it's not installed (and we still want mypy to pass) AND the
    # case when it IS installed but has incomplete type stubs (some
    # versions return Any from decompress()).  `unused-ignore` lets the
    # ignores stay quiet whichever mypy verdict applies on this host.
    import zstandard as _zstd  # type: ignore[import-not-found,unused-ignore]

    def _decompress_zstd(data: bytes, max_output_size: int = 0) -> bytes:
        dctx = _zstd.ZstdDecompressor()
        if max_output_size > 0:
            return dctx.decompress(data, max_output_size=max_output_size)  # type: ignore[no-any-return,unused-ignore]
        # Try without limit first; if that fails (no content size in
        # the frame header), fall back with a generous cap.
        try:
            return dctx.decompress(data)  # type: ignore[no-any-return,unused-ignore]
        except _zstd.ZstdError:
            # Generous fallback for highly-compressible data (e.g.
            # sparse database backups with long runs of zeros).
            return dctx.decompress(data, max_output_size=max(len(data) * 100, 64 * 1024 * 1024))  # type: ignore[no-any-return,unused-ignore]

    _HAS_ZSTD = True
except ImportError:
    _HAS_ZSTD = False

    def _decompress_zstd(data: bytes, max_output_size: int = 0) -> bytes:  # noqa: F811
        raise RuntimeError(
            "This repository uses zstd compression but the 'zstandard' "
            "Python package is not installed.  Install it with:\n"
            "  pip install zstandard\n"
            "or extract compressed blobs manually using the zstd CLI tool."
        )


# ── Poly1305-AES MAC ────────────────────────────────────────────

def _clamp_r(r_bytes: bytes) -> int:
    """Clamp the Poly1305 r key per RFC 8439 §2.5."""
    r = int.from_bytes(r_bytes, "little")
    return r & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF


def _poly1305_mac(
    key_r: bytes,
    key_s: bytes,
    message: bytes,
) -> bytes:
    """Compute a Poly1305 MAC tag.

    Args:
        key_r: 16-byte Poly1305 *r* key (will be clamped).
        key_s: 16-byte Poly1305 *s* key (AES-encrypted nonce).
        message: Data to authenticate.

    Returns:
        16-byte MAC tag.
    """
    r = _clamp_r(key_r)
    s = int.from_bytes(key_s, "little")

    p = (1 << 130) - 5
    h = 0

    for i in range(0, len(message), 16):
        block = message[i : i + 16]
        n = int.from_bytes(block, "little") | (1 << (len(block) * 8))
        h = ((h + n) * r) % p

    tag = (h + s) & ((1 << 128) - 1)
    return tag.to_bytes(16, "little")


# ── Restic Authenticated Encryption ─────────────────────────────


@dataclass(frozen=True)
class MasterKey:
    """Decrypted restic master key triple."""

    encrypt: bytes   # 32 bytes — AES-256 key
    mac_k: bytes     # 16 bytes — AES-128 key for Poly1305 nonce
    mac_r: bytes     # 16 bytes — Poly1305 r key


class IntegrityError(Exception):
    """MAC verification failed — data corrupted or wrong key."""


def _decrypt_authenticated(
    encrypt_key: bytes,
    mac_k: bytes,
    mac_r: bytes,
    data: bytes,
) -> bytes:
    """Decrypt restic authenticated ciphertext.

    Format: ``IV (16) || ciphertext || MAC (16)``.

    Args:
        encrypt_key: 32-byte AES-256 key.
        mac_k: 16-byte AES key for Poly1305 nonce encryption.
        mac_r: 16-byte Poly1305 r key.
        data: Encrypted blob (minimum 33 bytes: 16 IV + 1 ct + 16 MAC).

    Returns:
        Decrypted plaintext.

    Raises:
        IntegrityError: MAC verification failed.
    """
    if len(data) < 33:
        raise IntegrityError(
            f"Encrypted data too short ({len(data)} bytes, need ≥33)"
        )

    iv = data[:16]
    ciphertext = data[16:-16]
    expected_mac = data[-16:]

    # Verify MAC: s = AES-128-ECB(mac_k, iv), then Poly1305(mac_r, s, ct)
    mac_rk = key_schedule(mac_k)
    s = aes_encrypt_block(iv, mac_rk)
    computed_mac = _poly1305_mac(mac_r, s, ciphertext)

    if not _constant_time_eq(computed_mac, expected_mac):
        raise IntegrityError("MAC verification failed — wrong key or corrupted data")

    return aes_ctr(encrypt_key, iv, ciphertext)


def _constant_time_eq(a: bytes, b: bytes) -> bool:
    """Constant-time byte comparison to prevent timing attacks."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b, strict=False):
        result |= x ^ y
    return result == 0


# ── Key File Parsing ─────────────────────────────────────────────

def _load_master_key(key_file: Path, password: bytes) -> MasterKey:
    """Parse a restic key file and derive the master key.

    Args:
        key_file: Path to a restic key file (JSON).
        password: The repository password (raw bytes).

    Returns:
        Decrypted MasterKey.

    Raises:
        IntegrityError: Wrong password or corrupted key file.
    """
    with open(key_file, encoding="utf-8") as f:
        key_doc = json.load(f)

    # Derive key-encryption key via scrypt
    salt = base64.b64decode(key_doc["salt"])
    n = key_doc.get("N", 32768)
    r = key_doc.get("r", 8)
    p = key_doc.get("p", 1)

    derived = hashlib.scrypt(
        password, salt=salt, n=n, r=r, p=p, dklen=64,
        maxmem=max(128 * r * (n + p + 2) * 2, 2**25),  # ≥32 MB
    )

    # Split derived key into AES encryption key + Poly1305 MAC keys
    kek_encrypt = derived[:32]
    kek_mac_k = derived[32:48]
    kek_mac_r = derived[48:64]

    # Decrypt the master key
    encrypted_master = base64.b64decode(key_doc["data"])
    master_json = _decrypt_authenticated(
        kek_encrypt, kek_mac_k, kek_mac_r, encrypted_master
    )

    master = json.loads(master_json)
    return MasterKey(
        encrypt=base64.b64decode(master["encrypt"]),
        mac_k=base64.b64decode(master["mac"]["k"]),
        mac_r=base64.b64decode(master["mac"]["r"]),
    )


def _try_keys(keys_dir: Path, password: bytes) -> MasterKey:
    """Try all key files in the directory until one works.

    Args:
        keys_dir: Path to the repository ``keys/`` directory.
        password: The repository password.

    Returns:
        The first successfully decrypted MasterKey.

    Raises:
        IntegrityError: No key file could be decrypted.
    """
    key_files = sorted(keys_dir.iterdir())
    if not key_files:
        raise IntegrityError(f"No key files found in {keys_dir}")

    last_error: Exception | None = None
    for kf in key_files:
        if not kf.is_file():
            continue
        try:
            return _load_master_key(kf, password)
        except (IntegrityError, json.JSONDecodeError, KeyError) as e:
            last_error = e
            continue

    raise IntegrityError(
        f"Could not decrypt any key file in {keys_dir} — "
        f"wrong password? Last error: {last_error}"
    )


# ── Repository Reader ───────────────────────────────────────────

@dataclass
class BlobLocation:
    """Where a blob lives inside a pack file."""

    pack_id: str
    offset: int
    length: int
    blob_type: str  # "data" or "tree"
    uncompressed_length: int | None = None


@dataclass
class SnapshotMeta:
    """Parsed snapshot metadata."""

    snapshot_id: str
    time: str
    tree: str  # root tree blob ID
    paths: list[str] = field(default_factory=list)
    hostname: str = ""
    tags: list[str] = field(default_factory=list)


class PurePythonRestorer:
    """Pure-Python restic repository restore engine.

    This is a *last-resort* fallback.  Use ``rustic`` or ``restic``
    whenever possible — they are orders of magnitude faster.

    The restorer expects a fully-assembled repository cache (i.e., all
    pack files already copied to ``repo_path/data/``).  Use
    ``RestoreExecutor.prepare_cache()`` and ``ingest_volume()`` first.
    """

    def __init__(
        self,
        repo_path: Path,
        password_file: Path | None = None,
        password: bytes | None = None,
    ) -> None:
        """Initialize the restorer.

        Provide either *password_file* or raw *password*, not both.

        Args:
            repo_path: Path to an assembled restic repository.
            password_file: File containing the raw password on the first line.
            password: Raw password bytes (alternative to password_file).
        """
        self.repo_path = repo_path

        if password is not None:
            self._password = password
        elif password_file is not None:
            self._password = password_file.read_bytes().rstrip(b"\n\r")
        else:
            raise ValueError("Provide either password_file or password")

        self._master_key: MasterKey | None = None
        self._blob_index: dict[str, BlobLocation] | None = None
        self._snapshots: list[SnapshotMeta] | None = None

    # ── Public API ───────────────────────────────────────────────

    def restore(
        self,
        target: Path,
        snapshot_id: str | None = None,
    ) -> SnapshotMeta:
        """Restore a snapshot to *target*.

        If *snapshot_id* is ``None``, restores the most recent snapshot.

        Args:
            target: Directory to write restored files into.
            snapshot_id: Optional specific snapshot to restore.

        Returns:
            SnapshotMeta for the restored snapshot.
        """
        self._ensure_loaded()

        if snapshot_id is not None:
            snap = self._find_snapshot(snapshot_id)
        else:
            snap = self._latest_snapshot()

        _log(f"Restoring snapshot {snap.snapshot_id[:12]}... "
             f"({', '.join(snap.paths)})")
        _log(f"Target: {target}")

        target.mkdir(parents=True, exist_ok=True)
        self._restore_tree(snap.tree, target)

        _log("Restore complete.")
        return snap

    def list_snapshots(self) -> list[SnapshotMeta]:
        """Return all snapshots sorted by time (oldest first)."""
        self._ensure_loaded()
        assert self._snapshots is not None
        return list(self._snapshots)

    def verify_key(self) -> bool:
        """Test whether the password can decrypt the repository.

        Returns True if key decryption succeeds, False otherwise.
        """
        try:
            self._load_key()
            return True
        except IntegrityError:
            return False

    # ── Loading / Initialization ─────────────────────────────────

    def _ensure_loaded(self) -> None:
        """Load key, index, and snapshots if not already done."""
        if self._master_key is None:
            self._load_key()
        if self._blob_index is None:
            self._load_index()
        if self._snapshots is None:
            self._load_snapshots()

    def _load_key(self) -> None:
        """Decrypt the master key from the repository's key files."""
        keys_dir = self.repo_path / "keys"
        self._master_key = _try_keys(keys_dir, self._password)
        _log("Master key decrypted successfully.")

    def _decrypt(self, encrypted: bytes) -> bytes:
        """Decrypt data using the master key."""
        assert self._master_key is not None
        mk = self._master_key
        return _decrypt_authenticated(mk.encrypt, mk.mac_k, mk.mac_r, encrypted)

    def _decrypt_file(self, path: Path) -> bytes:
        """Read, decrypt, and (if needed) decompress a repository file.

        Restic repository format v2 prepends a compression-type byte
        after decryption:
          - ``\\x00``  → uncompressed (strip the prefix)
          - ``\\x01``  → zstd-compressed (strip the prefix, decompress)
          - ``\\x02``  → zstd-compressed (strip the prefix, decompress)
          - ``{``       → v1 repo, raw JSON, no prefix

        After the prefix byte the data is either raw JSON or a
        zstd frame (magic ``\\x28\\xB5\\x2F\\xFD``).
        """
        data = self._decrypt(path.read_bytes())

        # Repo v2: first byte is a compression-type indicator
        if len(data) > 5 and data[1:5] == _ZSTD_MAGIC:
            # Byte 0 is compression type (1 or 2), rest is zstd frame
            data = _decompress_zstd(data[1:])
        elif len(data) > 1 and data[0:1] in (b"\x00", b"\x01", b"\x02"):
            # Compression type present but no zstd magic → just strip
            data = data[1:]

        return data

    def _load_index(self) -> None:
        """Read and decrypt all index files → build blob location map."""
        index_dir = self.repo_path / "index"
        if not index_dir.is_dir():
            raise FileNotFoundError(f"Index directory not found: {index_dir}")

        self._blob_index = {}
        supersedes: set[str] = set()

        # First pass: find superseded index files
        index_files = sorted(index_dir.iterdir())
        index_data: list[tuple[str, dict[str, Any]]] = []
        for idx_file in index_files:
            if not idx_file.is_file():
                continue
            plaintext = self._decrypt_file(idx_file)
            idx_doc = json.loads(plaintext)
            index_data.append((idx_file.name, idx_doc))
            for sup in idx_doc.get("supersedes", []):
                supersedes.add(sup)

        # Second pass: build blob index, skipping superseded
        for idx_name, idx_doc in index_data:
            if idx_name in supersedes:
                continue
            for pack_entry in idx_doc.get("packs", []):
                pack_id = pack_entry["id"]
                for blob in pack_entry.get("blobs", []):
                    blob_id = blob["id"]
                    self._blob_index[blob_id] = BlobLocation(
                        pack_id=pack_id,
                        offset=blob["offset"],
                        length=blob["length"],
                        blob_type=blob["type"],
                        uncompressed_length=blob.get("uncompressed_length"),
                    )

        _log(f"Loaded index: {len(self._blob_index)} blobs "
             f"across {len(index_data)} index files.")

    def _load_snapshots(self) -> None:
        """Read and decrypt all snapshot files."""
        snap_dir = self.repo_path / "snapshots"
        if not snap_dir.is_dir():
            raise FileNotFoundError(f"Snapshots directory not found: {snap_dir}")

        self._snapshots = []
        for snap_file in sorted(snap_dir.iterdir()):
            if not snap_file.is_file():
                continue
            plaintext = self._decrypt_file(snap_file)
            snap_doc = json.loads(plaintext)
            self._snapshots.append(
                SnapshotMeta(
                    snapshot_id=snap_file.name,
                    time=snap_doc.get("time", ""),
                    tree=snap_doc["tree"],
                    paths=snap_doc.get("paths", []),
                    hostname=snap_doc.get("hostname", ""),
                    tags=snap_doc.get("tags", []),
                )
            )

        # Sort by time (ISO 8601 strings sort lexicographically)
        self._snapshots.sort(key=lambda s: s.time)
        _log(f"Found {len(self._snapshots)} snapshot(s).")

    def _latest_snapshot(self) -> SnapshotMeta:
        """Return the most recent snapshot."""
        assert self._snapshots is not None
        if not self._snapshots:
            raise ValueError("No snapshots found in repository")
        return self._snapshots[-1]

    def _find_snapshot(self, snapshot_id: str) -> SnapshotMeta:
        """Find a snapshot by exact or prefix ID match."""
        assert self._snapshots is not None
        # Try exact match first
        for snap in self._snapshots:
            if snap.snapshot_id == snapshot_id:
                return snap
        # Try prefix match
        matches = [
            s for s in self._snapshots
            if s.snapshot_id.startswith(snapshot_id)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous snapshot prefix '{snapshot_id}' — "
                f"matches {len(matches)} snapshots"
            )
        raise ValueError(f"Snapshot not found: '{snapshot_id}'")

    # ── Blob Reading ─────────────────────────────────────────────

    def _find_pack_path(self, pack_id: str) -> Path:
        """Locate a pack file in the data directory.

        Supports both flat and two-level layouts.
        """
        data_dir = self.repo_path / "data"
        # Two-level layout (standard)
        two_level = data_dir / pack_id[:2] / pack_id
        if two_level.is_file():
            return two_level
        # Flat layout (LCSAS disc layout)
        flat = data_dir / pack_id
        if flat.is_file():
            return flat
        raise FileNotFoundError(
            f"Pack file not found: {pack_id}\n"
            f"Looked in: {two_level}, {flat}"
        )

    def _read_blob(self, blob_id: str) -> bytes:
        """Read, decrypt, and (optionally) decompress a blob.

        Also verifies the SHA-256 of the decrypted content matches
        the blob ID.

        Returns:
            Raw blob content (file data or tree JSON).
        """
        assert self._blob_index is not None
        if blob_id not in self._blob_index:
            raise KeyError(f"Blob not found in index: {blob_id}")

        loc = self._blob_index[blob_id]
        pack_path = self._find_pack_path(loc.pack_id)

        with open(pack_path, "rb") as f:
            f.seek(loc.offset)
            encrypted = f.read(loc.length)

        plaintext = self._decrypt(encrypted)

        # Handle zstd compression.  In restic repo v2, compressed
        # pack blobs start directly with the zstd frame (no type
        # prefix byte, unlike standalone files like index/snapshots).
        if plaintext[:4] == _ZSTD_MAGIC:
            max_out = loc.uncompressed_length or (len(plaintext) * 20)
            plaintext = _decompress_zstd(plaintext, max_output_size=max_out)

        # Verify content hash
        actual_hash = hashlib.sha256(plaintext).hexdigest()
        if actual_hash != blob_id:
            raise IntegrityError(
                f"Blob content hash mismatch: expected {blob_id}, "
                f"got {actual_hash}"
            )

        return plaintext

    # ── Tree Traversal & File Extraction ─────────────────────────

    def _restore_tree(self, tree_id: str, target_dir: Path) -> None:
        """Recursively restore a tree node to *target_dir*."""
        tree_data = self._read_blob(tree_id)
        tree_doc = json.loads(tree_data)

        # Track hardlink targets: inode → first_path
        hardlink_map: dict[int, Path] = {}

        for node in tree_doc.get("nodes", []):
            name = node["name"]
            node_type = node.get("type", "file")
            # Sanitize node name to prevent path traversal (e.g., "../../../etc/passwd")
            # Use only the basename to strip any directory components
            safe_name = Path(name).name
            if not safe_name or safe_name != name:
                _log(
                    f"Skipping node with suspicious name: {name!r} "
                    f"(contains directory components or empty)"
                )
                continue
            node_path = target_dir / safe_name

            if node_type == "file":
                inode = node.get("inode", 0)
                links = node.get("links", 1)

                # Hardlink deduplication: if inode already restored, link it
                if inode and links > 1 and inode in hardlink_map:
                    src = hardlink_map[inode]
                    try:
                        node_path.parent.mkdir(parents=True, exist_ok=True)
                        os.link(src, node_path)
                        continue
                    except OSError:
                        # Cross-device or permission issue — fall through
                        # to normal restore
                        _log(
                            f"Hardlink {node_path} → {src} failed, "
                            f"copying instead"
                        )

                self._restore_file(node, node_path)

                # Register as hardlink source for future occurrences
                if inode and links > 1:
                    hardlink_map[inode] = node_path

            elif node_type == "dir":
                node_path.mkdir(parents=True, exist_ok=True)
                if "subtree" in node:
                    self._restore_tree(node["subtree"], node_path)
                # Restore directory permissions after contents
                self._apply_metadata(node, node_path)
            elif node_type == "symlink":
                link_target = node.get("linktarget", "")
                # Validate symlink target: only allow relative links to stay within target_dir
                if Path(link_target).is_absolute():
                    _log(
                        f"Skipping symlink {node_path.name} with absolute target "
                        f"(security: {link_target!r})"
                    )
                    continue
                # Resolve the symlink target relative to the node's parent directory
                resolved = (node_path.parent / link_target).resolve()
                try:
                    resolved.is_relative_to(target_dir.resolve())
                except ValueError:
                    # Symlink resolves outside target directory
                    _log(
                        f"Skipping symlink {node_path.name} with out-of-bounds target "
                        f"(would escape to {resolved})"
                    )
                    continue
                # Target is valid; create the symlink
                if node_path.is_symlink() or node_path.exists():
                    if node_path.is_dir() and not node_path.is_symlink():
                        shutil.rmtree(node_path)
                    else:
                        node_path.unlink()
                node_path.symlink_to(link_target)
            else:
                _log(
                    f"Skipping unsupported node type {node_type!r}: {name}"
                )

    def _restore_file(self, node: dict[str, Any], path: Path) -> None:
        """Extract a file from its content blobs."""
        path.parent.mkdir(parents=True, exist_ok=True)
        content_ids: list[str] = node.get("content", [])

        with open(path, "wb") as f:
            for blob_id in content_ids:
                chunk = self._read_blob(blob_id)
                f.write(chunk)

        self._apply_metadata(node, path)

    def _apply_metadata(self, node: dict[str, Any], path: Path) -> None:
        """Best-effort metadata restoration (permissions, timestamps, xattrs)."""
        try:
            if "mode" in node and not path.is_symlink():
                os.chmod(path, node["mode"] & 0o7777)
        except OSError as exc:
            _log(f"Could not set permissions on {path}: {exc}")

        try:
            mtime = node.get("mtime")
            atime = node.get("atime")
            if mtime:
                # Parse ISO 8601 nanosecond timestamp → epoch float
                mt = _parse_timestamp(mtime)
                at = _parse_timestamp(atime) if atime else mt
                os.utime(path, (at, mt), follow_symlinks=False)
        except (OSError, ValueError) as exc:
            _log(f"Could not set timestamps on {path}: {exc}")

        # Extended attributes (restic stores as list of {name, value})
        for xa in node.get("extended_attributes", []):
            try:
                xa_name = xa.get("name", "")
                xa_value = base64.b64decode(xa.get("value", ""))
                if xa_name and hasattr(os, "setxattr"):
                    os.setxattr(
                        str(path), xa_name, xa_value,
                        follow_symlinks=False,
                    )
            except OSError:
                _log(
                    f"Could not set xattr {xa.get('name')} on {path}"
                )

    # ── Diagnostic / Info ────────────────────────────────────────

    def repo_info(self) -> dict[str, Any]:
        """Return basic repository information (config + snapshot count)."""
        self._ensure_loaded()

        config_path = self.repo_path / "config"
        config_doc = {}
        if config_path.is_file():
            import contextlib
            with contextlib.suppress(Exception):
                config_doc = json.loads(self._decrypt_file(config_path))

        assert self._snapshots is not None
        assert self._blob_index is not None
        return {
            "repository_id": config_doc.get("id", "unknown"),
            "version": config_doc.get("version", "unknown"),
            "snapshots": len(self._snapshots),
            "indexed_blobs": len(self._blob_index),
            "has_zstd": _HAS_ZSTD,
        }


# ── Utilities ────────────────────────────────────────────────────

def _parse_timestamp(ts: str) -> float:
    """Parse a restic-style ISO 8601 timestamp to epoch seconds.

    Handles nanosecond precision: ``2026-01-15T10:30:00.123456789Z``.
    Falls back to basic parsing if the format is unexpected.
    """
    # Strip trailing timezone designator and nanoseconds
    ts = ts.rstrip("Z")
    if "." in ts:
        date_part, frac = ts.split(".", 1)
        # Truncate to microseconds (Python limit)
        frac = frac[:6].ljust(6, "0")
        ts = f"{date_part}.{frac}"
    else:
        ts = ts + ".000000"

    # Python 3.7+ datetime.fromisoformat
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(ts).replace(tzinfo=UTC)
    except ValueError:
        # Last resort: basic parse
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f").replace(
            tzinfo=UTC
        )
    return dt.timestamp()


def _log(msg: str) -> None:
    """Simple stderr logger for fallback restore progress."""
    print(f"[restic-fallback] {msg}", file=sys.stderr, flush=True)
