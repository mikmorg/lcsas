"""The host-A image's rustic pin must equal the tier-2 recovery pin (#428).

``docker/Dockerfile.host-a`` hand-copies the rustic version, tarball name
and SHA-256 that ``recovery/UPSTREAM.sha256`` already pins.  The two agreed
when written and nothing enforced it, which is the unenforced-duplication
rot pattern catalogued in the 2026-07 docs audit: two copies of one fact,
no gate.

The agreement is load-bearing, not cosmetic.  The Dockerfile's own comment
says why: host-A writes the mirror with the *same* rustic the tier-2
recovery reader is bundled with, because the config default
``allow_unverified_repo_format=false`` makes ``lcsas burn`` refuse to
master a disc from a repo it cannot prove readable by the pinned readers.
Bump ``UPSTREAM.sha256`` alone and the drift is silent at image-build time
and surfaces at recovery time — the worst possible moment.

These tests read both files and compare.  They never write either: an
intentional rustic bump means updating the Dockerfile too, which is the
whole point.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.host-a"
UPSTREAM = REPO_ROOT / "recovery" / "UPSTREAM.sha256"

# The triple host-A builds for.  UPSTREAM.sha256 pins rustic for several
# targets; only this one is what the image downloads.
TARGET = "x86_64-unknown-linux-musl"


def _dockerfile_args() -> dict[str, str]:
    """The ARG values host-A uses to fetch and verify rustic."""
    text = DOCKERFILE.read_text()
    args: dict[str, str] = {}
    for key in ("RUSTIC_VERSION", "RUSTIC_TARBALL", "RUSTIC_SHA256"):
        m = re.search(rf"^ARG\s+{key}=(\S+)\s*$", text, re.MULTILINE)
        if m:
            args[key] = m.group(1)
    return args


def _upstream_rustic_row() -> tuple[str, str]:
    """``(sha256, tarball_name)`` for this target's rustic in UPSTREAM."""
    for line in UPSTREAM.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != 2:
            continue
        sha, path = parts
        if path.startswith(f"rustic/{TARGET}/") and path.endswith(".tar.gz"):
            return sha.lower(), path.rsplit("/", 1)[-1]
    raise AssertionError(
        f"no rustic row for {TARGET} in {UPSTREAM} — the pin this gate "
        "compares against has moved or been removed"
    )


@pytest.mark.skipif(
    not DOCKERFILE.is_file(), reason="docker/Dockerfile.host-a not present"
)
class TestDockerfileRusticPinParity:
    def test_all_three_args_are_present(self):
        """A renamed or deleted ARG must fail loudly rather than let the
        comparisons below silently compare nothing."""
        args = _dockerfile_args()
        assert set(args) == {
            "RUSTIC_VERSION", "RUSTIC_TARBALL", "RUSTIC_SHA256"
        }, f"expected all three rustic ARGs in {DOCKERFILE}, got {sorted(args)}"

    def test_sha256_matches_upstream(self):
        args = _dockerfile_args()
        upstream_sha, _tarball = _upstream_rustic_row()
        assert args["RUSTIC_SHA256"].lower() == upstream_sha, (
            f"{DOCKERFILE} pins rustic {args['RUSTIC_SHA256']} but "
            f"{UPSTREAM} pins {upstream_sha}. host-A must write the mirror "
            "with the same rustic the tier-2 recovery reader is bundled "
            "with, or `lcsas burn` will refuse the repo format at burn time."
        )

    def test_tarball_name_matches_upstream(self):
        args = _dockerfile_args()
        _sha, upstream_tarball = _upstream_rustic_row()
        assert args["RUSTIC_TARBALL"] == upstream_tarball, (
            f"{DOCKERFILE} downloads {args['RUSTIC_TARBALL']} but "
            f"{UPSTREAM} pins {upstream_tarball}."
        )

    def test_version_is_consistent_with_the_tarball_name(self):
        """RUSTIC_VERSION is interpolated into the download URL, so a
        version that disagrees with the tarball fetches a URL whose file
        cannot match the pinned digest."""
        args = _dockerfile_args()
        version = args["RUSTIC_VERSION"]
        assert f"rustic-v{version}-{TARGET}.tar.gz" == args["RUSTIC_TARBALL"], (
            f"RUSTIC_VERSION={version} does not match "
            f"RUSTIC_TARBALL={args['RUSTIC_TARBALL']}"
        )
        _sha, upstream_tarball = _upstream_rustic_row()
        assert f"v{version}" in upstream_tarball, (
            f"RUSTIC_VERSION={version} is not the version pinned in "
            f"{UPSTREAM} ({upstream_tarball})"
        )
