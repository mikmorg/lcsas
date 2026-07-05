"""test_supply_chain_372.py -- bundle-time upstream re-verification (#372).

MetaVolumeBuilder must re-hash every PRESENT cached upstream archive against
recovery/UPSTREAM.sha256 before staging, so a rotted/tampered cache entry
never reaches a burned disc — while a partial cache (absent archives) is
still a legitimate single-arch build.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lcsas.meta.builder import MetaBuildError, MetaVolumeBuilder


def test_bundle_time_reverify_rejects_corruption(tmp_path: Path) -> None:
    recovery_dir = tmp_path / "recovery"
    recovery_dir.mkdir()
    good = b"the-real-upstream-archive-bytes"
    good_hash = hashlib.sha256(good).hexdigest()
    rel = "rustic/x86_64-unknown-linux-musl/rustic-v0.tar.gz"
    (recovery_dir / "UPSTREAM.sha256").write_text(
        "# pinned upstream artifacts\n"
        f"{good_hash}  {rel}\n"
    )

    out = tmp_path / "meta"
    out.mkdir()
    builder = MetaVolumeBuilder(
        out, catalog_db_path=None, allow_no_dvdisaster_source=True,
        recovery_dir=recovery_dir,
    )

    cache = tmp_path / "cache"
    archive = cache / rel
    archive.parent.mkdir(parents=True)

    # Corrupted cache entry → refuse to bundle.
    archive.write_bytes(b"tampered")
    with pytest.raises(MetaBuildError, match="UPSTREAM.sha256"):
        builder._verify_cached_upstream_archives(cache)

    # Intact cache entry → passes.
    archive.write_bytes(good)
    builder._verify_cached_upstream_archives(cache)

    # Absent archive (partial cache) → passes (not a corruption).
    archive.unlink()
    builder._verify_cached_upstream_archives(cache)
