"""Hardening test: ENV_VARS.txt inventory + opt-in/opt-out principle.

Without a documented principle, operators cannot predict which env-var
knobs are active by default and which require explicit opt-in.  The
v3 blind run surfaced this concretely: LCSAS_PACK_CACHE_DIR defaulted
ON (auto) and LCSAS_TIER_FALLBACK defaulted OFF — with no rationale
captured — so an operator reading source code would have no way to
know which was the intentional direction for each.

These static assertions catch:
  * recovery/docs/ENV_VARS.txt being deleted or moved.
  * The LCSAS_PACK_CACHE_DIR or LCSAS_TIER_FALLBACK entries being
    stripped from the inventory (closes #89).
  * The "Default" column being removed (operators lose the quick-
    reference table).
  * The opt-in/opt-out principle being removed (operators lose the
    rationale for each default choice).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_VARS_TXT = REPO_ROOT / "recovery" / "docs" / "ENV_VARS.txt"


def test_env_vars_txt_exists() -> None:
    """ENV_VARS.txt must exist at recovery/docs/ENV_VARS.txt."""
    assert ENV_VARS_TXT.is_file(), (
        f"recovery/docs/ENV_VARS.txt is missing.  This file documents "
        f"the opt-in/opt-out principle and the default for every "
        f"LCSAS_ operator knob.  Operators cannot predict restore "
        f"behavior without it.  Expected at: {ENV_VARS_TXT}"
    )


def test_env_vars_txt_contains_pack_cache() -> None:
    """ENV_VARS.txt must document LCSAS_PACK_CACHE_DIR."""
    content = ENV_VARS_TXT.read_text()
    assert "LCSAS_PACK_CACHE_DIR" in content, (
        "ENV_VARS.txt does not mention LCSAS_PACK_CACHE_DIR.  This "
        "variable controls the opportunistic pack cache that reduced "
        "the v3 blind run from 16 disc swaps to ~3 (one per disc).  "
        "Its opt-out default (ON by default) must be documented with "
        "a rationale so operators know why the cache is active by "
        "default and how to disable it."
    )


def test_env_vars_txt_contains_tier_fallback() -> None:
    """ENV_VARS.txt must document LCSAS_TIER_FALLBACK."""
    content = ENV_VARS_TXT.read_text()
    assert "LCSAS_TIER_FALLBACK" in content, (
        "ENV_VARS.txt does not mention LCSAS_TIER_FALLBACK.  This "
        "variable's opt-in default (OFF by default) is intentional: "
        "tier-1 crashes must surface immediately to avoid masking a "
        "tier-1 regression.  That rationale must be documented so "
        "operators don't accidentally flip it on in production."
    )


def test_env_vars_txt_has_default_column() -> None:
    """ENV_VARS.txt must contain a 'Default' section or column header."""
    content = ENV_VARS_TXT.read_text()
    assert "Default" in content, (
        "ENV_VARS.txt does not contain the word 'Default'.  The file "
        "must include a defaults column or per-variable default so "
        "operators can see at a glance which knobs are active without "
        "reading restore.sh source."
    )


def test_env_vars_txt_contains_optin_optout_principle() -> None:
    """ENV_VARS.txt must state the opt-in/opt-out principle."""
    content = ENV_VARS_TXT.read_text()
    has_optin = "opt-in" in content
    has_optout = "opt-out" in content
    assert has_optin or has_optout, (
        "ENV_VARS.txt does not contain the words 'opt-in' or 'opt-out'.  "
        "The documented principle — that behavior-changing features are "
        "opt-in unless forgetting them causes data loss or silent failure "
        "(in which case they are opt-out) — must appear in the file so "
        "operators understand the rationale behind each default choice "
        "and can predict how the restore script will behave without "
        "reading source code."
    )
