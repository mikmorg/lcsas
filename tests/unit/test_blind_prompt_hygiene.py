"""KEY-04: the docs-driven split-key blind prompt stays docs-driven.

The whole point of ``agent_prompt_split_docs.txt`` is that it names no
LCSAS command, path, or binary — the heir must derive everything from the
on-disc instructions.  If a spoon-feed token leaks back in, the gate
silently reverts to a scripted smoke (the failure mode KEY-04 fixes), so
``make gate`` enforces the prompt's hygiene here on every run.

The scripted prompt (``agent_prompt_split.txt``) is exempt — it is the
tooling smoke and is allowed to name commands.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_E2E_DIR = REPO_ROOT / "tests" / "e2e" / "cdemu_blind_restore"
_DOCS_PROMPT = _E2E_DIR / "agent_prompt_split_docs.txt"
_SCRIPTED_PROMPT = _E2E_DIR / "agent_prompt_split.txt"
_HYGIENE_PY = _E2E_DIR / "prompt_hygiene_check.py"

_spec = importlib.util.spec_from_file_location(
    "prompt_hygiene_check", _HYGIENE_PY
)
assert _spec is not None and _spec.loader is not None
_hygiene = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hygiene)


def test_docs_prompt_exists() -> None:
    assert _DOCS_PROMPT.is_file(), f"missing docs-driven prompt: {_DOCS_PROMPT}"


def test_docs_prompt_has_no_spoonfeed_tokens() -> None:
    hits = _hygiene.check_file(str(_DOCS_PROMPT))
    assert hits == [], f"docs-driven prompt leaks spoon-feed tokens: {hits}"


@pytest.mark.parametrize("token", _hygiene.FORBIDDEN_TOKENS)
def test_each_forbidden_token_absent(token: str) -> None:
    text = _DOCS_PROMPT.read_text(encoding="utf-8").lower()
    assert token.lower() not in text


def test_mutation_bites() -> None:
    """Adding a forbidden token must make the check fail (the test bites)."""
    clean = _DOCS_PROMPT.read_text(encoding="utf-8")
    mutated = clean + "\n  sh /mnt/restore.sh ~/restored/ latest\n"
    hits = _hygiene.check_text(mutated)
    assert "restore.sh" in hits


def test_scripted_prompt_is_exempt() -> None:
    """The scripted smoke prompt is allowed to name commands — it should
    contain spoon-feed tokens, confirming the two prompts are distinct."""
    if not _SCRIPTED_PROMPT.is_file():
        pytest.skip("scripted prompt absent")
    hits = _hygiene.check_file(str(_SCRIPTED_PROMPT))
    assert hits, "scripted prompt unexpectedly has no command tokens"
