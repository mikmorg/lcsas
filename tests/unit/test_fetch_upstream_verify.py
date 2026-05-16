"""Tests for ``recovery/scripts/fetch_upstream.sh --verify-only`` mode.

The verify-only mode (Phase 21.5.b) backs ``make verify-recovery``: it
audits the local cache against ``recovery/UPSTREAM.sha256`` without
downloading.  These tests exercise it with a tiny synthetic cache so
no network calls happen.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FETCH_SCRIPT = REPO_ROOT / "recovery" / "scripts" / "fetch_upstream.sh"


def _run(*args: str, cache: Path | None = None) -> subprocess.CompletedProcess:
    """Invoke fetch_upstream.sh with a clean environment."""
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
    if cache is not None:
        env["LCSAS_RECOVERY_CACHE"] = str(cache)
    return subprocess.run(
        ["sh", str(FETCH_SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def synthetic_cache(tmp_path):
    """Build a one-file cache with a known SHA-256 and a matching manifest.

    Returns (cache_root, manifest_path, body_sha).
    """
    cache = tmp_path / "cache"
    body = b"synthetic upstream tarball body"
    body_sha = hashlib.sha256(body).hexdigest()
    rel = "rustic/x86_64-unknown-linux-musl/synth.tar.gz"
    (cache / rel).parent.mkdir(parents=True)
    (cache / rel).write_bytes(body)

    manifest = tmp_path / "UPSTREAM.sha256"
    manifest.write_text(f"{body_sha}  {rel}\n")

    return cache, manifest, body_sha


class TestVerifyOnly:
    def test_clean_cache_passes(self, synthetic_cache):
        """Cache matches manifest → exit 0, prints [cached] + success."""
        cache, manifest, _ = synthetic_cache
        result = _run(
            "--verify-only",
            "--cache", str(cache),
            "--manifest", str(manifest),
        )
        assert result.returncode == 0
        # stderr because all the script's status logging goes there.
        out = result.stderr
        assert "[cached]" in out
        assert "all 1 artifacts verified" in out

    def test_corrupted_cache_fails(self, synthetic_cache):
        """A SHA mismatch against a cached file → exit 1 with [error]."""
        cache, manifest, _ = synthetic_cache
        # Replace the cached file with bytes that won't hash-match.
        bad_rel = "rustic/x86_64-unknown-linux-musl/synth.tar.gz"
        (cache / bad_rel).write_bytes(b"different content entirely")

        result = _run(
            "--verify-only",
            "--cache", str(cache),
            "--manifest", str(manifest),
        )
        assert result.returncode == 1
        out = result.stderr
        assert "SHA mismatch" in out
        assert "1 of 1 artifacts failed" in out

    def test_missing_file_fails(self, synthetic_cache):
        """Manifest entry with no cached file → exit 1 with [error]."""
        cache, manifest, _ = synthetic_cache
        # Add an extra manifest line for a file we never cached.
        with manifest.open("a") as f:
            f.write("0" * 64 + "  rustic/never-fetched/something.tar.gz\n")

        result = _run(
            "--verify-only",
            "--cache", str(cache),
            "--manifest", str(manifest),
        )
        assert result.returncode == 1
        out = result.stderr
        assert "missing from cache" in out
        assert "never-fetched/something.tar.gz" in out

    def test_verify_only_never_downloads(self, synthetic_cache, tmp_path):
        """Empty cache + valid manifest → exit 1 with "missing"
        (NOT a silent download).  Guard against the verify-only mode
        accidentally falling through to the fetch path."""
        empty_cache = tmp_path / "empty-cache"
        empty_cache.mkdir()
        _, manifest, _ = synthetic_cache

        result = _run(
            "--verify-only",
            "--cache", str(empty_cache),
            "--manifest", str(manifest),
        )
        assert result.returncode == 1
        out = result.stderr
        assert "missing from cache" in out
        # And the cache must stay empty — nothing was downloaded.
        assert not any(empty_cache.rglob("*.tar.gz"))

    def test_help_advertises_verify_only(self):
        """`fetch_upstream.sh --help` mentions the verify-only mode so
        operators can discover it without reading the script."""
        result = _run("--help")
        assert result.returncode == 0
        # Help goes to stderr per the script's convention.
        out = result.stderr
        assert "--verify-only" in out
        assert "make verify-recovery" in out


@pytest.mark.skipif(
    not shutil.which("make"),
    reason="GNU make not on PATH",
)
class TestMakeVerifyRecoveryTarget:
    """The Makefile target wires fetch_upstream.sh --verify-only.

    Smoke-test that `make verify-recovery` is registered and runnable;
    we don't actually invoke it against the live cache (which may or
    may not exist on the CI host).
    """

    def test_target_is_declared_phony(self):
        """`make verify-recovery` must be declared .PHONY so it always
        runs regardless of any file named verify-recovery."""
        makefile = (REPO_ROOT / "Makefile").read_text()
        assert "verify-recovery" in makefile
        # On the .PHONY line specifically.
        phony_line = next(
            line for line in makefile.splitlines() if line.startswith(".PHONY:")
        )
        assert "verify-recovery" in phony_line

    def test_target_invokes_verify_only_flag(self):
        """The make target body invokes fetch_upstream.sh with
        --verify-only (not the default download mode)."""
        makefile = (REPO_ROOT / "Makefile").read_text()
        # Find the recipe under the verify-recovery: line.
        in_recipe = False
        recipe_lines: list[str] = []
        for line in makefile.splitlines():
            if line.startswith("verify-recovery:"):
                in_recipe = True
                continue
            if in_recipe:
                if line.startswith("\t"):
                    recipe_lines.append(line.strip())
                else:
                    break
        body = " ".join(recipe_lines)
        assert "fetch_upstream.sh" in body
        assert "--verify-only" in body
