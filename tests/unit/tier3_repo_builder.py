"""A small synthetic restic repository for tier-3 (the Python reader) tests.

Not a test module itself (no ``test_`` prefix).  It builds a repository
with nested directories, one pack per call, and lets a test store a
blob's bytes as they should sit in the pack (for example a damaged zstd
frame whose index entry says "compressed").  The keys, password and
encryption come from ``test_restic_fallback`` so every tier-3 test uses
the same scheme.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from lcsas.restore.restic_fallback import MasterKey

from .test_restic_fallback import (
    MASTER_ENCRYPT,
    MASTER_MAC_K,
    MASTER_MAC_R,
    PASSWORD,
    _encrypt_with_master,
    _make_key_file,
)

__all__ = ["PASSWORD", "Blob", "build_repo"]


@dataclass(frozen=True)
class Blob:
    """One data blob: the bytes stored (before encryption) and its index entry.

    ``blob_id`` defaults to the SHA-256 of ``stored``.  Set
    ``uncompressed_length`` to mark the blob compressed in the index.
    """

    stored: bytes
    blob_id: str = ""
    uncompressed_length: int | None = None

    def id(self) -> str:
        return self.blob_id or hashlib.sha256(self.stored).hexdigest()


#: A file is its content bytes (one plain blob) or a list of Blob.
#: A directory is a dict of name -> file or directory.
Tree = dict[str, Union[bytes, list[Blob], "Tree"]]


def build_repo(
    tmp_path: Path, tree: Tree, *, name: str = "repo",
    pack_per_blob: bool = False,
) -> Path:
    """Write a repository holding one snapshot of *tree*; return its path.

    All blobs go in one pack, or with *pack_per_blob* each blob in a
    pack of its own (to spread a snapshot over several discs).
    """
    repo = tmp_path / name
    repo.mkdir()
    mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)
    _make_key_file(mk, PASSWORD, repo)

    packs: list[tuple[bytearray, list[dict[str, object]]]] = [(bytearray(), [])]
    seen: set[str] = set()

    def add_blob(stored: bytes, blob_id: str, btype: str,
                 ulen: int | None) -> None:
        if blob_id in seen:
            return
        seen.add(blob_id)
        if pack_per_blob and packs[-1][1]:
            packs.append((bytearray(), []))
        pack, entries = packs[-1]
        enc = _encrypt_with_master(stored)
        entry: dict[str, object] = {
            "id": blob_id, "type": btype,
            "offset": len(pack), "length": len(enc),
        }
        if ulen is not None:
            entry["uncompressed_length"] = ulen
        entries.append(entry)
        pack.extend(enc)

    def add_tree(t: Tree) -> str:
        nodes: list[dict[str, object]] = []
        for nm, item in t.items():
            if isinstance(item, dict):
                nodes.append({"name": nm, "type": "dir", "mode": 0o755,
                              "subtree": add_tree(item)})
                continue
            blobs = [Blob(item)] if isinstance(item, bytes) else item
            for b in blobs:
                add_blob(b.stored, b.id(), "data", b.uncompressed_length)
            size = sum(
                b.uncompressed_length if b.uncompressed_length is not None
                else len(b.stored)
                for b in blobs
            )
            nodes.append({
                "name": nm, "type": "file", "mode": 0o644,
                "mtime": "2026-01-15T10:00:00.000000000Z",
                "size": size,
                "content": [b.id() for b in blobs],
            })
        doc = json.dumps({"nodes": nodes}).encode()
        tid = hashlib.sha256(doc).hexdigest()
        add_blob(doc, tid, "tree", None)
        return tid

    root = add_tree(tree)

    index_packs: list[dict[str, object]] = []
    for pack, entries in packs:
        pack_id = hashlib.sha256(bytes(pack)).hexdigest()
        (repo / "data" / pack_id[:2]).mkdir(parents=True, exist_ok=True)
        (repo / "data" / pack_id[:2] / pack_id).write_bytes(bytes(pack))
        index_packs.append({"id": pack_id, "blobs": entries})

    def write_doc(sub: str, doc: dict[str, object]) -> None:
        raw = json.dumps(doc).encode()
        d = repo / sub
        d.mkdir(exist_ok=True)
        (d / hashlib.sha256(raw).hexdigest()).write_bytes(
            _encrypt_with_master(raw))

    write_doc("index", {"packs": index_packs})
    write_doc("snapshots", {
        "time": "2026-01-15T10:30:00.000000000Z", "tree": root,
        "paths": ["/home/test"], "hostname": "testhost",
    })
    (repo / "config").write_bytes(_encrypt_with_master(json.dumps(
        {"version": 2, "id": "tier3-test", "chunker_polynomial": "3DA3358B4DC173"},
    ).encode()))
    return repo
