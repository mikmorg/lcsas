"""The recovery docs quote only messages the recovery programs print (#809).

An heir reads RECOVER.txt, RECOVER_WINDOWS.txt and TIERS.txt and looks
for the words on the screen.  A quoted message the scripts no longer
print sends them looking for words that never come: RECOVER.txt quoted
restore.sh's old "noexec" report long after it changed, and its exit
code table quoted a question ("restore into RAM anyway?") the script
asks in other words.

Every quote of three words or more in those docs must be found in what
the recovery programs print: restore.sh and the other recovery scripts,
restore.bat, the tier-1 C sources and the Python reader (tier 3).  Not
checked: section titles (quoted to point at a section), paths and
option examples, and the messages of other programs (listed below with
whose they are).

How a quote is found: the docs write a changing part as "...", "<name>"
or a capital letter (N, K, X, M); the programs as %s, %d, !VAR!, %VAR%
or {expr}.  Both become a wildcard.  Whitespace is collapsed, so a
message printed over two lines matches a quote on one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"
DOCS = [RECOVERY / "docs" / n for n in
        ("RECOVER.txt", "RECOVER_WINDOWS.txt", "TIERS.txt")]

# Messages of other programs, and printed labels, quoted on purpose.
NOT_OURS = {
    "cannot be opened because the developer cannot be verified": "macOS",
    "Windows protected your PC": "Windows SmartScreen",
    "show extensions for known file types": "Windows Explorer option",
    "Windows Command Processor": "Windows",
    "lcsas-restore.exe is not recognized as an internal or external "
    "command.": "cmd.exe",
    "Add Python to PATH": "the Python installer",
    "No suitable key found": "rustic",
    "wrong password or no key found": "restic",
    "LCSAS KEY SHARE": "the heading printed on a share card",
    "BARE-MINIMUM PATH ENDS HERE": "a comment in restore.sh (TIERS.txt "
    "names the part of the script before it)",
}

# Messages a program builds from parts: each part must be in its source.
COMPOSED = {
    "AES-256-CTR: using the openssl command (/usr/bin/openssl, ...)": [
        "AES-256-CTR: using \x00", "the openssl command (\x00\x00)"],
}

_WILD = "\x00"
_QUOTE = re.compile(r'"([^"\n]+(?:\n[^"\n]+){0,3}?)"')


def _norm(text: str) -> str:
    return " ".join(text.split())


def _program_text(text: str) -> str:
    """Placeholders of the programs as wildcards, a plural "%s" after a
    word as "s" (pack%s: packs), bat escapes and \\n dropped."""
    text = text.replace("\\n", " ").replace("\\t", " ")
    text = re.sub(r"\^([()&<>|])", r"\1", text)
    text = re.sub(r"(?<=[a-z])%s", "s", text)
    text = re.sub(r"%[-0-9.]*l*[sdui]|![A-Za-z_]\w*!|%~?\w+%|%~\d", _WILD, text)
    return _norm(text)


_SH_PRINT = re.compile(
    r"\b(?:printf|echo)\s+(?:-\w+\s+)?(?:'([^']*)'|\"((?:[^\"\\]|\\.)*)\")")


def _shell_formats(src: str) -> str:
    """The printf/echo formats and here-document lines of a shell
    script, in order: what it prints, without its arguments."""
    out: list[str] = []
    heredoc: str | None = None
    for line in src.splitlines():
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            else:
                out.append(line)
            continue
        m = re.search(r"<<-?\s*'?(\w+)'?", line)
        if m and "cat" in line:
            heredoc = m.group(1)
        for m in _SH_PRINT.finditer(line):
            out.append(m.group(1) if m.group(1) is not None else m.group(2))
    return " ".join(out)


def _bat_echoes(src: str) -> str:
    out = []
    for line in src.splitlines():
        m = re.search(r"\becho[ .(](.*)$", line, re.IGNORECASE)
        if m:
            out.append(re.sub(r"\s*(?:1?>&2|>\s*nul|2>nul)\s*$", "", m.group(1)))
    return " ".join(out)


def _c_strings(src: str) -> str:
    """Adjacent C string literals are one string: joined with nothing."""
    return "".join(m.group(1) for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"', src))


def _py_strings(src: str) -> str:
    parts: list[tuple[int, int, str]] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.JoinedStr):
            text = "".join(str(v.value) if isinstance(v, ast.Constant) else _WILD
                           for v in node.values)
            parts.append((node.lineno, node.col_offset, str(text)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts.append((node.lineno, node.col_offset, node.value))
    return " ".join(t for _, _, t in sorted(parts))


def _code(src: str, comment: str) -> str:
    """The source without its comment lines: a quote that lives on only
    in a comment is not a message."""
    return "\n".join(line for line in src.splitlines()
                     if not line.lstrip().upper().startswith(comment))


def _corpus() -> list[str]:
    """What the recovery programs print, as several streams: each one
    joined in source order, and each source whole (a quote on one line)."""
    streams: list[str] = []
    for f in sorted((RECOVERY / "scripts").glob("*.sh")):
        src = _code(f.read_text(encoding="utf-8"), "#")
        streams += [_shell_formats(src), src]
    for f in sorted((RECOVERY / "scripts").glob("*.bat")):
        src = _code(f.read_text(encoding="utf-8"), "REM")
        streams += [_bat_echoes(src), src]
    for f in sorted((RECOVERY / "src").rglob("*.c")):
        streams.append(_c_strings(f.read_text(encoding="utf-8", errors="replace")))
    for f in sorted((REPO_ROOT / "src" / "lcsas" / "restore").glob("*.py")):
        streams.append(_py_strings(f.read_text(encoding="utf-8")))
    return [_program_text(s) for s in streams]


def _headings() -> set[str]:
    """Section titles of the recovery docs: lines at the margin with no
    lower-case letter (but in a program's name or in brackets), and
    markdown headings."""
    found: set[str] = set()
    for f in [*(RECOVERY / "docs").glob("*.txt"), *(REPO_ROOT / "docs").rglob("*.md")]:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if f.suffix == ".md":
                if line.startswith("#"):
                    found.add(line.lstrip("#").strip())
            elif line and not line[0].isspace():
                bare = re.sub(r"restore\.(?:sh|bat)|lcsas-[\w.]+|python|\([^)]*\)",
                              "", line)
                if not re.search(r"[a-z]", bare):
                    found.add(line.strip())
    return found


def _doc_quotes() -> list[tuple[str, int, str]]:
    quotes = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for m in _QUOTE.finditer(text):
            q = _norm(m.group(1))
            if len(q.split()) >= 3:
                quotes.append((doc.name, text.count("\n", 0, m.start()) + 1, q))
    return quotes


def _checked(q: str, headings: set[str]) -> bool:
    if q in NOT_OURS:
        return False
    if q.startswith("-") or re.match(r"^(?:[A-Za-z]:\\|[/~])", q):
        return False  # an option or a path, not a message
    return not any(h == q or h.startswith(q + " ") for h in headings)


def _pattern(q: str) -> re.Pattern[str]:
    parts = re.split(r"\.\.\.|<[^>]*>|\b[NKXM]\b", q)
    return re.compile(".{0,200}?".join(re.escape(p) for p in parts))


def _missing(quotes: list[tuple[str, int, str]]) -> list[str]:
    corpus = _corpus()
    headings = _headings()
    bad = []
    for doc, line, q in quotes:
        if not _checked(q, headings):
            continue
        if q in COMPOSED:
            if not all(any(_program_text(p) in s for s in corpus)
                       for p in COMPOSED[q]):
                bad.append(f'{doc}:{line}: "{q}" (its parts)')
            continue
        pat = _pattern(q)
        if not any(pat.search(s) for s in corpus):
            bad.append(f'{doc}:{line}: "{q}"')
    return bad


def test_quoted_messages_are_printed_by_the_programs() -> None:
    quotes = _doc_quotes()
    checked = [q for _, _, q in quotes if _checked(q, _headings())]
    # The extraction works: there are quotes, and most are checked.
    assert len(quotes) >= 60, len(quotes)
    assert len(checked) >= 30, checked
    missing = _missing(quotes)
    assert not missing, (
        "The recovery docs quote messages no recovery program prints.  "
        "Quote what the program prints now (or, for another program's "
        "message, add it to NOT_OURS):\n  " + "\n  ".join(missing))


@pytest.mark.parametrize("quote", [
    # Split over two printf lines in restore.sh.
    "the recovery programs are on this disc, but this computer will not run them",
    # A placeholder on both sides (%s; <...> or ...).
    "special file ... not created",
    # tier 1 C, a plural written as pack%s.
    "Total: N packs, X MB across M discs.",
    # restore.bat, with ^( ^) escapes.
    "The restore stopped before it finished (code N).",
])
def test_known_messages_are_found(quote: str) -> None:
    assert _missing([("t", 1, quote)]) == []


@pytest.mark.parametrize("quote", [
    # restore.bat's free-space question before #798.
    "That size counts all 2 backup sets on these discs",
    "restore into RAM anyway?",
    "Carry on anyway? [Y/n]",
])
def test_a_message_no_program_prints_is_caught(quote: str) -> None:
    assert _missing([("t", 1, quote)]) != []


def test_the_next_set_folder_example_matches_the_script() -> None:
    """#809: RECOVER.txt's example of the next set's folder is the one
    restore.sh offers: the first folder's name with -2."""
    sh = (RECOVERY / "scripts" / "restore.sh").read_text(encoding="utf-8")
    assert "_os_n=2" in sh and '"$_os_base-$_os_n"' in sh
    assert "~/restored-2)" in (RECOVERY / "docs" / "RECOVER.txt").read_text(
        encoding="utf-8")
