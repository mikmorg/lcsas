"""Tier 3 restores a REAL rustic repository through native AES (#774).

A real ``rustic`` repository (zstd-compressed, rustic's own key file and
pack layout) is restored by the generated ``standalone_restorer.py``
with each native AES-256-CTR backend this host has: the libcrypto
library through ctypes and the ``openssl`` command.  The restored files
must be byte-identical to the source, and the reader must say in one
log line which backend it used.  The pure backend on the same
repository is covered by ``test_pure_python_restore.py``.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.restore._aes_fast import select_aes_ctr_backend
from lcsas.restore.standalone_builder import build_standalone

pytestmark = [pytest.mark.integration, pytest.mark.requires_rustic]

PASSWORD = "tier3-fast-aes-test-pw\n"


def _rustic(repo: Path, pw: Path, *args: str, cwd: Path | None = None) -> None:
    proc = subprocess.run(
        ["rustic", "-r", str(repo), "--password-file", str(pw), *args],
        capture_output=True, text=True, cwd=cwd, timeout=300, check=False,
    )
    assert proc.returncode == 0, proc.stderr


def _make_source(root: Path) -> None:
    """Random (incompressible) files, compressible text, and small files."""
    rng = random.Random(774)
    (root / "photos").mkdir(parents=True)
    (root / "docs" / "notes").mkdir(parents=True)
    for i, size in enumerate((900_000, 1_700_000)):
        (root / "photos" / f"p{i}.jpg").write_bytes(os.urandom(size))
    words = ["".join(rng.choice("abcdefghij") for _ in range(rng.randint(2, 9)))
             for _ in range(800)]
    (root / "docs" / "book.txt").write_text(
        " ".join(rng.choice(words) for _ in range(150_000)))
    for i in range(30):
        (root / "docs" / "notes" / f"n{i}.txt").write_text(
            "note " * rng.randint(1, 600))
    (root / "docs" / "empty.txt").write_bytes(b"")


@pytest.fixture(scope="module")
def rustic_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path, Path]:
    """(source dir, repository, password file, generated reader)."""
    base = tmp_path_factory.mktemp("tier3_fast_aes")
    src, repo, pw = base / "src", base / "repo", base / "pw.txt"
    _make_source(src)
    pw.write_text(PASSWORD)
    _rustic(repo, pw, "init")
    _rustic(repo, pw, "backup", ".", cwd=src)
    script = base / "standalone_restorer.py"
    script.write_text(build_standalone())
    return src, repo, pw, script


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.mark.parametrize("mode", ["ctypes", "openssl"])
def test_real_rustic_repo_restores_byte_identical(
    rustic_repo: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    src, repo, pw, script = rustic_repo
    monkeypatch.setenv("LCSAS_TIER3_AES", mode)
    if mode == "openssl" and shutil.which("openssl") is None:
        pytest.skip("no openssl command on this host")
    if mode == "ctypes" and select_aes_ctr_backend(False)[0].name != "ctypes":
        pytest.skip("no usable libcrypto for ctypes on this host")
    target = tmp_path / "restored"
    proc = subprocess.run(
        [sys.executable, str(script), "--repo", str(repo),
         "--password-file", str(pw), "--target", str(target),
         "--interactive", "off"],
        capture_output=True, text=True, timeout=900, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    said = [ln for ln in proc.stderr.splitlines() if "AES-256-CTR:" in ln]
    assert len(said) == 1, proc.stderr
    assert {"ctypes": "(ctypes)", "openssl": "the openssl command"}[mode] in said[0]
    assert _tree(target) == _tree(src)
