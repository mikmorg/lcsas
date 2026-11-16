"""The docs on the meta disc point only at what the disc carries.

FAILURE MODE CAUGHT
-------------------
The meta disc builder leaves the owner's developer records off the
disc (#724): ``DEV_ONLY_DOC_DIRS`` (``docs/development/recovery/``)
and ``DEV_ONLY_RECOVERY_FILES`` (the audit-gate files in
``recovery/docs/``).  Docs that ARE on the disc kept pointing at them
(``docs/ESTATE_PLANNING.md``, ``docs/workflows/*``), so an heir reading
the disc was sent to files that are not there.
``tests/unit/test_doc_link_integrity.py`` cannot see this: it resolves
links in the source repository, where those files exist.

WHAT THIS GATE ASSERTS
----------------------
The docs are bundled by the real builder (``_bundle_docs``,
``_bundle_recovery_toolchain_artifacts``, ``_bundle_source``) into a
temporary meta disc tree, and every Markdown and text doc in it is read:

* A Markdown link ``[text](target)`` must reach a file or folder on the
  disc.  One relocation is allowed: the builder puts the LCSAS source
  tree ``src/`` at ``lcsas/src/``, so a link into ``src/`` counts when
  the file is there.
* A path to a doc (``docs/...`` or ``recovery/docs/...`` ending in .md
  or .txt, with or without backticks) or to a folder the builder leaves
  off the disc may name a file the disc does not carry only when its
  paragraph says where it is: "in the source repository", "not on this
  disc", "stays off the disc" or "does not carry".
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from lcsas.meta.builder import (
    DEV_ONLY_DOC_DIRS,
    MetaVolumeBuilder,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Under recovery/: the C test fixtures, vendored sources, fuzz corpora and
# pinned specs are not docs anyone navigates.
_NOT_DOCS = {"specs", "vendored", "tests", "fuzz"}

# Docs left as they are, with the reason.  Keep this short.
_ALLOWED: dict[str, str] = {
    # Agent instructions for the recovery/ tree; CLAUDE.md files are the
    # owner's to edit.  It names recovery/docs/AUDIT.md, which stays in
    # the source repository.
    "recovery/CLAUDE.md": "agent instructions, edited by the owner",
}

# A Markdown link: the (target) half of [text](target).
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
# A doc path, with or without backticks: docs/... or recovery/docs/...,
# ending in .md or .txt, maybe behind ../ steps.
_DOC_PATH = re.compile(
    r"(?<![\w./-])((?:\.\./)*(?:recovery/)?docs/[\w./-]*\w\.(?:md|txt))"
)
# A folder the builder leaves off the disc (docs/development/recovery/).
_OFF_DISC_DIR = re.compile(
    r"(?<![\w.-])((?:\.\./)*(?:docs/)?(?:"
    + "|".join(re.escape(d) for d in sorted(DEV_ONLY_DOC_DIRS))
    + r")(?:/[\w.-]+)*/?)"
)
# What a paragraph says when it names a file that is not on the disc.
_SAYS_OFF_DISC = re.compile(
    r"source repositor|not on (?:this|the) (?:meta )?disc"
    r"|stays? off the disc|does not carry",
    re.IGNORECASE,
)
_EXTERNAL = ("http://", "https://", "mailto:", "ftp://")
# A line that starts its own block: a heading, a list item, a table row.
_BLOCK_START = re.compile(r"\s*(?:#|[-*+] |\d+\. |\|)")


@pytest.fixture(scope="module")
def disc(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The doc, recovery and source parts of a meta disc, as built."""
    tmp = tmp_path_factory.mktemp("meta-docs")
    out = tmp / "meta"
    out.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LCSAS_RECOVERY_CACHE", str(tmp / "empty-cache"))
        builder = MetaVolumeBuilder(
            out, project_root=REPO_ROOT, recovery_dir=REPO_ROOT / "recovery",
        )
        builder._bundle_docs()
        builder._bundle_recovery_toolchain_artifacts()
        builder._bundle_source()
    return out


def _docs(disc: Path) -> Iterator[Path]:
    for path in sorted(disc.rglob("*")):
        if path.suffix not in (".md", ".txt") or not path.is_file():
            continue
        parts = path.relative_to(disc).parts
        if parts[0] == "lcsas":
            continue  # the source tree: README files of packages, no docs
        if parts[0] == "recovery" and len(parts) > 2 and parts[1] in _NOT_DOCS:
            continue
        yield path


def _blocks(text: str) -> Iterator[list[tuple[int, str]]]:
    """Paragraphs, list items and table rows, with 1-based line numbers."""
    block: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or _BLOCK_START.match(line):
            if block:
                yield block
            block = [] if not line.strip() else [(lineno, line)]
            continue
        block.append((lineno, line))
    if block:
        yield block


def _on_disc(disc: Path, doc: Path, target: str) -> bool:
    """True when *target*, as written in *doc*, is a path on the disc."""
    target = target.split("#", 1)[0]
    if not target:
        return True
    bases = [doc.parent, disc]
    rel = doc.relative_to(disc)
    if rel.parts[0] == "recovery":
        bases.append(disc / "recovery")  # recovery docs say docs/X.txt
    for base in bases:
        found = os.path.normpath(base / target)
        inside = os.path.relpath(found, disc)
        if inside.startswith(".."):
            continue  # outside the disc
        if Path(found).exists():
            return True
        # The builder copies src/ to lcsas/src/ (_bundle_source).
        if inside.startswith("src/") and (disc / "lcsas" / inside).exists():
            return True
    return False


def test_the_tree_has_the_docs_under_test(disc: Path) -> None:
    names = {p.relative_to(disc).as_posix() for p in _docs(disc)}
    assert "docs/ESTATE_PLANNING.md" in names
    assert "docs/workflows/verify-and-audit.md" in names
    assert "recovery/docs/FORMAT.txt" in names
    assert not any(n.startswith("docs/development/recovery/") for n in names)


def test_markdown_links_reach_the_disc(disc: Path) -> None:
    broken: list[str] = []
    for doc in _docs(disc):
        if doc.suffix != ".md":
            continue
        rel = doc.relative_to(disc).as_posix()
        if rel in _ALLOWED:
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for target in _MD_LINK.findall(line):
                if target.startswith(_EXTERNAL) or "://" in target:
                    continue
                if not _on_disc(disc, doc, target):
                    broken.append(f"{rel}:{lineno}: {target}")
    assert not broken, (
        "links in docs on the meta disc that lead off the disc; drop the "
        "link and name the file as plain text with 'in the source "
        "repository (not on this disc)':\n" + "\n".join(broken)
    )


def test_off_disc_paths_say_so(disc: Path) -> None:
    unsaid: list[str] = []
    for doc in _docs(disc):
        rel = doc.relative_to(disc).as_posix()
        if rel in _ALLOWED:
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        for block in _blocks(text):
            joined = "\n".join(line for _, line in block)
            if _SAYS_OFF_DISC.search(joined):
                continue
            for lineno, line in block:
                paths = set(_DOC_PATH.findall(line) + _OFF_DISC_DIR.findall(line))
                for path in sorted(paths):
                    if not _on_disc(disc, doc, path):
                        unsaid.append(f"{rel}:{lineno}: {path}")
    assert not unsaid, (
        "docs on the meta disc name a file the disc does not carry; say "
        "'in the source repository (not on this disc)' in that paragraph, "
        "or drop the reference:\n" + "\n".join(unsaid)
    )


def test_allowed_docs_exist(disc: Path) -> None:
    for rel in _ALLOWED:
        assert (disc / rel).is_file(), f"{rel} is allowed but not on the disc"
