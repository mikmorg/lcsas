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

__all__ = ["PASSWORD", "Blob", "RawNode", "build_repo"]


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


@dataclass(frozen=True)
class RawNode:
    """A node stored as given (its ``name`` comes from the tree key).

    For what rustic will not write: a device, which needs root to make,
    or a node whose ``type`` is unknown or missing.  It has no blobs.
    """

    node: dict[str, object]


#: A file is its content bytes (one plain blob) or a list of Blob.
#: A directory is a dict of name -> file or directory.
Tree = dict[str, Union[bytes, list[Blob], RawNode, "Tree"]]


def build_repo(
    tmp_path: Path, tree: Tree, *, name: str = "repo",
    pack_per_blob: bool = False,
    layout: dict[str, list[str]] | None = None,
) -> Path:
    """Write a repository holding one snapshot of *tree*; return its path.

    All blobs go in one pack, or with *pack_per_blob* each blob in a
    pack of its own (to spread a snapshot over several discs).

    *layout*, when given, is filled with where each item's own blobs
    went: its path ("/"-joined; "" is the root folder) -> the pack id of
    each of its blobs, in order (a file's content blobs, a folder's tree
    blob).
    """
    repo = tmp_path / name
    repo.mkdir()
    mk = MasterKey(encrypt=MASTER_ENCRYPT, mac_k=MASTER_MAC_K, mac_r=MASTER_MAC_R)
    _make_key_file(mk, PASSWORD, repo)

    packs: list[tuple[bytearray, list[dict[str, object]]]] = [(bytearray(), [])]
    seen: dict[str, int] = {}       # blob id -> index of its pack
    item_blobs: dict[str, list[str]] = {}

    def add_blob(stored: bytes, blob_id: str, btype: str,
                 ulen: int | None) -> None:
        if blob_id in seen:
            return
        if pack_per_blob and packs[-1][1]:
            packs.append((bytearray(), []))
        seen[blob_id] = len(packs) - 1
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

    def add_tree(t: Tree, at: str = "") -> str:
        nodes: list[dict[str, object]] = []
        for nm, item in t.items():
            path = f"{at}/{nm}" if at else nm
            if isinstance(item, dict):
                nodes.append({"name": nm, "type": "dir", "mode": 0o755,
                              "subtree": add_tree(item, path)})
                continue
            if isinstance(item, RawNode):
                nodes.append({"name": nm, **item.node})
                continue
            blobs = [Blob(item)] if isinstance(item, bytes) else item
            for b in blobs:
                add_blob(b.stored, b.id(), "data", b.uncompressed_length)
            item_blobs[path] = [b.id() for b in blobs]
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
        item_blobs[at] = [tid]
        return tid

    root = add_tree(tree)

    index_packs: list[dict[str, object]] = []
    pack_ids: list[str] = []
    for pack, entries in packs:
        pack_id = hashlib.sha256(bytes(pack)).hexdigest()
        (repo / "data" / pack_id[:2]).mkdir(parents=True, exist_ok=True)
        (repo / "data" / pack_id[:2] / pack_id).write_bytes(bytes(pack))
        index_packs.append({"id": pack_id, "blobs": entries})
        pack_ids.append(pack_id)
    if layout is not None:
        for path, ids in item_blobs.items():
            layout[path] = [pack_ids[seen[b]] for b in ids]

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
