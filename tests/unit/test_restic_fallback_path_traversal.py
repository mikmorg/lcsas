"""Path-traversal / symlink-escape protection in the tier-3 restorer.

These tests drive the **production** ``PurePythonRestorer._restore_tree``
(via ``restore()``) against crafted tree blobs in a synthetic restic repo,
rather than re-implementing the guard logic inline. RST-06: the escaping
relative-symlink case previously asserted against a hand-rolled
``relative_to()`` copy, which gave false assurance while the real guard was
dead code.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lcsas.restore.restic_fallback import MasterKey, PurePythonRestorer

from .test_restic_fallback import (
    MASTER_ENCRYPT,
    MASTER_MAC_K,
    MASTER_MAC_R,
    PASSWORD,
    _encrypt_with_master,
    _make_key_file,
)


def _build_repo_with_tree(tmp_path: Path, tree_nodes: list) -> Path:
    """Build a synthetic restic repo whose root tree has *tree_nodes*."""
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
        blobs_info.append(
            {"id": bid, "type": btype, "offset": len(pack_data), "length": len(enc)}
        )
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

    snap_doc = json.dumps(
        {
            "time": "2026-04-01T00:00:00Z",
            "tree": root_tree_id,
            "paths": ["/test"],
            "hostname": "testhost",
        }
    ).encode()
    snap_dir = repo / "snapshots"
    snap_dir.mkdir()
    snap_id = hashlib.sha256(snap_doc).hexdigest()
    (snap_dir / snap_id).write_bytes(_encrypt_with_master(snap_doc))

    config_doc = json.dumps({"version": 2, "id": "tree-test"}).encode()
    (repo / "config").write_bytes(_encrypt_with_master(config_doc))

    return repo


class TestSymlinkEscapeProtection:
    """Drive the real restorer; assert escaping links never materialize."""

    def test_reject_node_name_with_parent_dir_component(self, tmp_path, capsys):
        """Node names with ``../`` are sanitized to the basename and skipped."""
        repo = _build_repo_with_tree(
            tmp_path,
            [
                {"name": "../../../etc/passwd", "type": "file", "content": []},
                {"name": "safe.txt", "type": "file", "content": []},
            ],
        )
        target = tmp_path / "restored"
        PurePythonRestorer(repo, password=PASSWORD).restore(target=target)
        # The traversal node must not escape the target.
        assert not (tmp_path / "etc" / "passwd").exists()
        assert not (tmp_path.parent / "passwd").exists()
        assert "suspicious name" in capsys.readouterr().err

    def test_reject_absolute_symlink_target(self, tmp_path, capsys):
        """Absolute symlink targets are skipped (never created)."""
        repo = _build_repo_with_tree(
            tmp_path,
            [{"name": "evil.link", "type": "symlink", "linktarget": "/etc/passwd"}],
        )
        target = tmp_path / "restored"
        PurePythonRestorer(repo, password=PASSWORD).restore(target=target)
        assert not (target / "evil.link").is_symlink()
        assert not (target / "evil.link").exists()
        assert "absolute target" in capsys.readouterr().err

    def test_reject_symlink_target_escaping_tree(self, tmp_path, capsys):
        """RST-06: a relative target resolving outside the target dir is
        skipped + logged + recorded, NOT created (the audit repro)."""
        repo = _build_repo_with_tree(
            tmp_path,
            [
                {
                    "name": "escape.link",
                    "type": "symlink",
                    "linktarget": "../../../../etc",
                }
            ],
        )
        target = tmp_path / "restored"
        PurePythonRestorer(repo, password=PASSWORD).restore(target=target)
        # No symlink, not even a dangling one.
        assert not (target / "escape.link").is_symlink()
        assert not (target / "escape.link").exists()
        captured = capsys.readouterr()
        assert "out-of-bounds" in captured.err
        manifest = (target / "RESTORE_FAILURES.txt").read_text()
        assert "skipped-symlink" in manifest

    def test_allow_relative_symlink_within_tree(self, tmp_path):
        """Legitimate in-tree relative symlinks are still restored."""
        repo = _build_repo_with_tree(
            tmp_path,
            [
                {"name": "actual.txt", "type": "file", "content": []},
                {"name": "link.txt", "type": "symlink", "linktarget": "actual.txt"},
            ],
        )
        target = tmp_path / "restored"
        PurePythonRestorer(repo, password=PASSWORD).restore(target=target)
        link = target / "link.txt"
        assert link.is_symlink()
        assert Path(link.readlink()) == Path("actual.txt")
        # No manifest is written when nothing failed.
        assert not (target / "RESTORE_FAILURES.txt").exists()
