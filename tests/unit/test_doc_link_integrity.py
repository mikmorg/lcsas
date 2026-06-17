"""Link-integrity gate: relative cross-references between docs must resolve.

The docs (``docs/**/*.md`` and ``recovery/docs/*.txt``) are burned onto
every meta-disc and followed by an heir decades later: the holographic
promise is that on-disc cross-references stay navigable.  A doc rename
silently rots every inbound link — e.g. ``docs/SURVIVABILITY.md`` moved
to ``docs/guides/survivability.md`` and the two docs that pointed at the
old name became dead ends.  Nothing caught it at commit time.

This gate extracts the relative-path references docs make to each other
and asserts every one resolves to a real file.  It is deliberately
conservative about what counts as a "link" so a false positive never
blocks a commit: it scans Markdown ``[text](target)`` links (explicit
navigation) and bare backtick path tokens that *explicitly* name the
doc tree (``docs/...`` / ``recovery/docs/...``).  Tokens that name
source/build trees, on-disc-only filenames, placeholders, or
``file:line`` citations are intentionally skipped — under-matching beats
flagging a non-link (issue #348).

Pure pathlib + regex; no subprocess, no external tools.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Markdown link target: the (...) half of [text](target).
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)")

# Inline-code span: `...`.
_BACKTICK = re.compile(r"`([^`]+)`")

# Schemes that are never a local file reference.
_EXTERNAL = ("http://", "https://", "mailto:", "ftp://")

# Bare tokens are only treated as doc links when they explicitly name the
# documentation tree — anything else (source paths, on-disc filenames,
# example fixtures) is too ambiguous to flag.
_DOC_PREFIXES = ("docs/", "recovery/docs/")


def _docs() -> list[Path]:
    """Every prose doc that may cross-reference another doc."""
    return sorted((REPO_ROOT / "docs").rglob("*.md")) + sorted(
        (REPO_ROOT / "recovery" / "docs").glob("*.txt")
    )


def _resolves(doc: Path, target: str) -> bool:
    """True if ``target`` resolves to a real file.

    The anchor (``#section``) is stripped first.  Resolution is tried
    both relative to the containing file's directory (standard Markdown
    semantics) and relative to the repo root, because the tree uses both
    conventions: ``[](../guides/x.md)`` is file-relative while a prose
    ``docs/x.md`` backtick token is repo-root-relative.  A link is broken
    only when *neither* convention finds the file.
    """
    target = target.split("#", 1)[0]
    if not target:
        return True  # pure anchor (#section) — same-doc, nothing to check
    # ``.exists()`` (not ``.is_file()``) so a link to a directory
    # (``workflows/``, ``../recovery/docs/``) resolves too.
    return (doc.parent / target).resolve().exists() or (
        REPO_ROOT / target
    ).resolve().exists()


def _md_link_targets(line: str) -> list[str]:
    """Local Markdown link targets on ``line`` (external URLs skipped)."""
    return [
        target
        for target in _MD_LINK.findall(line)
        if not target.startswith(_EXTERNAL)
        and not target.startswith("#")
        and "://" not in target
    ]


def _bare_doc_tokens(line: str) -> list[str]:
    """Backtick path tokens that explicitly reference the doc tree.

    Conservative on purpose (issue #348): only ``docs/...`` /
    ``recovery/docs/...`` tokens ending in ``.md``/``.txt``, with no
    placeholder/glob metacharacters and no ``file:line`` citation suffix.
    """
    tokens: list[str] = []
    for raw in _BACKTICK.findall(line):
        token = raw.strip()
        if re.search(r":\d", token):
            continue  # file:line citation, not a navigable link
        token = token.split("#", 1)[0]
        if not token.startswith(_DOC_PREFIXES):
            continue
        if any(ch in token for ch in "<>* "):
            continue  # placeholder / glob — not a literal path
        if not token.endswith((".md", ".txt")):
            continue
        tokens.append(token)
    return tokens


def test_doc_cross_references_resolve() -> None:
    """Every relative doc-to-doc reference must point at a real file.

    Red-first catch: ``docs/SURVIVABILITY.md`` was renamed to
    ``docs/guides/survivability.md``; the references in
    DISC_CONFIDENTIALITY.md and CROSS_PLATFORM_META_RFC.md, plus the
    ``formats/``-style links in README.md / architecture.md, all rotted
    silently (issue #348).
    """
    broken: list[str] = []
    for doc in _docs():
        rel = doc.relative_to(REPO_ROOT)
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for target in _md_link_targets(line) + _bare_doc_tokens(line):
                if not _resolves(doc, target):
                    broken.append(f"{rel}:{lineno}: broken link -> {target}")

    assert not broken, (
        "doc cross-references that no longer resolve (a rename rotted the "
        "link; repoint it to the file's current path):\n" + "\n".join(broken)
    )
