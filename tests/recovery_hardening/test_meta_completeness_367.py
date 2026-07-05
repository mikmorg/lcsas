"""test_meta_completeness_367.py -- tiered meta completeness (issue #367).

Per-repo metadata keys are RESTORE-BLOCKING (hard-required); stock restic
(tier-2b hedge) and lcsas-keyshare are RECOMMENDED (warn, not fail).
"""

from __future__ import annotations

from pathlib import Path

from lcsas.meta.builder import MetaVolumeBuilder
from lcsas.meta.required_contents import recommended_meta_paths


def _builder(out: Path) -> MetaVolumeBuilder:
    out.mkdir(exist_ok=True)
    return MetaVolumeBuilder(
        out, catalog_db_path=None, allow_no_dvdisaster_source=True,
    )


def test_repo_missing_keys_is_a_required_failure(tmp_path: Path) -> None:
    out = tmp_path / "meta"
    builder = _builder(out)
    # alpha carries its keys/, bravo does not.
    (out / "metadata" / "alpha" / "keys").mkdir(parents=True)
    (out / "metadata" / "bravo" / "index").mkdir(parents=True)

    missing = builder.missing_required_contents()
    assert "metadata/bravo/keys" in missing, (
        f"a repo without keys/ must be a required-completeness failure; "
        f"missing={missing}"
    )
    assert "metadata/alpha/keys" not in missing


def test_restic_and_keyshare_are_recommended_not_required(
    tmp_path: Path,
) -> None:
    out = tmp_path / "meta"
    builder = _builder(out)

    recommended = builder.missing_recommended_contents()
    # An empty output is missing every recommended path.
    assert set(recommended) == set(recommended_meta_paths())
    assert any("restic" in r for r in recommended)
    assert any("lcsas-keyshare" in r for r in recommended)

    # ...and none of restic / lcsas-keyshare leak into the REQUIRED set.
    required = builder.missing_required_contents()
    assert not any(
        "restic" in m or "lcsas-keyshare" in m for m in required
    ), f"restic/keyshare must not be hard-required; required={required}"
