"""FMT-01: the RS03 repair tool ships on every meta volume.

A meta disc that bundles ``lcsas-restore`` for all six targets but omits
``lcsas-ecc`` would give an heir the fail-loud half of the disc-integrity
layer (tier-1 rejects corrupt blobs) but not the repair half -- the
exact gap FMT-01 closes.  These tests pin that ``lcsas-ecc`` is a
REQUIRED per-target artifact: present + git-tracked in source, bundled
by an actual ``lcsas meta build``, and enforced by the completeness gate
so a missing one fails the build loudly rather than silently.

Complements ``test_meta_bundling_completeness.py`` (which proves the same
for lcsas-restore/rustic/CPython); kept as a dedicated FMT-01 file so the
repair-tool contract is discoverable on its own.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lcsas.meta.required_contents import APPROVED_TARGETS, required_meta_paths

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_BIN = REPO_ROOT / "recovery" / "bin"

# rust-triple -> (short-arch dir, ecc filename), mirroring builder.py's
# tier1_map short-arch convention.
_SHORT_ARCH = {
    "x86_64-unknown-linux-musl":     ("x86_64",        "lcsas-ecc"),
    "aarch64-unknown-linux-musl":    ("aarch64",       "lcsas-ecc"),
    "armv7-unknown-linux-gnueabihf": ("armv7",         "lcsas-ecc"),
    "aarch64-apple-darwin":          ("aarch64-macos", "lcsas-ecc"),
    "x86_64-apple-darwin":           ("x86_64-macos",  "lcsas-ecc"),
    "x86_64-pc-windows-gnu":         ("x86_64-windows", "lcsas-ecc.exe"),
}
_TARGETS = [(t, *_SHORT_ARCH[t]) for t in APPROVED_TARGETS]


@pytest.mark.parametrize(
    "rust_triple,short_arch,ecc",
    _TARGETS, ids=[t[0] for t in _TARGETS],
)
def test_ecc_source_present_and_tracked(
    rust_triple: str, short_arch: str, ecc: str
) -> None:
    rel = f"recovery/bin/{short_arch}/{ecc}"
    src = REPO_ROOT / rel
    assert src.is_file(), (
        f"no pre-built lcsas-ecc for {rust_triple} at {src}; "
        f"build with `make -C recovery ecc-arches`."
    )
    if shutil.which("git") is None or subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT, capture_output=True,
    ).returncode != 0:
        pytest.skip("not a git checkout")
    res = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert res.returncode == 0, f"{rel} is not git-tracked; fix: git add -f {rel}"


def test_required_contents_enforces_ecc_for_every_target() -> None:
    paths = set(required_meta_paths())
    for rust_triple, _short, _ecc in _TARGETS:
        rel = f"recovery/bin/{rust_triple}/{_ecc_name(rust_triple)}"
        assert rel in paths, (
            f"required_meta_paths() omits {rel}; the completeness gate "
            f"would not catch a meta disc missing the {rust_triple} "
            f"repair tool."
        )


def _ecc_name(rust_triple: str) -> str:
    return "lcsas-ecc.exe" if rust_triple == "x86_64-pc-windows-gnu" else "lcsas-ecc"


def test_built_meta_volume_bundles_ecc_for_present_targets(tmp_path: Path) -> None:
    """A real `lcsas meta build` copies lcsas-ecc under every target whose
    source binary is present, and the completeness gate reports a clean
    bundle (no missing required contents)."""
    from lcsas.meta.builder import MetaVolumeBuilder

    out = tmp_path / "meta"
    out.mkdir()
    builder = MetaVolumeBuilder(
        out, catalog_db_path=None, allow_no_dvdisaster_source=True,
    )
    builder.build()

    bundled_bin = out / "recovery" / "bin"
    for rust_triple, short_arch, ecc in _TARGETS:
        if not (RECOVERY_BIN / short_arch / ecc).is_file():
            continue
        assert (bundled_bin / rust_triple / ecc).is_file(), (
            f"meta build did not bundle lcsas-ecc for {rust_triple}"
        )

    # With all 6 ecc bins committed, the completeness gate must be clean
    # w.r.t. lcsas-ecc paths.
    missing = builder.missing_required_contents()
    ecc_missing = [m for m in missing if "lcsas-ecc" in m]
    assert ecc_missing == [], (
        f"completeness gate reports missing lcsas-ecc artifacts: {ecc_missing}"
    )
