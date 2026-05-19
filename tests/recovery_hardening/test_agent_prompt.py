"""Hardening test: agent_prompt.txt staying current with lcsas-restore features.

The blind-restore agent reads agent_prompt.txt at the start of each run and
relies on it to know how to interact with lcsas-restore.  Before #103, new
features (LCSAS_PACK_CACHE_DIR, LCSAS_TIER_FALLBACK, disc-swap interaction,
progress lines) landed without updating the prompt.  The agent had to
re-derive them from scratch on each run, burning budget and risking wrong
behavior (e.g. treating a 60-second quiet stretch as a hang, or not knowing
to press ENTER after a disc swap).

These are static assertions — no binary is invoked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_PROMPT = (
    REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore" / "agent_prompt.txt"
)


def _text() -> str:
    return AGENT_PROMPT.read_text()


def test_agent_prompt_exists() -> None:
    """The file itself must exist — a missing prompt silences the agent."""
    assert AGENT_PROMPT.exists(), (
        f"agent_prompt.txt not found at {AGENT_PROMPT}"
    )


def test_agent_prompt_documents_pack_cache() -> None:
    """LCSAS_PACK_CACHE_DIR must be mentioned so the agent knows the cache
    is active and doesn't unset it while debugging disc-swap problems."""
    assert "LCSAS_PACK_CACHE_DIR" in _text(), (
        "agent_prompt.txt does not mention LCSAS_PACK_CACHE_DIR; "
        "the agent will not know the opportunistic pack cache is active "
        "and may disable it, causing 3-16x more disc swaps."
    )


def test_agent_prompt_documents_tier_fallback() -> None:
    """LCSAS_TIER_FALLBACK must be mentioned so the agent has a legitimate
    escape hatch when tier-1 crashes rather than improvising workarounds."""
    assert "LCSAS_TIER_FALLBACK" in _text(), (
        "agent_prompt.txt does not mention LCSAS_TIER_FALLBACK; "
        "the agent will not know the opt-in fallback exists and may "
        "resort to unauthorized workarounds (e.g. renaming binaries)."
    )


def test_agent_prompt_documents_progress_lines() -> None:
    """The prompt must tell the agent about periodic progress output so it
    doesn't mistake a slow-disc quiet stretch for a frozen process."""
    assert "progress" in _text(), (
        "agent_prompt.txt does not mention progress lines; "
        "the agent will treat silence as a hang and may abort a "
        "working restore."
    )


def test_agent_prompt_documents_enter_for_disc_swap() -> None:
    """The disc-swap interaction requires pressing ENTER; the prompt must
    document this so the agent knows what to do after inserting a disc."""
    assert "ENTER" in _text(), (
        "agent_prompt.txt does not mention pressing ENTER to acknowledge "
        "a disc swap; the agent will insert the disc but lcsas-restore "
        "will keep waiting indefinitely."
    )


def test_tmux_is_not_presented_as_required_to_real_operators() -> None:
    """tmux is a test-rig workaround, not a real-operator requirement.

    The agent_prompt.txt uses tmux only because the test agent's Bash tool
    is one-shot and cannot hold an interactive process open.  Real operators
    run restore.sh directly in a single terminal.  If agent_prompt.txt ever
    stops calling out this distinction, it risks misleading readers into
    thinking tmux is required for production recovery.

    This test verifies that any tmux reference in the file is accompanied
    by clear framing — either "test-rig" or "test harness" — within 500
    characters of the first tmux mention.  It also verifies that
    recovery/docs/RECOVER.txt (the real-operator guide) contains no tmux
    reference at all.
    """
    text = _text()

    # 1. agent_prompt.txt must contextualize tmux as a test-rig artifact.
    tmux_idx = text.find("tmux")
    assert tmux_idx != -1, (
        "agent_prompt.txt no longer mentions tmux at all — if tmux was "
        "removed entirely, also remove this guard and confirm no test-rig "
        "guidance is needed."
    )
    # Search a 500-char window starting 500 chars before the first tmux
    # reference so the NOTE block that precedes it is included.
    window_start = max(0, tmux_idx - 500)
    window = text[window_start: tmux_idx + 500].lower()
    flagged = (
        "test-rig" in window
        or "test rig" in window
        or "test-harness" in window
        or "test harness" in window
    )
    assert flagged, (
        "agent_prompt.txt mentions tmux but does not flag it as a "
        "test-rig or test-harness workaround within 500 characters of "
        "the first 'tmux' occurrence.  Add a NOTE explaining that real "
        "operators do not need tmux — see recovery/docs/RECOVER.txt for "
        "the real flow."
    )

    # 2. RECOVER.txt (the real-operator guide) must NOT mention tmux.
    recover_txt = (
        REPO_ROOT / "recovery" / "docs" / "RECOVER.txt"
    ).read_text(encoding="utf-8")
    assert "tmux" not in recover_txt, (
        "recovery/docs/RECOVER.txt mentions tmux.  The real-operator "
        "guide must not reference tmux — that is a test-rig artifact.  "
        "Remove any tmux references from RECOVER.txt."
    )
