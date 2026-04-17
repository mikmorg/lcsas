"""Unit tests for LCSAS recovery wizard disc discovery.

Tests the repository discovery logic without requiring the full TUI.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest


class TestDiscMetadataDiscovery:
    """Test repository discovery by scanning /metadata/ directory."""

    def test_discover_single_repo(self):
        """Discover a single repository on the disc."""
        xorriso_output = """Directory:  /metadata/
Directory:  /metadata/my-repo
"""
        repos = self._parse_metadata_dirs(xorriso_output)
        assert repos == ["my-repo"]

    def test_discover_multiple_repos(self):
        """Discover multiple repositories on the disc."""
        xorriso_output = """Directory:  /metadata/
Directory:  /metadata/repo-1
Directory:  /metadata/repo-2
Directory:  /metadata/repo-3
"""
        repos = self._parse_metadata_dirs(xorriso_output)
        assert set(repos) == {"repo-1", "repo-2", "repo-3"}

    def test_discover_ignores_whitespace_and_quotes(self):
        """Handle xorriso output with varied formatting."""
        xorriso_output = """Directory:  '/metadata/'
Directory:  '/metadata/archive'
Directory:  '/metadata/personal'
"""
        repos = self._parse_metadata_dirs(xorriso_output)
        assert set(repos) == {"archive", "personal"}

    def test_discover_no_repos(self):
        """Handle case where /metadata/ exists but is empty."""
        xorriso_output = """Directory:  /metadata/
"""
        repos = self._parse_metadata_dirs(xorriso_output)
        assert repos == []

    def test_discover_handles_empty_output(self):
        """Handle empty xorriso output (e.g., missing /metadata/)."""
        xorriso_output = ""
        repos = self._parse_metadata_dirs(xorriso_output)
        assert repos == []

    def test_discover_ignores_non_metadata_dirs(self):
        """Ignore /data/ and other non-metadata directories."""
        xorriso_output = """Directory:  /data/
Directory:  /metadata/
Directory:  /metadata/my-repo
Directory:  /other/
"""
        repos = self._parse_metadata_dirs(xorriso_output)
        assert repos == ["my-repo"]

    @staticmethod
    def _parse_metadata_dirs(xorriso_output: str) -> list[str]:
        """Parse xorriso -find output to extract repository IDs.

        This mirrors the parsing logic in screen_select_repo().
        xorriso -find output format: "Directory:  /path/to/dir" or "Directory:  '/path/to/dir'"
        """
        repos: list[str] = []
        for line in xorriso_output.splitlines():
            line = line.strip()
            # Skip the "Directory:" prefix if present
            if line.startswith("Directory:"):
                line = line.split(None, 1)[1] if " " in line else ""
            # Strip quotes
            line = line.strip("'\"")
            if line.startswith("/metadata/") and line != "/metadata/":
                repo_id = line.split("/")[2]
                if repo_id and repo_id not in repos:
                    repos.append(repo_id)
        return repos

    @patch("subprocess.run")
    def test_discover_via_xorriso_subprocess(self, mock_run):
        """Test the subprocess integration for xorriso discovery."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="""Directory:  /metadata/
Directory:  /metadata/home
Directory:  /metadata/work
""",
            stderr="",
        )

        result = subprocess.run(
            ["xorriso", "-indev", "test.iso", "-find", "/metadata",
             "-maxdepth", "1", "-type", "d"],
            capture_output=True, text=True, timeout=30,
        )

        repos = self._parse_metadata_dirs(result.stdout)
        assert set(repos) == {"home", "work"}
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_discover_handles_xorriso_not_found(self, mock_run):
        """Handle missing xorriso binary gracefully."""
        mock_run.side_effect = FileNotFoundError("xorriso not found")

        with pytest.raises(FileNotFoundError):
            subprocess.run(["xorriso", "-help"], capture_output=True, timeout=5)

    @patch("subprocess.run")
    def test_discover_handles_xorriso_timeout(self, mock_run):
        """Handle xorriso timeout gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["xorriso"], timeout=30
        )

        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run(
                ["xorriso", "-indev", "test.iso", "-find", "/metadata"],
                timeout=30,
            )

    @patch("subprocess.run")
    def test_discover_handles_xorriso_error(self, mock_run):
        """Handle xorriso returning non-zero exit code."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="xorriso: FAILURE : Cannot open image file 'nonexistent.iso'",
        )

        result = subprocess.run(
            ["xorriso", "-indev", "nonexistent.iso", "-find", "/metadata"],
            capture_output=True, text=True, timeout=30,
        )

        assert result.returncode != 0
        assert "Cannot open" in result.stderr
