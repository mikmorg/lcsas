"""The meta disc's README is byte-for-byte pinned (#689).

``README_RESTORE.md`` used to be a string constant in
``src/lcsas/meta/builder.py``; #689 moved it to ``src/lcsas/meta/assets/``.
Step 2 of #689 retired the two bash drivers that lived there
(``restore_legacy.sh``, ``restore-auto.sh``) and cut the README down to a
pointer to ``recovery/docs/RECOVER.txt``.  The SHA-256 values below pin
the README as the builder writes it, so a change can not alter what lands
on a disc without someone noticing.

These files are on the restore path, which is frozen per release.  If you
change the asset on purpose, update the hashes below in the same commit
(``sha256sum`` the file the test names) and say so in the PR: a new hash
means new bytes on every meta disc built afterwards.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lcsas.meta.builder import (
    _RETIRED_META_FILES,
    MetaBuildError,
    MetaVolumeBuilder,
)

_ASSETS = Path(__file__).resolve().parents[2] / "src" / "lcsas" / "meta" / "assets"

_README_MD = "2ad456e5829c7eff9ebbb460a7d9f8646a178dc9993983c0d3096dc18b0ce74a"
_README_TXT = "001d52f915762ffa8e7314eec027caeecc68fcfa68a7f74ce7ce7815d5d81c7d"

# The asset files themselves: README_RESTORE.md is the only one left.
_GOLDEN_ASSETS = {"README_RESTORE.md": _README_MD}

# Files the builder writes from the assets, by meta-disc path.
_GOLDEN_WITH_RECOVERY = {
    "README_RESTORE.md": _README_MD,
    "README_RESTORE.txt": _README_TXT,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    # #689: no shell script is kept as a Python package asset any more.
    assert sorted(p.name for p in _ASSETS.iterdir()) == ["README_RESTORE.md"]


def test_meta_tree_without_recovery_fails_loud(tmp_path: Path) -> None:
    """#689: with no recovery/ tree there is no restore driver to ship.
    The builder used to write the legacy bash driver as restore.sh; it
    now stops with a MetaBuildError, before writing anything."""
    out = tmp_path / "meta"
    builder = MetaVolumeBuilder(out, recovery_dir=tmp_path / "no-recovery")
    with pytest.raises(MetaBuildError, match="restore driver"):
        builder.build()
    assert not out.exists(), "the check must run before the slow bundling"

    out.mkdir()
    with pytest.raises(MetaBuildError, match="recovery/ tree"):
        builder._write_restore_script()
    assert not (out / "restore.sh").exists()


def test_meta_tree_with_recovery_matches_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal build: the recovery/ tree is bundled, restore.sh is the
    recovery/ driver, and none of the retired files is written."""
    out = tmp_path / "meta"
    out.mkdir()
    cache = tmp_path / "empty-cache"  # no upstream binaries: keeps it small
    cache.mkdir()
    monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache))
    builder = MetaVolumeBuilder(out)
    builder._bundle_recovery_toolchain_artifacts()
    if not (out / "recovery" / "scripts" / "restore.sh").is_file():
        pytest.skip("no recovery/ tree in this checkout")
    builder._write_restore_script()
    builder._write_readme()
    builder._write_readme_txt()
    _assert_golden(out, _GOLDEN_WITH_RECOVERY)

    assert "LCSAS_RESTORE_BUILD_SHA" in (out / "restore.sh").read_text()
    left = [rel for rel in _RETIRED_META_FILES if (out / rel).exists()]
    assert not left, f"retired files on the meta disc: {left}"
