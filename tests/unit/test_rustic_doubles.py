"""#948: test doubles of the rustic runner stay honest.

``RusticRunner`` is a seam with one real adapter, ``SubprocessRusticRunner``,
and many doubles in the tests.  ``restore_dry_run`` lived on in every
double long after the real rustic stopped accepting the command it ran, so
the four commands that used it were broken while their tests passed.  A
double may only define what the adapter has.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from lcsas.rustic.wrapper import RusticRunner, SubprocessRusticRunner

_TESTS = Path(__file__).resolve().parents[1]

#: The runner's operations: a class defining two or more of these is
#: taken for a rustic double.
_RUNNER_OPS = {"init_repo", "backup", "snapshots", "restore"}


def _public_methods(cls: type) -> set[str]:
    return {
        name for name, _ in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def _doubles() -> list[tuple[str, str, set[str]]]:
    """(file, class, public methods) of every rustic double in tests/."""
    found = []
    for path in sorted(_TESTS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name.startswith("Test"):
                continue
            methods = {
                n.name for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not n.name.startswith("_")
            }
            if len(methods & _RUNNER_OPS) >= 2 or (
                "rustic" in node.name.lower() and methods
            ):
                found.append((str(path.relative_to(_TESTS)), node.name, methods))
    return found


def test_doubles_define_only_what_the_adapter_has() -> None:
    real = _public_methods(SubprocessRusticRunner)
    doubles = _doubles()
    assert doubles, "no rustic double found: the scan is broken"
    stale = [
        f"{where}::{name} defines {sorted(methods - real)}"
        for where, name, methods in doubles if methods - real
    ]
    assert not stale, (
        "test doubles define methods SubprocessRusticRunner does not "
        "have:\n" + "\n".join(stale)
    )


def test_protocol_matches_the_adapter() -> None:
    """The Protocol lists exactly the rustic operations the adapter
    defines (its base class adds helpers such as ``check_binary``)."""

    def own(cls: type) -> set[str]:
        return {
            name for name, value in vars(cls).items()
            if not name.startswith("_") and inspect.isfunction(value)
        }

    assert own(RusticRunner) == own(SubprocessRusticRunner)


def test_no_dry_run_the_real_rustic_rejects() -> None:
    """rustic 0.11.2 rejects ``restore --dry-run --json`` and ``prune
    --dry-run --json``; the runner offers neither."""
    for name in ("restore_dry_run", "prune_dry_run"):
        assert not hasattr(SubprocessRusticRunner, name), name
        assert name not in vars(RusticRunner), name
