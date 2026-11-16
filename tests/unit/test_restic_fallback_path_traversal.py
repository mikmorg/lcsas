"""Path-traversal and symlink protection in the tier-3 restorer.

These tests drive the **production** ``PurePythonRestorer._restore_tree``
(via ``restore()``) against crafted tree blobs in a synthetic restic repo,
rather than re-implementing the guard logic inline.

#782 changed the symlink rule: every link is restored with its stored
target, as tier 1 and rustic do (the RST-06 skip of absolute and
escaping links is gone).  Making a link writes nothing outside the
target; the protection is that nothing is ever written THROUGH a link
(#784), which the TestNoWriteThroughLinks cases check.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

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
        """A node name holding ``/`` is refused and counted (#781)."""
        repo = _build_repo_with_tree(
            tmp_path,
            [
                {"name": "../../../etc/passwd", "type": "file", "content": []},
                {"name": "safe.txt", "type": "file", "content": []},
            ],
        )
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        # The traversal node must not escape the target.
        assert not (tmp_path / "etc" / "passwd").exists()
        assert not (tmp_path.parent / "passwd").exists()
        # #781: a refused name is reported and is a failure, not a
        # silent skip.
        assert "NOT RESTORED" in capsys.readouterr().err
        assert restorer.failures == 1

    def test_absolute_symlink_restored_as_stored(self, tmp_path):
        """#782: an absolute target is restored as stored, as tier 1 and
        rustic do; it is not a failure."""
        repo = _build_repo_with_tree(
            tmp_path,
            [{"name": "abs.link", "type": "symlink", "linktarget": "/etc/passwd"}],
        )
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        assert os.readlink(target / "abs.link") == "/etc/passwd"
        assert restorer.failures == 0
        assert not (target / "RESTORE_FAILURES.txt").exists()

    def test_symlink_pointing_out_restored_as_stored(self, tmp_path):
        """#782: a relative target that points up and out of the restore
        (or up and across inside it) is restored as stored."""
        repo = _build_repo_with_tree(
            tmp_path,
            [
                {"name": "out.link", "type": "symlink",
                 "linktarget": "../../../../etc"},
                {"name": "dangling", "type": "symlink", "linktarget": "nowhere"},
            ],
        )
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        assert os.readlink(target / "out.link") == "../../../../etc"
        assert os.readlink(target / "dangling") == "nowhere"
        assert restorer.failures == 0

    @pytest.mark.skipif(os.name == "nt", reason="POSIX byte link targets")
    def test_linktarget_raw_wins(self, tmp_path):
        """#852: rustic keeps a non-UTF-8 target in linktarget_raw; the
        lossy linktarget must not be used."""
        repo = _build_repo_with_tree(
            tmp_path,
            [{"name": "raw.link", "type": "symlink",
              "linktarget": "tgt\ufffd",
              "linktarget_raw": base64.b64encode(b"tgt\xe9").decode()}],
        )
        target = tmp_path / "restored"
        PurePythonRestorer(repo, password=PASSWORD).restore(target=target)
        assert os.readlink(os.fsencode(target / "raw.link")) == b"tgt\xe9"

    def test_unusable_link_targets_are_failures(self, tmp_path, capsys):
        """#783: a link target that cannot be used is an error: no link,
        a row in RESTORE_FAILURES.txt, failures counted.  Same reasons
        as tier 1."""
        repo = _build_repo_with_tree(
            tmp_path,
            [
                {"name": "empty", "type": "symlink", "linktarget": ""},
                {"name": "nul", "type": "symlink", "linktarget": "a\u0000b"},
                {"name": "none", "type": "symlink"},
                {"name": "badraw", "type": "symlink", "linktarget": "x",
                 "linktarget_raw": "*not base64*"},
                {"name": "long", "type": "symlink", "linktarget": "x" * 5000},
                {"name": "ok", "type": "symlink", "linktarget": "t"},
            ],
        )
        target = tmp_path / "restored"
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        assert restorer.failures == 5
        for name in ("empty", "nul", "none", "badraw", "long"):
            assert not os.path.lexists(target / name), name
        assert os.readlink(target / "ok") == "t"
        manifest = (target / "RESTORE_FAILURES.txt").read_text()
        for reason in (
            "link refused: the link target is empty",
            "link refused: the link target holds a NUL byte",
            "link refused: the link has no target",
            "link refused: the link target cannot be decoded",
            "link refused: the link target is too long",
        ):
            assert reason in manifest, reason
        assert "ERROR: 5 item(s) were NOT restored." in capsys.readouterr().err

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


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
class TestNoWriteThroughLinks:
    """#784: restoring into a folder that holds an earlier restore never
    writes through a symlink there, and never removes a folder."""

    def test_links_in_the_way_are_replaced(self, tmp_path):
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("OUTSIDE")
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        outside_dir.chmod(0o751)
        repo = _build_repo_with_tree(
            tmp_path,
            [
                {"name": "x", "type": "file", "mode": 0o600, "content": []},
                {"name": "d", "type": "dir", "mode": 0o700},
                {"name": "q", "type": "file", "content": []},
            ],
        )
        target = tmp_path / "restored"
        target.mkdir()
        (target / "x").symlink_to(outside_file)
        (target / "d").symlink_to(outside_dir)
        os.mkfifo(target / "q")
        # The partial file's name, too, must not be followed.
        (target / "x.lcsas-partial").symlink_to(outside_file)

        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)

        assert restorer.failures == 0
        assert outside_file.read_text() == "OUTSIDE"
        assert (outside_file.stat().st_mode & 0o777) != 0o600
        assert (outside_dir.stat().st_mode & 0o777) == 0o751
        assert not (target / "x").is_symlink() and (target / "x").is_file()
        assert not (target / "d").is_symlink() and (target / "d").is_dir()
        assert (target / "q").is_file()

    def test_folder_in_the_way_is_kept_and_reported(self, tmp_path):
        repo = _build_repo_with_tree(
            tmp_path,
            [
                {"name": "l", "type": "symlink", "linktarget": "x"},
                {"name": "f", "type": "file", "content": []},
            ],
        )
        target = tmp_path / "restored"
        (target / "l").mkdir(parents=True)
        (target / "l" / "keep").write_text("keep")
        (target / "f").mkdir()
        restorer = PurePythonRestorer(repo, password=PASSWORD)
        restorer.restore(target=target)
        assert (target / "l" / "keep").read_text() == "keep"
        assert (target / "f").is_dir()
        assert restorer.failures == 2
        manifest = (target / "RESTORE_FAILURES.txt").read_text()
        assert manifest.count("a folder with this name is already there") == 2
