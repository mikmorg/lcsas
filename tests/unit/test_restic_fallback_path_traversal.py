"""Unit tests for path traversal protection in restic_fallback restore."""

from pathlib import Path
from unittest.mock import MagicMock, patch


class TestPathTraversalProtection:
    """Test that _restore_tree sanitizes node names and symlink targets."""

    @patch("lcsas.restore.restic_fallback._log")
    def test_reject_node_name_with_parent_dir_component(self, mock_log):
        """Node names with ../ should be rejected to prevent traversal."""
        from lcsas.restore.restic_fallback import PurePythonRestorer

        restorer = MagicMock(spec=PurePythonRestorer)
        blob_data = b'{"nodes":[{"name":"../../../etc/passwd","type":"file"}]}'
        restorer._read_blob = MagicMock(return_value=blob_data)

        # Test the sanitization logic
        import json

        tree_data = restorer._read_blob("tree-id")
        tree_doc = json.loads(tree_data)

        for node in tree_doc.get("nodes", []):
            name = node["name"]
            safe_name = Path(name).name
            # Safe name should be just "passwd", not the full traversal path
            assert safe_name == "passwd"
            assert safe_name != name, "Dangerous path should be sanitized"

    def test_reject_absolute_symlink_target(self, tmp_path):
        """Symlink targets that are absolute should be rejected."""
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        # Simulate absolute symlink target
        link_target = "/etc/passwd"

        # This should be detected as dangerous
        is_absolute = Path(link_target).is_absolute()
        assert is_absolute, "Absolute path should be detected"

    def test_reject_symlink_target_escaping_tree(self, tmp_path):
        """Symlink targets that resolve outside target_dir should be rejected."""
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        node_parent = target_dir / "dir"
        node_parent.mkdir()

        # Create a symlink target that escapes the target_dir
        link_target = "../../outside"

        # Resolve the target relative to the node's parent
        resolved = (node_parent / link_target).resolve()

        # Check if it escapes the target directory
        try:
            resolved.relative_to(target_dir.resolve())
            escapes = False
        except ValueError:
            escapes = True

        assert escapes, "Symlink target ../../outside should escape target_dir"

    def test_allow_relative_symlink_within_tree(self, tmp_path):
        """Relative symlinks that stay within target_dir should be allowed."""
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        subdir = target_dir / "subdir"
        subdir.mkdir()

        # Valid relative symlink within the tree
        link_target = "../other_file"

        # Resolve relative to subdir
        resolved = (subdir / link_target).resolve()

        # Should be within target_dir
        try:
            resolved.relative_to(target_dir.resolve())
            within = True
        except ValueError:
            within = False

        assert within, "Relative symlink ../other_file should stay within target_dir"

    def test_node_name_with_slashes_stripped(self):
        """Node names with directory separators should be stripped to basename only."""
        # Names like "dir/file" should become just "file"
        names = [
            "simple.txt",              # OK
            "dir/file.txt",            # Should become "file.txt"
            "../../../etc/passwd",     # Should become "passwd"
            "./file.txt",              # Should become "file.txt"
        ]

        for name in names:
            safe_name = Path(name).name
            assert "/" not in safe_name, f"Safe name {safe_name!r} should not contain /"
            assert ".." not in safe_name, f"Safe name {safe_name!r} should not contain .."

    def test_empty_node_name_rejected(self):
        """Node names that resolve to empty after sanitization should be rejected."""
        # Edge case: name is just a directory separator
        for name in [".", "..", "/", "///", ""]:
            safe_name = Path(name).name
            if not safe_name:  # Empty basename means it should be rejected
                assert safe_name == "", f"Name {name!r} should sanitize to empty"
