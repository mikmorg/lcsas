"""Hardening tests #7 + #8: static guards on the blind-test setup.

Two regressions the blind-restore harness suffered until v2:

  • #7 — FIXTURE under /mnt got shadowed by the agent's legitimate
    `sudo mount /dev/sr0 /mnt`, which made verify.sh fail-open on the
    manifest comparison.  The fix moved FIXTURE to /var/lib/...;
    this test pins it.

  • #8 — the synthetic source tree (alpha/, bravo/) was left
    world-readable.  The blind agent walked the filesystem with
    `find / -path '*sources/alpha*'` and `cp`'d the original
    plaintext into ~/restored/ — a cheat that the v1 verify happily
    PASSed.  The fix added a chmod 0700 + chown root:root step
    after backup; this test pins that the step still exists.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore" / "setup.py"


def _setup_source() -> str:
    return SETUP.read_text()


# Paths the agent legitimately mounts.  FIXTURE must never live under
# any of these or it gets shadowed at mount-time.
SHADOWED_PREFIXES = (
    "/mnt",
    "/media",
    "/run/media",
    "/Volumes",
)


def test_fixture_path_not_under_shadowed_mount_point() -> None:
    src = _setup_source()
    m = re.search(r'^FIXTURE\s*=\s*Path\(\s*[\'"](?P<path>[^\'"]+)[\'"]\s*\)',
                  src, re.MULTILINE)
    assert m is not None, "FIXTURE assignment not found in setup.py"
    fixture_path = m.group("path")
    for prefix in SHADOWED_PREFIXES:
        assert not (
            fixture_path == prefix
            or fixture_path.startswith(prefix + "/")
        ), (
            f"FIXTURE = {fixture_path!r} lives under {prefix!r}, which "
            f"the agent legitimately mounts.  When the agent runs "
            f"`sudo mount /dev/sr0 {prefix}` (or similar) the fixture "
            f"directory becomes invisible and verify.sh's manifest "
            f"comparison fails open."
        )


def test_source_tree_locked_after_backup() -> None:
    """setup.py must lock SOURCES to root:root 0700 after backup.

    Without this lock the blind agent can find the original plaintext
    under /var/lib/lcsas-blind-test/sources/ and cp-cheat instead of
    using restore.sh.
    """
    src = _setup_source()
    # We look for the canonical sequence: a banner mentioning "lock"
    # plus a chmod-on-SOURCES line.  Defensive against simple drift,
    # but strict enough that deleting the entire step trips the gate.
    has_lock_banner = bool(
        re.search(r'banner\(["\'][^"\']*lock\s+source[^"\']*["\']\)',
                  src, re.IGNORECASE)
    )
    has_chmod_sources = bool(
        re.search(r'os\.chmod\(\s*SOURCES\b', src)
    )
    has_chown_sources = bool(
        re.search(r'os\.chown\(\s*SOURCES\b\s*,\s*0\s*,\s*0\s*\)', src)
    )
    has_walk = bool(
        re.search(r'SOURCES\.rglob\(', src)
    )

    missing = []
    if not has_lock_banner:
        missing.append('a "lock source tree" banner step')
    if not has_chmod_sources:
        missing.append('os.chmod(SOURCES, ...) call')
    if not has_chown_sources:
        missing.append('os.chown(SOURCES, 0, 0) call')
    if not has_walk:
        missing.append('SOURCES.rglob walk to recurse into children')

    assert not missing, (
        "setup.py is missing the source-tree lockdown step: "
        + ", ".join(missing)
        + ". Without it the blind agent can read the pre-disaster "
          "plaintext directly and bypass restore.sh entirely."
    )


def test_fixture_is_owner_traversable_only() -> None:
    """setup.py must chmod the FIXTURE root so lcsas-blind cannot
    list its contents directly (0710 root:cdemu).

    The fixture's root permissions are the second line of defence
    after the source-tree lock above; together they keep the agent
    blind to the fixture layout.
    """
    src = _setup_source()
    # chmod(FIXTURE, 0o710) — read+exec for owner, exec-only for group
    m = re.search(r'os\.chmod\(\s*FIXTURE\s*,\s*0o([0-7]+)\s*\)', src)
    assert m is not None, (
        "setup.py does not call os.chmod(FIXTURE, 0o710) — without "
        "tightening, the fixture root is world-traversable and the "
        "agent can `ls /var/lib/lcsas-blind-test/`."
    )
    mode = int(m.group(1), 8)
    assert mode & 0o007 == 0, (
        f"FIXTURE mode {oct(mode)} grants other-side access; should be "
        f"0o710 (root rwx, group exec-only, other none)."
    )
