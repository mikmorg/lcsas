"""Contract gate: heir docs reference only real restore.sh flags [KEY-02].

Every ``--flag`` that the generated heir-facing docs (START_HERE.txt,
KEY_INFO.txt), the bundled ``keyshare_combine.py`` header, and
``docs/ESTATE_PLANNING.md`` attach to a ``restore.sh`` command must be
a flag that ``recovery/scripts/restore.sh`` actually accepts.  A
phantom flag silently became a positional argument before restore.sh
grew a strict ``-*)`` reject arm, producing an uninterpretable
snapshot-not-found failure for a non-technical heir who had already
done the hard part (reconstructing the password from share cards).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.staging.metadata import HolographicInjector

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
KEYSHARE_COMBINE = REPO_ROOT / "src" / "lcsas" / "meta" / "keyshare_combine.py"
ESTATE_PLANNING = REPO_ROOT / "docs" / "ESTATE_PLANNING.md"

# A long-option token: ``--`` followed by a letter (so the ``-----``
# separator rules in the docs never match).
_FLAG_RE = re.compile(r"--[a-z][a-z-]*")


def _accepted_flags() -> set[str]:
    """Parse restore.sh's flag-parsing case block for accepted flags."""
    text = RESTORE_SH.read_text(encoding="utf-8")
    # The flag parser is the first (and only) ``while [ $# -gt 0 ]``
    # loop; everything up to its ``done`` is the case block.
    m = re.search(r"while \[ \$# -gt 0 \]; do\n(.*?)\ndone\n", text, re.S)
    assert m is not None, "flag-parsing while-loop not found in restore.sh"
    block = m.group(1)
    # Case-arm patterns are ``--flag)`` / ``-h|--help)``.
    flags = set(re.findall(r"(--[a-z][a-z-]*)\)", block))
    assert "--help" in flags, f"sanity: parsed flag set looks wrong: {flags}"
    return flags


def _restore_sh_flag_refs(text: str) -> set[str]:
    """Every --flag token on a line that mentions restore.sh."""
    refs: set[str] = set()
    for line in text.splitlines():
        if "restore.sh" in line:
            refs.update(_FLAG_RE.findall(line))
    return refs


def _split_config(tmp_path: Path) -> LCSASConfig:
    return LCSASConfig(
        mirror_base_path=tmp_path / "mirror",
        staging_path=tmp_path / "staging",
        db_path=tmp_path / "db.db",
        key_split=True,
        key_threshold=2,
        key_shares=5,
        repositories={
            "family": RepositoryConfig(
                name="family",
                mirror_path=tmp_path / "mirror" / "family",
                password_file=Path("/keys/family.key"),
            ),
        },
    )


@pytest.fixture()
def rendered_docs(tmp_path: Path) -> dict[str, str]:
    """Render START_HERE.txt + KEY_INFO.txt with key_split=True."""
    root = tmp_path / "staging"
    root.mkdir()
    injector = HolographicInjector(root)
    config = _split_config(tmp_path)
    injector.write_start_here(config)
    injector.write_key_info(config)
    return {
        "START_HERE.txt": (root / "START_HERE.txt").read_text(
            encoding="utf-8"
        ),
        "KEY_INFO.txt": (root / "KEY_INFO.txt").read_text(encoding="utf-8"),
    }


def test_rendered_disc_docs_use_only_real_flags(
    rendered_docs: dict[str, str],
) -> None:
    accepted = _accepted_flags()
    for name, text in rendered_docs.items():
        refs = _restore_sh_flag_refs(text)
        assert refs, f"{name}: expected at least one restore.sh flag ref"
        phantom = refs - accepted
        assert not phantom, (
            f"{name} tells the heir to use restore.sh flag(s) {sorted(phantom)} "
            f"that restore.sh does not accept (accepted: {sorted(accepted)})"
        )


def test_rendered_split_blocks_use_mount_robust_command(
    rendered_docs: dict[str, str],
) -> None:
    """STEP 2 must be typeable from a freshly-mounted disc."""
    for name, text in rendered_docs.items():
        assert "sh /mnt/restore.sh --target ~/restored" in text, (
            f"{name}: split block must show the mount-robust "
            "'sh /mnt/restore.sh --target ~/restored' command"
        )


def test_keyshare_combine_header_uses_only_real_flags() -> None:
    accepted = _accepted_flags()
    refs = _restore_sh_flag_refs(
        KEYSHARE_COMBINE.read_text(encoding="utf-8")
    )
    assert refs, "keyshare_combine.py: expected a restore.sh usage line"
    phantom = refs - accepted
    assert not phantom, (
        f"keyshare_combine.py header references restore.sh flag(s) "
        f"{sorted(phantom)} that restore.sh does not accept "
        f"(accepted: {sorted(accepted)})"
    )


def test_estate_planning_doc_uses_only_real_flags() -> None:
    accepted = _accepted_flags()
    refs = _restore_sh_flag_refs(ESTATE_PLANNING.read_text(encoding="utf-8"))
    phantom = refs - accepted
    assert not phantom, (
        f"docs/ESTATE_PLANNING.md references restore.sh flag(s) "
        f"{sorted(phantom)} that restore.sh does not accept "
        f"(accepted: {sorted(accepted)})"
    )
