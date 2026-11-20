"""Heir-facing text uses the glossary's terms (#933).

CONTEXT.md names what an heir meets: a backup set (not a repository),
a data disc, the tools folder, the restore folder, the meta disc.  This
test reads the text an heir reads -- the walkthroughs on the meta disc
(RESTORE*.txt, README.txt, BOOT.txt, README_RESTORE.md, the README the
meta builder writes), the restore scripts' messages and the card text --
and fails on:

* an avoided phrase in prose (the hand-kept list below);
* a name #934 retired (tests/contract/renames.py), anywhere in that
  text, except in the "renamed" message that refuses it.

Command lines are not prose: a line indented by 4 or more spaces, or
holding a flag (``--name``) or the words rustic or restic, may say
"repository", because that is what rustic and restic call it.  The
technical reference documents (FORMAT.txt, CRYPTO.txt, BUILD.txt,
TIERS.txt, AUDIT.md) are not heir walkthroughs and are not read here:
there "repository" names restic's format (owner decision 2026-11-20).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.contract.renames import RENAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "recovery" / "docs"
SCRIPTS = REPO_ROOT / "recovery" / "scripts"

#: Phrases CONTEXT.md says to avoid in heir-facing text (the "_Avoid_"
#: lines of Backup set, Tools folder, Restore folder and Meta disc).
#: Hand-kept: add a phrase here when the glossary adds one.
AVOIDED = (
    r"repositor(?:y|ies)",   # Backup set
    r"work dir",             # Tools folder
    r"work folder",          # Tools folder
    r"RAM copy",             # Tools folder
    r"destination folder",   # Restore folder
    r"output dir",           # Restore folder
    r"recovery disc",        # Meta disc
    r"rescue disc",          # Meta disc
    r"meta-volume",          # Meta disc
)
_AVOIDED = re.compile(r"\b(" + "|".join(AVOIDED) + r")\b", re.IGNORECASE)

#: Lines that may hold an avoided phrase: (file name, text on the line).
ALLOWED = (
    # The catalog's table is named repositories; this is SQL, not prose.
    ("restore.sh", "JOIN repositories r ON"),
)

#: The walkthrough files on the meta disc.
WALKTHROUGHS = (
    DOCS / "RESTORE.txt",
    DOCS / "RESTORE_WINDOWS.txt",
    DOCS / "RESTORE_STANDARD_TOOLS.txt",
    DOCS / "README.txt",
    DOCS / "BOOT.txt",
    REPO_ROOT / "src" / "lcsas" / "meta" / "assets" / "README_RESTORE.md",
    REPO_ROOT / "docs" / "PASSWORD_CARD.txt",
)
#: The scripts an heir runs: what they print is heir-facing.
HEIR_SCRIPTS = (SCRIPTS / "restore.sh", SCRIPTS / "restore.bat", SCRIPTS / "restore_auto.sh")
#: Python functions whose string literals are heir-facing text.
HEIR_FUNCTIONS = (
    (REPO_ROOT / "src" / "lcsas" / "meta" / "builder.py", {"_render_meta_start_here"}),
    (REPO_ROOT / "src" / "lcsas" / "cli" / "main.py",
     {"_share_card_text", "_recovery_card_text", "_estate_card_text"}),
)
#: Technical reference documents: exempt (see the module docstring).
REFERENCE = ("FORMAT.txt", "CRYPTO.txt", "BUILD.txt", "TIERS.txt", "AUDIT.md")

_FLAG = re.compile(r"(?:^|[\s(`'\"=])--[a-z]")
_TOOL = re.compile(r"\b(?:rustic|restic)\b")


def _is_command_line(line: str) -> bool:
    return line.startswith("    ") or bool(_FLAG.search(line)) or bool(_TOOL.search(line))


def _script_lines(path: Path) -> list[tuple[int, str]]:
    """The lines of a script that are not comments."""
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        s = line.strip()
        if s.startswith(("#", "::")) or re.match(r"(?i)rem(\s|$)", s):
            continue
        out.append((n, line))
    return out


def _function_literals(path: Path, names: set[str]) -> list[tuple[int, str]]:
    """The lines of every string literal inside the named functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.extend((sub.lineno + i, ln)
                               for i, ln in enumerate(sub.value.splitlines()))
    return out


def _heir_text() -> list[tuple[str, bool, list[tuple[int, str]]]]:
    """(name, is_code, lines) for every heir-facing source."""
    found: list[tuple[str, bool, list[tuple[int, str]]]] = []
    for path in WALKTHROUGHS:
        lines = list(enumerate(path.read_text(encoding="utf-8").splitlines(), 1))
        found.append((path.name, False, lines))
    for path in HEIR_SCRIPTS:
        found.append((path.name, True, _script_lines(path)))
    for path, names in HEIR_FUNCTIONS:
        found.append((path.name, True, _function_literals(path, names)))
    return found


def _allowed(name: str, line: str) -> bool:
    return any(name == f and text in line for f, text in ALLOWED)


def test_the_heir_text_was_read() -> None:
    """The sources exist and hold text, or the checks below pass on nothing."""
    for name, _code, lines in _heir_text():
        assert len(lines) > 20, f"{name}: {len(lines)} lines read"
    for ref in REFERENCE:
        assert not any(p.name == ref for p in WALKTHROUGHS)


def test_heir_prose_avoids_the_glossary_s_avoided_phrases() -> None:
    problems = []
    for name, code, lines in _heir_text():
        for n, line in lines:
            # A script's lines are all indented code; there a command
            # line is one holding a flag or naming rustic or restic.
            if code:
                if _FLAG.search(line) or _TOOL.search(line):
                    continue
            elif _is_command_line(line):
                continue
            if _allowed(name, line):
                continue
            for m in _AVOIDED.finditer(line):
                problems.append(f"{name}:{n}: {m.group(0)!r} in {line.strip()!r}")
    assert not problems, (
        "heir-facing prose uses a phrase CONTEXT.md says to avoid "
        "(say backup set, tools folder, restore folder, meta disc):\n"
        + "\n".join(problems))


#: Retired from the scripts but still another program's flag: tier 1's,
#: the Python reader's and lcsas's --repo DIR, restic's --target.  The
#: scripts' refusals are pinned by test_restore_renamed_names.py.
_STILL_USED_ELSEWHERE = frozenset({"--repo", "--target"})


def _retired_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """A word-bounded pattern for each retired name an heir could meet.

    Fields (set=, target=, tenant=, key_file) and owner config keys are
    left out: they are code words that appear in the scripts for other
    reasons; their renames have their own tests.
    """
    out = []
    for r in RENAMES:
        if r.kind in ("field", "config") or r.old in _STILL_USED_ELSEWHERE:
            continue
        out.append((r.old, re.compile(r"(?<![\w.-])" + re.escape(r.old) + r"(?![\w-])")))
    return out


def test_heir_text_names_no_retired_name() -> None:
    patterns = _retired_patterns()
    problems = []
    for name, code, lines in _heir_text():
        for n, line in lines:
            if re.search(r"\b(?:renamed|removed)\b", line):
                continue  # the message that refuses a retired name
            for old, pat in patterns:
                if code and old.startswith(("--", "LCSAS_", "TARGET_DIR", "SNAPSHOT_ID")):
                    continue  # the scripts' refusal code reads these
                if pat.search(line):
                    problems.append(f"{name}:{n}: {old!r} in {line.strip()!r}")
    assert not problems, (
        "heir-facing text names a name retired by #934 (see "
        "tests/contract/renames.py for its replacement):\n" + "\n".join(problems))


@pytest.mark.parametrize("line", [
    "The repository password unlocks it.",
    "Copy it to the work folder first.",
    "Pick a destination folder.",
])
def test_the_check_bites(line: str) -> None:
    assert _AVOIDED.search(line)
    assert not _is_command_line(line)


@pytest.mark.parametrize("line", [
    "    restic -r /mnt/repo restore latest",
    "  rustic repository on the disc",
    "  sh restore.sh --restore-folder ~/restored",
])
def test_command_lines_are_exempt(line: str) -> None:
    assert _is_command_line(line)
