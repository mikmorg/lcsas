"""Contract tests: commands printed in the burned docs must match reality.

The recovery manuals (``recovery/docs/*.txt``, ``docs/workflows/*.md``)
are burned onto every meta-disc and followed literally by an heir on a
machine with nothing else installed.  These tests pin the doc-quoted
paths and commands to the actual tool interfaces so drift is caught at
commit time, not decades later (UX-04; contract-test home intended by
UX-02).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _recovery_docs() -> list[Path]:
    return sorted((REPO_ROOT / "recovery" / "docs").glob("*.txt"))


def _workflow_docs() -> list[Path]:
    return sorted((REPO_ROOT / "docs" / "workflows").glob("*.md"))


def _all_docs() -> list[Path]:
    """Every prose doc an heir or operator might follow."""
    return sorted((REPO_ROOT / "docs").rglob("*.md")) + _recovery_docs()


def _tier1_map() -> dict[str, tuple[str, str] | None]:
    """Extract the ``tier1_map`` literal from ``meta/builder.py``.

    The mapping is a function-local variable inside
    ``MetaVolumeBuilder._bundle_tier1_binaries`` so it cannot be
    imported; parse it out of the AST instead of hardcoding the
    triples here (the whole point is to follow the builder).
    """
    src = (REPO_ROOT / "src" / "lcsas" / "meta" / "builder.py").read_text()
    for node in ast.walk(ast.parse(src)):
        target: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == "tier1_map"
            and getattr(node, "value", None) is not None
        ):
            mapping = ast.literal_eval(node.value)  # type: ignore[attr-defined]
            assert isinstance(mapping, dict) and mapping
            return mapping
    raise AssertionError("tier1_map literal not found in src/lcsas/meta/builder.py")


def test_no_legacy_windows_triple_in_docs() -> None:
    """On-disc Windows paths must use the rust triple, never the
    legacy short-arch name.

    The backslash form (``...x86_64-windows\\...``) is by definition an
    on-disc Windows path; the meta-builder bundles binaries under the
    rust triple (``x86_64-pc-windows-gnu``), so any legacy backslash
    path in a burned manual sends the heir to a directory that does
    not exist.  Forward-slash references to the *source-tree* build
    dir (``recovery/bin/x86_64-windows/``) remain legitimate.
    """
    legacy = "x86_64-windows" + "\\"
    offenders = []
    for doc in _recovery_docs() + _workflow_docs():
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            if legacy in line:
                offenders.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "legacy on-disc windows path (x86_64-windows\\) in docs:\n"
        + "\n".join(offenders)
    )

    # Every on-disc windows bin dir the docs DO name must be a
    # rust-triple key of the meta-builder's tier1_map.  Design-plan
    # docs (*_PLAN.txt) may sketch dirs for targets that do not ship
    # yet (e.g. the Phase W5 msvcrt fallbacks), so only the
    # heir-facing manuals and workflow docs are held to the map.
    triples = set(_tier1_map())
    win_dir = re.compile(r"bin\\([A-Za-z0-9_.-]*windows[A-Za-z0-9_.-]*)\\")
    bad = []
    for doc in _recovery_docs() + _workflow_docs():
        if doc.name.endswith("_PLAN.txt"):
            continue
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            for match in win_dir.finditer(line):
                if match.group(1) not in triples:
                    bad.append(
                        f"{doc.relative_to(REPO_ROOT)}:{lineno}: bin\\{match.group(1)}\\"
                    )
    assert not bad, (
        "on-disc windows bin dir not a tier1_map rust triple:\n" + "\n".join(bad)
    )


_INVOKE = re.compile(r"\b(?:python3?|py)(?:\.exe)?\s+\S*standalone_restorer\.py")

# Flags of the generated CLI that consume a value (see
# src/lcsas/restore/standalone_builder.py _CLI_BLOCK).
_VALUE_FLAGS = {
    "--repo",
    "--password-file",
    "--target",
    "--snapshot",
    "--mount-point",
    "--interactive",
    "--catalog",
}
_BOOL_FLAGS = {"--list-snapshots", "--info", "--help"}


def _is_continued(line: str) -> bool:
    """True when ``line`` ends with a CMD ``^`` or sh ``\\`` line
    continuation (a *lone* trailing char — ``E:\\`` is a drive path,
    not a continuation)."""
    stripped = line.rstrip()
    if not stripped or stripped[-1] not in "^\\":
        return False
    return len(stripped) == 1 or stripped[-2] in " \t"


def _extract_invocations(text: str) -> list[tuple[int, str]]:
    """Return ``(lineno, full_command)`` for every doc-quoted
    standalone_restorer.py run, joining continuation lines."""
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        match = _INVOKE.search(lines[i])
        if match is None:
            i += 1
            continue
        start = i
        first = lines[i][match.start():]
        # Inline-code mentions end at the closing backtick; anything
        # after it is prose, not part of the command.
        first = first.split("`", 1)[0]
        parts = [first]
        cur = first
        while _is_continued(cur) and i + 1 < len(lines):
            i += 1
            cur = lines[i]
            parts.append(cur)
        joined = " ".join(
            part.rstrip().rstrip("^\\").strip() for part in parts
        )
        found.append((start + 1, joined))
        i += 1
    return found


def test_standalone_restorer_invocations_use_real_flags() -> None:
    """Every doc-quoted standalone_restorer.py command must use the
    script's real interface: ``--repo``, ``--password-file`` and
    ``--target`` are all ``required=True`` and there are NO positional
    arguments, so a positional invocation makes argparse error out.
    ``--help``-style mentions are exempt.
    """
    problems = []
    for doc in _all_docs():
        for lineno, cmd in _extract_invocations(doc.read_text()):
            where = f"{doc.relative_to(REPO_ROOT)}:{lineno}"
            tokens = [t.strip("`'\"") for t in cmd.split()]
            tokens = [t for t in tokens if t]
            script_idx = next(
                idx for idx, t in enumerate(tokens) if "standalone_restorer.py" in t
            )
            args = tokens[script_idx + 1:]
            # Bare references ("run `python3 standalone_restorer.py`,
            # see ...") and --help pointers are mentions, not commands
            # an heir would copy verbatim.
            if not args or "--help" in args:
                continue
            for flag in ("--repo", "--password-file", "--target"):
                if flag not in args:
                    problems.append(f"{where}: missing {flag} in: {cmd}")
            idx = 0
            while idx < len(args):
                tok = args[idx]
                if tok in _VALUE_FLAGS:
                    idx += 2
                elif tok.startswith("--"):
                    if tok not in _BOOL_FLAGS:
                        problems.append(f"{where}: unknown flag {tok} in: {cmd}")
                    idx += 1
                else:
                    problems.append(f"{where}: positional argument {tok!r} in: {cmd}")
                    idx += 1
    assert not problems, (
        "doc-quoted standalone_restorer.py commands diverge from the real CLI:\n"
        + "\n".join(problems)
    )


def test_no_stdin_password_claim_for_standalone() -> None:
    """The generated standalone restorer never prompts for the
    password (``--password-file`` is required); the docs must not
    claim otherwise near a standalone_restorer mention."""
    claim = re.compile(r"prompts?\s+for\s+the\s+password", re.IGNORECASE)
    offenders = []
    for doc in _all_docs():
        lines = doc.read_text().splitlines()
        for i, line in enumerate(lines):
            if "standalone_restorer" not in line:
                continue
            for j in range(i, min(i + 4, len(lines))):
                if claim.search(lines[j]):
                    offenders.append(
                        f"{doc.relative_to(REPO_ROOT)}:{j + 1}: {lines[j].strip()}"
                    )
    assert not offenders, (
        "stdin-password claim near standalone_restorer mention:\n"
        + "\n".join(offenders)
    )
