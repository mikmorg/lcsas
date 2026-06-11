"""
test_boot_docs_reality.py -- docs-vs-reality contract gate for `lcsas ...`
invocations in the on-disc recovery documentation (BOOT-01; recovery-docs
leg of the UX-02 contract).

FAILURE MODE CAUGHT
-------------------
recovery/docs/BOOT.txt shipped the command
`lcsas meta build --output meta/ --recovery-boot` on every burned meta disc
for a long time, but `--recovery-boot` never existed in the argparse tree.
Similarly, READINESS_CHECKLIST.txt documented `--snapshot`/`--target` flags
on `restore exec` that are actually positionals.  An heir following the
on-disc docs decades from now gets `error: unrecognized arguments` with no
hint the documented flag is fictional.

This gate extracts every `lcsas <subcommand> [flags]` invocation from
recovery/docs/*.txt and validates each subcommand path and each long flag
against the real parser built by lcsas.cli.main.build_parser().  Values,
positionals, placeholders (<...>), and ellipses are intentionally not
validated — the contract is "no phantom subcommands, no phantom flags".

Tests are static text extraction + in-process argparse introspection — no
subprocesses, no optical hardware.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from lcsas.cli.main import build_parser

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS_DIR = REPO_ROOT / "recovery" / "docs"

# An invocation starts at a word-boundary `lcsas` (not lcsas-restore, not a
# path component) followed by at least one argument token.
_INVOCATION_RE = re.compile(r"(?<![\w/.-])lcsas((?:[ \t]+\S+)+)")


def _subparser_actions(
    parser: argparse.ArgumentParser,
) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    return {
        opt for action in parser._actions for opt in action.option_strings
    }


def _extract_invocations(text: str) -> list[tuple[int, list[str]]]:
    """Return (line_number, tokens) for each `lcsas ...` invocation."""
    found: list[tuple[int, list[str]]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _INVOCATION_RE.finditer(line):
            tail = match.group(1)
            # Inline-code spans: stop at the closing backtick so prose
            # after "`lcsas recovery build ...` already plumbs" is ignored.
            tail = tail.split("`", 1)[0]
            tokens = tail.split()
            if tokens:
                found.append((lineno, tokens))
    return found


def _validate(tokens: list[str]) -> list[str]:
    """Validate one tokenized invocation; return a list of problems."""
    problems: list[str] = []
    parser = build_parser()
    # Flags documented mid-line may legally belong to any parser on the
    # subcommand path, including the root parser (e.g. `--db`).
    allowed_flags = _option_strings(parser)

    # Walk the subcommand path as deep as the tokens allow.  Root-parser
    # flags may legally precede the first subcommand (`lcsas --db X burn`),
    # so skip them (and their value tokens) while still validating them.
    choices = _subparser_actions(parser)
    consumed_path: list[str] = []
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token in choices:
            parser = choices[token]
            consumed_path.append(token)
            allowed_flags |= _option_strings(parser)
            choices = _subparser_actions(parser)
            idx += 1
            continue
        if consumed_path or not re.match(r"^--?[A-Za-z]", token):
            break
        flag = token.split("=", 1)[0].rstrip(".,;:)")
        if flag not in allowed_flags:
            problems.append(
                f"flag '{flag}' does not exist on the root lcsas parser"
            )
        idx += 1
        # Skip the flag's value token (`--db <catalog>`), but never a
        # token that is itself a flag or a subcommand.
        if (
            "=" not in token
            and idx < len(tokens)
            and tokens[idx] not in choices
            and not tokens[idx].startswith("-")
        ):
            idx += 1

    if not consumed_path and idx == 0:
        problems.append(
            f"'{tokens[0]}' is not an lcsas subcommand"
        )
        return problems

    for token in tokens[idx:]:
        if not token.startswith("-") or not re.match(r"^--?[A-Za-z]", token):
            continue  # value, positional, placeholder, or `...`
        flag = token.split("=", 1)[0].rstrip(".,;:)")
        if flag not in allowed_flags:
            problems.append(
                f"flag '{flag}' does not exist on "
                f"'lcsas {' '.join(consumed_path)}' (or the root parser)"
            )
    return problems


def _doc_files() -> list[Path]:
    files = sorted(_DOCS_DIR.glob("*.txt"))
    assert files, f"no .txt docs found under {_DOCS_DIR}"
    return files


def test_recovery_docs_lcsas_invocations_exist_in_argparse_tree():
    """Every `lcsas ...` invocation in recovery/docs/*.txt must use only
    subcommands and long flags that the real CLI accepts."""
    failures: list[str] = []
    for doc in _doc_files():
        text = doc.read_text(encoding="utf-8")
        for lineno, tokens in _extract_invocations(text):
            for problem in _validate(tokens):
                failures.append(
                    f"{doc.relative_to(REPO_ROOT)}:{lineno}: "
                    f"`lcsas {' '.join(tokens)}` -- {problem}"
                )
    assert not failures, (
        "recovery docs reference lcsas invocations that the real CLI "
        "rejects (phantom subcommand or flag):\n  " + "\n  ".join(failures)
    )


def test_validator_accepts_root_flags_before_subcommand():
    """`lcsas --db <catalog> burn --media MDISC100 ...` is a legal shape —
    --db is a root-parser flag consumed before the subcommand (regression:
    checkpoint A, 2026-06; PHYSICAL_DISC_VALIDATION.txt:59 is correct)."""
    assert _validate(["--db", "<catalog>", "burn", "--media", "MDISC100", "..."]) == []
    assert _validate(["--verbose", "status"]) == []
    # Phantom root flags are still caught.
    assert _validate(["--no-such-flag", "burn"])


def test_extractor_sees_known_invocations():
    """Self-check: the extractor must find the known-good invocations, so a
    regex regression cannot silently turn the gate into a no-op."""
    corpus = "\n".join(
        doc.read_text(encoding="utf-8") for doc in _doc_files()
    )
    total = len(_extract_invocations(corpus))
    assert total >= 3, (
        f"extractor found only {total} `lcsas ...` invocations across "
        "recovery/docs/*.txt; expected at least 3 (status, restore exec, "
        "burn). The extraction regex has likely regressed."
    )
