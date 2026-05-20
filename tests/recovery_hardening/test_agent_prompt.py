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


def test_agent_prompt_documents_media_mount_workflow() -> None:
    """The disc-swap loop must tell the agent to mount data discs at
    /media — NOT /mnt.

    Why: the recovery binary received `--meta-disc /mnt` at startup
    (so it could drop cwd outside /mnt before the operator umounts
    it).  Internally the binary's `path_under` check excludes the
    meta_disc path and every child from pack-search, FOREVER — even
    after the operator ejects the meta disc and inserts a data disc
    at the same mount point.  Mounting subsequent data discs at /mnt
    leaves them invisible to the binary, and the swap-prompt loop
    fires forever.

    The blind-restore run-fix2 agent figured this out on its own by
    trial and error (~10 wasted commands before it discovered
    `mount /dev/sr0 /media` works).  Pin /media in the prompt so
    future agents don't burn budget re-deriving it.

    What this catches: any refactor that drops the /media instruction
    or replaces it with /mnt.
    """
    text = _text()
    assert "/media" in text, (
        "agent_prompt.txt does not mention /media; the agent will mount "
        "data discs at /mnt (the meta-disc path), the binary will skip "
        "them via its path_under(/mnt) check, and the swap loop will "
        "stall.  Document the /media workflow in Step 4."
    )
    # The swap-loop section should explicitly chain the umount + insert
    # + mount /media pattern.  Look for both umount and a /media mount.
    assert "umount" in text and "mount /dev/sr0 /media" in text, (
        "agent_prompt.txt mentions /media but doesn't show the canonical "
        "swap-disc pattern (`umount /media` + `disc-loader insert` + "
        "`mount /dev/sr0 /media`).  Without all three, the agent may try "
        "shortcuts that don't refresh the kernel mount."
    )


def test_agent_prompt_disc_swap_polling_is_bounded() -> None:
    """The disc-swap polling pattern must be a BOUNDED for-loop with
    a break on tmux death — never an unbounded `until ...; do sleep
    ...; done`.

    Why: in blind-restore run-fix2, the agent followed the prior
    prompt's `until tmux capture-pane -t r -p | grep -qE ...; do
    sleep 3; done` pattern.  When the tmux session ended after the
    final restore completed, `tmux capture-pane` exited non-zero, the
    grep saw no input (returned 1), and the until-loop kept sleeping
    forever — orphaning a `sleep 3` child whose parent shell was the
    agent's `run_in_background` task wrapper.  Claude Code refused to
    finalize the session until the background task completed, so the
    blind-test outer harness hung past the 6-minute restore for
    another ~14 minutes before being manually killed.

    Fix: every polling block in the swap-loop section must be either
    (a) inside a `for _ in $(seq 1 N)` or `for i in 1 2 3 ...` outer
    cap that bounds the iteration count, or (b) include an `|| break`
    on the `tmux capture-pane` so a dead tmux exits the loop.  This
    test checks both signals.
    """
    text = _text()
    # The swap-loop section (between "Step 4" and the next major
    # section) is what we care about; restrict the check to it.
    step4_start = text.find("Step 4 — disc-swap LOOP")
    assert step4_start != -1, (
        "Step 4 disc-swap LOOP header missing from agent_prompt.txt; "
        "this test cannot scope its check."
    )
    # Heuristic: section ends at the next "Step 5" or at the WHAT YOU
    # MAY NOT DO header.  Use whichever comes first.
    step5_idx = text.find("Step 5", step4_start)
    whatnot_idx = text.find("WHAT YOU MAY NOT", step4_start)
    section_end = min(
        [i for i in (step5_idx, whatnot_idx, len(text)) if i > step4_start]
    )
    section = text[step4_start: section_end]

    # The bounded-form signal: a `for _ in $(seq 1 N)` or `for i in
    # 1 2 3 ...` somewhere in the swap section.
    bounded = (
        "for _ in $(seq" in section
        or "for i in $(seq" in section
        or "for _ in 1 2 3" in section
        or "for i in 1 2 3" in section
    )
    assert bounded, (
        "Step 4 swap loop must use a BOUNDED `for ... do ... done` "
        "for polling tmux output, not an unbounded `until`.  An "
        "unbounded until-loop orphans a sleep process when tmux dies, "
        "blocking session finalization for the full 45-min wall-clock "
        "cap.  See the run-fix2 retrospective."
    )
    # Defence-in-depth: the polling block should also `break` on
    # capture failure so a dead tmux exits the loop immediately
    # rather than waiting for the outer cap.
    assert "|| break" in section, (
        "Step 4 swap loop must `break` when `tmux capture-pane` "
        "exits non-zero (the canonical 'tmux session ended' signal).  "
        "Without this, every successful restore wastes the full "
        "outer-cap timeout per swap because the until/for body keeps "
        "polling a dead session."
    )
