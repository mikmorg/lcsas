"""Hardening test (FMT-02): the dvdisaster RS03 source must be pinned,
bundled, and the spec must be re-implementable.

`docs/DVDISASTER_RS03_FORMAT.md` is the survivability artifact for the
day the dvdisaster binary no longer runs.  It is only re-implementable
against the *exact* source it transcribes, so that source has to travel
on the rescue disc — not at a dormant GitHub URL — and the spec has to
state offsets/layout definitively rather than punt to "consult the
source code".

This test is always-on (static + build-tree, like
`test_meta_bundling_completeness.py`).  It asserts:

  1. `recovery/UPSTREAM.sha256` pins a `dvdisaster/src/<tarball>` entry.
  2. The pinned tarball lands under `tools/src/` on a built meta tree.
  3. Removing the tarball from the cache makes the build fail loud.
  4. The spec carries the definitive-layout marker and none of the
     forbidden punts ("may vary", "consult the source code",
     "pip-installable").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lcsas.meta.builder import (
    MetaBuildError,
    MetaVolumeBuilder,
    pinned_dvdisaster_source_name,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = REPO_ROOT / "recovery" / "UPSTREAM.sha256"
SPEC = REPO_ROOT / "docs" / "DVDISASTER_RS03_FORMAT.md"

_FORBIDDEN = ("may vary", "consult the source code", "pip-installable")


def test_upstream_pins_dvdisaster_source() -> None:
    """A dvdisaster source tarball must be pinned by SHA-256."""
    assert UPSTREAM.is_file(), f"missing {UPSTREAM}"
    text = UPSTREAM.read_text(encoding="utf-8")
    rows = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
        and "dvdisaster/src/" in ln
    ]
    assert len(rows) == 1, (
        "expected exactly one dvdisaster/src/ row in UPSTREAM.sha256, "
        f"found {len(rows)}: {rows}"
    )
    sha, relpath = rows[0].split()[0], rows[0].split()[-1]
    assert re.fullmatch(r"[0-9a-f]{64}", sha), f"bad SHA-256: {sha!r}"
    assert relpath.endswith(".tar.gz"), f"not a tarball: {relpath!r}"

    name = pinned_dvdisaster_source_name(REPO_ROOT / "recovery")
    assert name == relpath.rsplit("/", 1)[-1]


def test_spec_is_definitive_and_unpunted() -> None:
    """The RS03 spec must claim definitiveness and drop the punts."""
    assert SPEC.is_file(), f"missing {SPEC}"
    text = SPEC.read_text(encoding="utf-8")

    # Definitive-layout marker: scoped to the pinned version + the
    # interleaving formula + the corrected GF generator polynomial.
    assert "definitive for dvdisaster 0.79" in text, (
        "spec must scope itself to the pinned dvdisaster version"
    )
    assert "RS03SectorIndex" in text, (
        "spec must give the layer/sector interleaving formula"
    )
    assert "0x187" in text, (
        "spec must state the correct GF(2^8) generator polynomial 0x187"
    )
    assert "tools/src/" in text, (
        "spec must point at the bundled source tarball, not a pip library"
    )

    lowered = text.lower()
    for phrase in _FORBIDDEN:
        assert phrase not in lowered, (
            f"spec still contains the forbidden punt {phrase!r}"
        )


def _seed_cache(cache_root: Path, name: str) -> Path:
    """Drop a stand-in tarball into the recovery cache layout."""
    dst_dir = cache_root / "dvdisaster" / "src"
    dst_dir.mkdir(parents=True, exist_ok=True)
    tarball = dst_dir / name
    tarball.write_bytes(b"stand-in dvdisaster source tarball")
    return tarball


def test_built_meta_tree_carries_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned tarball must land under tools/src/ on a built tree.

    Exercises only `_bundle_dvdisaster_source` (cheap) rather than the
    full `build()` (which bundles ~200 MB of tools).
    """
    name = pinned_dvdisaster_source_name(REPO_ROOT / "recovery")
    assert name is not None

    cache_root = tmp_path / "cache"
    _seed_cache(cache_root, name)
    monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache_root))

    output = tmp_path / "meta"
    output.mkdir()
    builder = MetaVolumeBuilder(output, project_root=REPO_ROOT)
    builder._bundle_dvdisaster_source()

    landed = output / "tools" / "src" / name
    assert landed.is_file(), f"dvdisaster source not bundled at {landed}"
    assert landed.read_bytes() == b"stand-in dvdisaster source tarball"


def test_missing_source_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the pinned tarball from the cache fails the build loud."""
    cache_root = tmp_path / "empty-cache"
    cache_root.mkdir()
    monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache_root))

    output = tmp_path / "meta"
    output.mkdir()
    builder = MetaVolumeBuilder(output, project_root=REPO_ROOT)
    with pytest.raises(MetaBuildError, match="dvdisaster source"):
        builder._bundle_dvdisaster_source()


def test_allow_flag_suppresses_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`allow_no_dvdisaster_source` lets the build proceed without it."""
    cache_root = tmp_path / "empty-cache"
    cache_root.mkdir()
    monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(cache_root))

    output = tmp_path / "meta"
    output.mkdir()
    builder = MetaVolumeBuilder(
        output, project_root=REPO_ROOT, allow_no_dvdisaster_source=True
    )
    builder._bundle_dvdisaster_source()  # must not raise

    name = pinned_dvdisaster_source_name(REPO_ROOT / "recovery")
    assert name is not None
    assert not (output / "tools" / "src" / name).exists()
