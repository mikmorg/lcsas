"""The meta disc's restore scripts and README are byte-for-byte pinned (#689).

``restore_legacy.sh``, ``restore-auto.sh`` and ``README_RESTORE.md`` used to
be string constants in ``src/lcsas/meta/builder.py``.  #689 moved them to
``src/lcsas/meta/assets/``.  The SHA-256 values below were taken from a meta
tree built on master before the move; this test builds the same files from
the assets and compares every one, so the move (and any later change) can
not alter what lands on a disc without someone noticing.

These files are on the restore path, which is frozen per release.  If you
change an asset on purpose, update the hashes below in the same commit
(``sha256sum`` the file the test names) and say so in the PR: a new hash
means new bytes on every meta disc built afterwards.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lcsas.meta.builder import (
    RESTORE_AUTO_SCRIPT_PATH,
    RESTORE_LEGACY_SCRIPT_PATH,
    MetaVolumeBuilder,
)

_ASSETS = Path(__file__).resolve().parents[2] / "src" / "lcsas" / "meta" / "assets"

_LEGACY = "9025194f1c6aaf19d73e23c13f27641a82e384a9576adfcb676006ea6854c27c"
_AUTO = "058088c474870819b20ba780125ff3c0c8f85c71530502f2851cc457d248ff85"
_README_MD = "bdc9e0d204a43f3fb25f990df2dcb7ca6a36e848aa7174d4418753d97d4b8f70"
_README_TXT = "8df274161c420168a12ed9d26d4a1cb1f13f7b3911cca0aee4bbf595b439c99f"

# The asset files themselves.
_GOLDEN_ASSETS = {
    "restore_legacy.sh": _LEGACY,
    "restore-auto.sh": _AUTO,
    "README_RESTORE.md": _README_MD,
}

# Files the builder writes from the assets, by meta-disc path.  With the
# recovery/ tree bundled (the normal build) the legacy driver goes to
# recovery/scripts/; without it, it is the root restore.sh.
_GOLDEN_WITH_RECOVERY = {
    RESTORE_LEGACY_SCRIPT_PATH: _LEGACY,
    RESTORE_AUTO_SCRIPT_PATH: _AUTO,
    "README_RESTORE.md": _README_MD,
    "README_RESTORE.txt": _README_TXT,
}
_GOLDEN_NO_RECOVERY = {
    "restore.sh": _LEGACY,
    RESTORE_AUTO_SCRIPT_PATH: _AUTO,
    "README_RESTORE.md": _README_MD,
    "README_RESTORE.txt": _README_TXT,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_asset_files(builder: MetaVolumeBuilder) -> None:
    builder._write_restore_script()
    builder._write_restore_auto_script()
    builder._write_readme()
    builder._write_readme_txt()


def _assert_golden(root: Path, golden: dict[str, str]) -> None:
    assert golden, "golden list must not be empty"
    found = {
        rel: _sha256(root / rel) if (root / rel).is_file() else "missing"
        for rel in golden
    }
    wrong = {rel: have for rel, have in found.items() if have != golden[rel]}
    assert not wrong, f"meta-disc bytes differ from the pinned SHA-256: {wrong}"


def test_asset_files_match_golden() -> None:
    _assert_golden(_ASSETS, _GOLDEN_ASSETS)


def test_meta_tree_without_recovery_matches_golden(tmp_path: Path) -> None:
    out = tmp_path / "meta"
    out.mkdir()
    builder = MetaVolumeBuilder(out, recovery_dir=tmp_path / "no-recovery")
    _write_asset_files(builder)
    _assert_golden(out, _GOLDEN_NO_RECOVERY)


def test_meta_tree_with_recovery_matches_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal build: the recovery/ tree is bundled, and the two bash
    scripts get rows in recovery/MANIFEST.sha256 that must match too."""
    out = tmp_path / "meta"
    out.mkdir()
    cache = tmp_path / "empty-cache"  # no upstream binaries: keeps it small
    cache.mkdir()
    monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache))
    builder = MetaVolumeBuilder(out)
    builder._bundle_recovery_toolchain_artifacts()
    if not (out / "recovery" / "scripts" / "restore.sh").is_file():
        pytest.skip("no recovery/ tree in this checkout")
    _write_asset_files(builder)
    _assert_golden(out, _GOLDEN_WITH_RECOVERY)

    rows: dict[str, str] = {}
    for line in (out / "recovery" / "MANIFEST.sha256").read_text().splitlines():
        parts = line.strip().split("  ", 1)
        if len(parts) == 2:
            rows[parts[1]] = parts[0]
    for rel, want in ((RESTORE_LEGACY_SCRIPT_PATH, _LEGACY), (RESTORE_AUTO_SCRIPT_PATH, _AUTO)):
        key = "./" + rel.removeprefix("recovery/")
        assert rows.get(key) == want, f"recovery/MANIFEST.sha256 row for {key}"
