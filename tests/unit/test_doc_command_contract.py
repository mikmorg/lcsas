"""Contract tests: commands printed in the burned docs must match reality.

The recovery manuals (``recovery/docs/*.txt``, ``docs/workflows/*.md``)
are burned onto every meta-disc and followed literally by an heir on a
machine with nothing else installed.  These tests pin the doc-quoted
paths and commands to the actual tool interfaces so drift is caught at
commit time, not decades later (UX-04; contract-test home intended by
UX-02).
"""

from __future__ import annotations

import argparse
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


def _meta_build_parser_flags() -> set[str]:
    """Option strings the real ``lcsas meta build`` subparser accepts.

    Root-parser flags (``--config``, ``--db``, ...) are deliberately
    NOT included: argparse rejects them after the subcommand, so a doc
    quoting ``lcsas meta build --config ...`` sends the operator to an
    ``unrecognized arguments`` error.
    """
    from lcsas.cli.main import build_parser

    parser: argparse.ArgumentParser = build_parser()
    for path in ("meta", "build"):
        sub = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        parser = sub.choices[path]
    return {
        opt for action in parser._actions for opt in action.option_strings
    }


_META_BUILD = re.compile(r"\blcsas(?:\s+\S+)*?\s+meta\s+build\b")


def _extract_meta_build_commands(text: str) -> list[tuple[int, str]]:
    """Return ``(lineno, args_after_meta_build)`` for every doc-quoted
    ``lcsas ... meta build`` command, joining inline-code spans and
    ``\\``-continuations that run onto following lines."""
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        match = _META_BUILD.search(lines[i])
        if match is None:
            i += 1
            continue
        start = i
        in_code = "`" in lines[i][: match.start()]
        parts = [lines[i][match.end():]]
        # The command continues onto the next line while an opened
        # inline-code span has no closing backtick, or the line ends
        # with a shell continuation.
        while i + 1 < len(lines) and (
            (in_code and "`" not in parts[-1]) or _is_continued(parts[-1])
        ):
            i += 1
            parts.append(lines[i])
        if in_code:
            parts[-1] = parts[-1].split("`", 1)[0]
        joined = " ".join(
            part.rstrip().rstrip("^\\").strip() for part in parts
        )
        found.append((start + 1, joined))
        i += 1
    return found


def test_meta_build_flags_in_docs_exist() -> None:
    """Every ``lcsas meta build --<flag>`` mention in the burned docs
    must use a flag the real argparse subparser defines (UX-03).

    This is the gate that kills the ``--recovery-boot`` class of
    failure: BOOT.txt shipped that phantom flag on every meta disc for
    a long time, and nothing checked doc-quoted build commands against
    the CLI.
    """
    real = _meta_build_parser_flags()
    assert "--output" in real  # introspection self-check
    problems = []
    for doc in _all_docs():
        for lineno, cmd in _extract_meta_build_commands(doc.read_text()):
            for token in cmd.split():
                token = token.strip("`'\"[]")
                if not token.startswith("--"):
                    continue
                flag = token.split("=", 1)[0].rstrip(".,;:)")
                if flag not in real:
                    problems.append(
                        f"{doc.relative_to(REPO_ROOT)}:{lineno}: "
                        f"flag {flag} not accepted by `lcsas meta build`"
                    )
    assert not problems, (
        "doc-quoted `lcsas meta build` commands use flags the real CLI "
        "rejects:\n" + "\n".join(problems)
    )


def _restore_sh_help_text() -> str:
    """Extract the ``--help`` heredoc from ``recovery/scripts/restore.sh``."""
    src = (REPO_ROOT / "recovery" / "scripts" / "restore.sh").read_text()
    match = re.search(
        r"-h\|--help\)\s*\n\s*cat <<EOF\n(.*?)\nEOF\n", src, re.DOTALL
    )
    assert match is not None, "could not locate the --help heredoc in restore.sh"
    return match.group(1)


def test_restore_sh_help_starts_from_meta() -> None:
    """The ``--help`` QUICK START must route the operator to the META
    disc (UX-06).

    Data discs carry standalone_restorer.py, docs and packs — never
    restore.sh; only the LCSAS_META disc does (RECOVER.txt).  The old
    "Insert ANY data disc" step 1 walked a first-time heir straight
    into ``sh: can't open /mnt/restore.sh``.
    """
    help_text = _restore_sh_help_text()
    assert "QUICK START" in help_text
    assert "Insert the disc labelled LCSAS_META" in help_text, (
        "--help QUICK START step 1 must start from the LCSAS_META disc "
        "(the only disc that carries restore.sh)"
    )
    assert "ANY data disc" not in help_text, (
        "--help QUICK START must not tell the operator to insert a data "
        "disc: data discs do not carry restore.sh (recovery/docs/RECOVER.txt)"
    )


# ────────────────────────────────────────────────────────────────────
#  restore.sh flags + lcsas CLI flags: docs-vs-reality gates (UX-02)
# ────────────────────────────────────────────────────────────────────

# A doc-quoted restore.sh *invocation*: a token ending exactly in
# ``restore.sh``.  Prose mentions (``restore.sh,``) and file:line
# citations (``restore.sh:368``) fail the lookahead and are skipped.
_RESTORE_SH_INVOKE = re.compile(
    r"(?:^|[\s`('\"])((?:[A-Za-z0-9_.$~{}/<>-]*/)?restore\.sh)(?=$|[\s`)'\"])"
)

_FLAG_TOKEN = re.compile(r"--[a-z][a-z-]*")

# Intentional counter-examples the restore.sh flag gate must skip:
# (corpus-name suffix, command substring).  Use only for docs that
# deliberately show a WRONG command to warn against it.
_RESTORE_SH_ALLOWLIST: set[tuple[str, str]] = set()


def _accepted_restore_sh_flags() -> set[str]:
    """Long flags the real restore.sh flag loop accepts (parsed from
    the ``case "$1" in`` arms, so future flags are picked up
    automatically)."""
    src = (REPO_ROOT / "recovery" / "scripts" / "restore.sh").read_text()
    match = re.search(r"while \[ \$# -gt 0 \]; do\n(.*?)\ndone\n", src, re.DOTALL)
    assert match is not None, "flag-parsing while-loop not found in restore.sh"
    flags = set(re.findall(r"(--[a-z][a-z-]*)\)", match.group(1)))
    assert "--help" in flags, f"sanity: parsed flag set looks wrong: {flags}"
    return flags


def _extract_restore_sh_commands(text: str) -> list[tuple[int, str]]:
    """Return ``(lineno, args_after_script)`` for every doc-quoted
    restore.sh invocation, joining continuation lines and stopping at
    a closing inline-code backtick."""
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        match = _RESTORE_SH_INVOKE.search(lines[i])
        if match is None:
            i += 1
            continue
        start = i
        parts = [lines[i][match.end():].split("`", 1)[0]]
        while _is_continued(parts[-1]) and i + 1 < len(lines):
            i += 1
            parts.append(lines[i].split("`", 1)[0])
        joined = " ".join(part.rstrip().rstrip("^\\").strip() for part in parts)
        found.append((start + 1, joined))
        i += 1
    return found


def _rendered_heir_docs(tmp_path: Path) -> dict[str, str]:
    """Render every *generated* doc that carries restore commands.

    The artifact is the contract (UX-02): the gate scans the rendered
    text an heir actually reads off the disc, not the template source.
    Uses a split-key config so the share-card pre-step blocks render.
    """
    from lcsas.config.settings import LCSASConfig, RepositoryConfig
    from lcsas.meta.builder import MetaVolumeBuilder
    from lcsas.staging.metadata import HolographicInjector

    config = LCSASConfig(
        mirror_base_path=tmp_path / "mirror",
        staging_path=tmp_path / "staging",
        db_path=tmp_path / "db.db",
        key_split=True,
        key_threshold=2,
        key_shares=5,
        repositories={
            "family": RepositoryConfig(
                name="family",
                mirror_path=tmp_path / "mirror" / "family",
                password_file=Path("/keys/family.key"),
            ),
        },
    )
    root = tmp_path / "data-disc"
    root.mkdir()
    injector = HolographicInjector(root)
    injector.write_start_here(config)
    injector.write_key_info(config)
    injector.write_restore_instructions()
    docs = {
        f"<data-disc>/{name}": (root / name).read_text(encoding="utf-8")
        for name in (
            "START_HERE.txt",
            "KEY_INFO.txt",
            "RESTORE_INSTRUCTIONS.txt",
        )
    }
    builder = MetaVolumeBuilder(tmp_path / "meta", config=config)
    docs["<meta>/START_HERE.txt"] = builder._render_meta_start_here(config)
    docs["<meta-noconfig>/START_HERE.txt"] = builder._render_meta_start_here(None)

    # UX-09: the generated whole-archive Recovery Card carries per-OS
    # restore.sh commands — hold them to the same flag contract.
    from lcsas.cli.main import _estate_card_text

    docs["<estate-card>"] = _estate_card_text(
        owner="Jane Smith",
        description="Family photos 2000-2025",
        technical_contact="Bob (bob@example.org)",
        repositories=["family"],
        key_storage_hints="Safe deposit box #1234",
        key_split=True,
        key_threshold=2,
        key_shares=5,
        label_prefix="LCSAS",
        disc_count=8,
        card_date="2026-06-13",
    )
    return docs


def test_restore_sh_doc_commands_use_only_real_flags(tmp_path: Path) -> None:
    """Every ``--flag`` in a doc-quoted restore.sh command — static
    manuals AND generated heir docs — must be a flag restore.sh's
    parser accepts (UX-02).

    This is the gate that kills the phantom-flag class of failure:
    KEY_INFO.txt shipped ``./restore.sh --target ~/restored`` for a
    long time while restore.sh accepted no such flag, dead-ending the
    heir right after the hardest step (password reconstruction).
    """
    accepted = _accepted_restore_sh_flags()

    corpus: dict[str, str] = {
        str(doc.relative_to(REPO_ROOT)): doc.read_text()
        for doc in (
            *_all_docs(),
            REPO_ROOT / "src" / "lcsas" / "meta" / "keyshare_combine.py",
        )
    }
    corpus.update(_rendered_heir_docs(tmp_path))

    problems = []
    flagged_commands = 0
    for name, text in corpus.items():
        for lineno, cmd in _extract_restore_sh_commands(text):
            if any(
                name.endswith(suffix) and substr in cmd
                for suffix, substr in _RESTORE_SH_ALLOWLIST
            ):
                continue
            flags = _FLAG_TOKEN.findall(cmd)
            flagged_commands += bool(flags)
            for flag in flags:
                if flag not in accepted:
                    problems.append(
                        f"{name}:{lineno}: restore.sh does not accept "
                        f"{flag}: restore.sh {cmd}"
                    )
    # Extractor self-check: the corpus is known to contain flagged
    # restore.sh commands (e.g. KEY_INFO's --target STEP 2); a count
    # of zero means the scanner broke, not that the docs are clean.
    assert flagged_commands >= 3, (
        f"extractor self-check: only {flagged_commands} restore.sh "
        "commands with flags found across the corpus — scanner broken?"
    )
    assert not problems, (
        "doc-quoted restore.sh commands use flags the real script "
        "rejects:\n" + "\n".join(problems)
    )


_LCSAS_CMD = re.compile(r"(?:^|[\s`$('\"])lcsas\s+")


def _extract_lcsas_commands(text: str) -> list[tuple[int, str]]:
    """Return ``(lineno, args_after_lcsas)`` for every doc-quoted
    ``lcsas`` command (``lcsas-restore`` etc. do not match)."""
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        match = _LCSAS_CMD.search(lines[i])
        if match is None:
            i += 1
            continue
        start = i
        parts = [lines[i][match.end():].split("`", 1)[0]]
        while _is_continued(parts[-1]) and i + 1 < len(lines):
            i += 1
            parts.append(lines[i].split("`", 1)[0])
        joined = " ".join(part.rstrip().rstrip("^\\").strip() for part in parts)
        found.append((start + 1, joined))
        i += 1
    return found


def _lcsas_parser_walk(tokens: list[str]) -> list[str]:
    """Walk doc-quoted ``lcsas`` args through the real argparse tree;
    return a description of every flag the CLI would reject.

    Descends subparsers as subcommand tokens appear and validates each
    ``--flag`` against the parser *at that level* — argparse rejects a
    root flag placed after a subcommand, so position matters.  The
    walk stops at the first unrecognized positional (its arity is
    unknowable from prose).
    """
    from lcsas.cli.main import build_parser

    parser: argparse.ArgumentParser = build_parser()
    problems: list[str] = []
    path = ["lcsas"]
    i = 0
    while i < len(tokens):
        tok = tokens[i].strip("`'\",;:()[]")
        if not tok:
            i += 1
            continue
        if re.fullmatch(r"--[a-zA-Z][a-zA-Z0-9-]*(=.*)?", tok):
            flag = tok.split("=", 1)[0]
            opts = {
                opt: action
                for action in parser._actions
                for opt in action.option_strings
            }
            if flag not in opts:
                problems.append(f"`{' '.join(path)}` does not accept {flag}")
                i += 1
                continue
            consumes_value = "=" not in tok and opts[flag].nargs != 0
            i += 2 if consumes_value else 1
            continue
        sub = next(
            (
                action
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
            ),
            None,
        )
        if sub is not None and tok in sub.choices:
            parser = sub.choices[tok]
            path.append(tok)
            i += 1
            continue
        break
    return problems


def test_lcsas_cli_flags_in_recovery_docs_exist() -> None:
    """Every ``lcsas <sub> --flag`` mention in ``recovery/docs/`` must
    resolve against the real argparse tree (UX-02 Assertion 3 —
    generalizes the UX-03 ``meta build`` gate to every subcommand).

    Red-first catch: PHYSICAL_DISC_VALIDATION.txt quoted
    ``lcsas burn --media ... --db <catalog>``, but ``--db`` is a
    root-parser flag that argparse rejects after the subcommand.
    """
    # Walker self-checks against known shapes so a silent breakage of
    # the walker cannot turn the gate into a no-op.
    assert _lcsas_parser_walk(["burn", "--db", "x"]), (
        "walker self-check: --db after the burn subcommand must be "
        "rejected (root flags are invalid post-subcommand)"
    )
    assert not _lcsas_parser_walk(["--db", "x", "burn", "--media", "BD25"])

    problems = []
    for doc in _recovery_docs():
        for lineno, cmd in _extract_lcsas_commands(doc.read_text()):
            problems.extend(
                f"{doc.relative_to(REPO_ROOT)}:{lineno}: {prob} | lcsas {cmd}"
                for prob in _lcsas_parser_walk(cmd.split())
            )
    assert not problems, (
        "doc-quoted lcsas commands use flags the real CLI rejects:\n"
        + "\n".join(problems)
    )


def test_split_key_python_fallback_matches_bundle_layout(tmp_path: Path) -> None:
    """The split-key heir docs must offer the per-target CPython
    bundled on the META disc for the combiner pre-step, and the
    documented path must match the layout restore.sh itself probes
    and the meta-builder ships (UX-02).

    Fallback chain pinned: lcsas-keyshare (static, primary) → system
    python3 → bundled ``recovery/bin/<platform>/python/bin/python3``.
    """
    fallback = "recovery/bin/<platform>/python/bin/python3"
    docs = _rendered_heir_docs(tmp_path)
    for name in (
        "<data-disc>/START_HERE.txt",
        "<data-disc>/KEY_INFO.txt",
        "<meta>/START_HERE.txt",
    ):
        text = docs[name]
        idx = text.find(fallback)
        assert idx != -1, (
            f"{name}: no bundled-python fallback for the combiner "
            "pre-step — an heir on a host without python3 dead-ends"
        )
        assert "keyshare_combine.py" in text[idx : idx + 200], (
            f"{name}: the bundled-python fallback must run "
            "keyshare_combine.py"
        )
        # The example platform folder named in the doc must be a real
        # bundled target, or the heir hunts for a directory that does
        # not exist.
        assert "x86_64-unknown-linux-musl" in text, (
            f"{name}: the doc must name a real example platform folder"
        )
    assert "x86_64-unknown-linux-musl" in _tier1_map()

    # Layout agreement: restore.sh's tier-3 probe and the documented
    # combiner fallback must point into the same bundle tree
    # (recovery/bin/<target>/python/bin/python3 — see
    # MetaVolumeBuilder._bundle_upstream_binaries).
    restore_sh = (REPO_ROOT / "recovery" / "scripts" / "restore.sh").read_text()
    assert "/bin/$TARGET/python/bin/python3" in restore_sh, (
        "restore.sh no longer probes recovery/bin/<target>/python/bin/"
        "python3 — update the heir-doc fallback path to the new layout"
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


def test_architecture_md_no_phantom_disc_readback_claim() -> None:
    """BURN-02 doc gate: architecture.md must not claim a post-burn
    full-disc per-pack SHA-256 read-back while it is unimplemented.

    Until the BURN-04 device read-back path exists, the implemented
    verification chain is: staging-time content hash of every pack,
    post-burn readability check, and on-demand
    ``catalog validate --content`` for mounted discs.
    """
    text = (REPO_ROOT / "docs" / "architecture.md").read_text()
    assert (
        "read-back the entire disc and verify SHA-256 of every pack"
        not in text
    ), (
        "docs/architecture.md claims a post-burn per-pack disc read-back "
        "that is not implemented (BURN-02/BURN-04)"
    )
