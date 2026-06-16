"""BOOT-07: nothing executable reaches a meta-volume tree unpinned.

The deleted Alpine live stack staged network-fetched, unpinned boot
artifacts (vmlinuz / initramfs / rootfs.squashfs, plus an executable
restore_wizard.py) onto the meta-volume — none of them appeared in
``recovery/MANIFEST.sha256`` or ``recovery/UPSTREAM.sha256``, violating
the doctrine that every shipped runtime artifact is pinned.  These
gates make that class of regression impossible to reintroduce quietly:

* every executable (and every boot-artifact-named file) in a built
  meta tree must either carry a row in the bundled pinning manifests
  or be one of the explicitly documented builder-authored scripts;
* no build code under ``src/lcsas/meta/`` may fetch from the network.

Extends the inventory approach of ``test_meta_bundling_completeness.py``;
``MetaVolumeBuilder._regenerate_recovery_manifest`` already rebuilds the
manifest at bundle time — these tests assert the *coverage* of that
manifest over what actually landed in the tree.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
META_SRC = REPO_ROOT / "src" / "lcsas" / "meta"

# Names that are boot artifacts no matter where they appear or what
# their mode bits say.  The Alpine stack staged exactly these.
_BOOT_NAME_RE = re.compile(
    r"^vmlinuz|^initramfs|\.squashfs$|\.efi$", re.IGNORECASE
)

# Builder-authored entry-point scripts, written at build time from
# reviewed in-repo source (heredocs in builder.py or verbatim copies
# of files under src/lcsas/ and manifest-pinned recovery/scripts/).
# They are text, not opaque artifacts — anything NOT in this list and
# not manifest-pinned fails the gate.
_AUTHORED_SCRIPTS = {
    "restore.sh",
    "restore-auto.sh",
    "restore_legacy.sh",
    "restore_c89.sh",
    "restore.bat",
    "keyshare_combine.py",
    "standalone_restorer.py",
}

# The borrowed-tools bundle: binaries + python stdlib copied from the
# *operator's own machine* at build time (never fetched), plus the
# helper scripts staged next to them.  A separate mechanism from the
# pinned recovery/ toolchain; exempt as a documented design decision.
_BORROWED_TOOLS_PREFIX = "tools/"


def _pinned_paths(tree: Path) -> set[str]:
    """Rows of the bundled pinning manifests, as path strings."""
    pinned: set[str] = set()
    for name in ("MANIFEST.sha256", "UPSTREAM.sha256"):
        manifest = tree / "recovery" / name
        if not manifest.is_file():
            continue
        for line in manifest.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            _digest, sep, path = stripped.partition("  ")
            if sep:
                pinned.add(path.lstrip("./"))
    return pinned


def _collect_unpinned(tree: Path) -> list[str]:
    """Every executable or boot-named regular file with no pinning row
    and no documented exemption."""
    pinned = _pinned_paths(tree)
    offenders: list[str] = []
    for path in sorted(tree.rglob("*")):
        if not path.is_file():
            continue
        boot_named = bool(_BOOT_NAME_RE.search(path.name))
        executable = os.access(path, os.X_OK)
        if not (boot_named or executable):
            continue
        rel = path.relative_to(tree).as_posix()
        if rel.startswith("recovery/"):
            # Must carry a row in the bundled manifests (the manifests
            # themselves use paths relative to recovery/).
            if rel[len("recovery/"):] in pinned:
                continue
        elif not boot_named:
            # Outside recovery/ only the documented exemptions pass —
            # and never for boot-artifact names, which must always be
            # manifest-pinned (the BOOT-07 lesson).
            if rel.startswith(_BORROWED_TOOLS_PREFIX):
                continue
            if rel in _AUTHORED_SCRIPTS:
                continue
        offenders.append(rel)
    return offenders


@pytest.fixture(scope="module")
def meta_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A full meta-volume tree built via the standard builder path."""
    if shutil.which("rustic") is None or shutil.which("xorriso") is None:
        pytest.skip("meta build requires rustic and xorriso on PATH")
    from lcsas.meta.builder import MetaVolumeBuilder

    out = tmp_path_factory.mktemp("boot07") / "meta_stage"
    out.mkdir()
    # allow_no_dvdisaster_source: this gate scans the staging tree for
    # unpinned EXECUTABLES; the dvdisaster source tarball (FMT-02) is neither
    # an executable nor what we test here, so don't require it in the cache
    # (build() raises MetaBuildError otherwise -- #322).
    MetaVolumeBuilder(
        out, catalog_db_path=None, allow_no_dvdisaster_source=True,
    ).build()
    return out


def test_no_unpinned_boot_artifacts(meta_tree: Path) -> None:
    """The built tree contains no unpinned executable / boot artifact."""
    offenders = _collect_unpinned(meta_tree)
    assert not offenders, (
        "meta-volume staging tree contains executables or boot "
        "artifacts with no row in recovery/MANIFEST.sha256 or "
        "recovery/UPSTREAM.sha256 and no documented exemption: "
        f"{offenders}.  Every shipped runtime artifact must be pinned "
        "(see plans/audit-2026-06/BOOT-07-remove-alpine-live-stack.md)."
    )


def test_detector_flags_planted_unpinned_executable(meta_tree: Path) -> None:
    """Prove the detector bites: plant an unmanifested executable and a
    boot-named file, assert both are flagged."""
    planted_exe = meta_tree / "evil-helper"
    planted_boot = meta_tree / "boot" / "vmlinuz"
    try:
        planted_exe.write_text("#!/bin/sh\nexit 0\n")
        planted_exe.chmod(0o755)
        planted_boot.parent.mkdir(parents=True, exist_ok=True)
        planted_boot.write_bytes(b"\x00" * 16)  # not even executable

        offenders = set(_collect_unpinned(meta_tree))
        assert "evil-helper" in offenders, (
            "detector missed a planted unmanifested executable — the "
            "gate is vacuous"
        )
        assert "boot/vmlinuz" in offenders, (
            "detector missed a planted boot-named artifact — the gate "
            "is vacuous"
        )
    finally:
        planted_exe.unlink(missing_ok=True)
        planted_boot.unlink(missing_ok=True)
        # Remove boot/ only if the plant created it empty.
        if planted_boot.parent.is_dir() and not any(
            planted_boot.parent.iterdir()
        ):
            planted_boot.parent.rmdir()


def test_no_network_fetch_in_meta_scripts() -> None:
    """No build code under src/lcsas/meta/ fetches from the network.

    The deleted Alpine build_rootfs.sh ran ``apk update && apk add``
    against Alpine 3.21 repos at build time.  Pinned fetches belong in
    ``recovery/scripts/fetch_upstream.sh`` (manifest-pinned, verified
    against recovery/UPSTREAM.sha256), never in the meta builder.  A
    line may opt out with a ``pinned-fetch-ok`` marker comment if it
    verifies against a pinning manifest.
    """
    tokens = ("apk ", "curl ", "wget ", "git clone")
    offenders: list[str] = []
    for path in sorted(META_SRC.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in {".pyc", ".pyo"} or "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(
            path.read_text(errors="replace").splitlines(), start=1
        ):
            if "pinned-fetch-ok" in line:
                continue
            for token in tokens:
                if token in line:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno}: {token.strip()}"
                    )
    assert not offenders, (
        "network-fetch tokens found in src/lcsas/meta/ — meta build "
        "scripts must not fetch from the network (unpinned artifacts "
        f"would reach burned discs): {offenders}"
    )
