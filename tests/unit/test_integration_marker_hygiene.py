"""test_integration_marker_hygiene.py -- keep tests/integration/ inside the gate.

FAILURE MODE CAUGHT
-------------------
``make test-integration`` selects with ``-m integration``::

    test-integration:
        pytest tests/integration/ -v -m integration

so a test file under tests/integration/ that never sets the ``integration``
marker is DESELECTED.  And because ``make test-all`` is the four tier targets
and ``make gate`` is built on ``test-all``, such a file runs in **no gate
target at all** -- only ``make coverage`` (bare ``pytest tests/``) would
collect it, and that is not a gate.

The failure is silent in the worst way: every gate run prints "N deselected"
and exits 0, which reads as "covered".  Seven tests sat outside every gate
this way (issue #426), including the pair that proves the committed
``lcsas-keyshare`` binary accepts real SLIP-0039 recovery cards and that the C
and Python share-recovery paths agree -- tier-1 recovery behaviour.

WHAT THIS GATE ASSERTS
----------------------
Every module under tests/integration/ that defines at least one test must
declare the ``integration`` marker at module level, i.e.::

    pytestmark = pytest.mark.integration
    # or
    pytestmark = [pytest.mark.integration, requires_xorriso, ...]

Module level, not per-function, is the repo convention (test_concurrency.py,
test_disc_only_restore.py, test_ecc_repair.py, ...) and it is the form that
cannot be half-applied: a new test added to an already-marked module inherits
the marker automatically, whereas a per-function decorator has to be
remembered every single time.

WHY STATIC AST, NOT PYTEST INTROSPECTION
----------------------------------------
Importing the modules would drag in their external-tool probes, and shelling
out to ``pytest --collect-only`` from inside pytest is fragile.  Parsing the
source is exact for the property we care about (does the module SAY it is an
integration module) and needs no external tools -- so this gate lives in
tests/unit/ and runs in every gate, including the ones that skip when rustic
or xorriso are absent.  A gate that lived in tests/integration/ could itself
be deselected by the very bug it is meant to catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"

_MARKER = "integration"


def _marker_names(node: ast.expr) -> set[str]:
    """Marker names in a `pytestmark` value: `pytest.mark.X`, a list of them,
    or a name bound elsewhere in the module (e.g. `requires_xorriso`).

    Only `pytest.mark.<name>` attribute chains yield a name; bare identifiers
    are ignored, which is safe here because the assertion is about the
    presence of `integration`, never its absence in some alias.
    """
    if isinstance(node, ast.List | ast.Tuple):
        found: set[str] = set()
        for element in node.elts:
            found |= _marker_names(element)
        return found
    # Unwrap `pytest.mark.skipif(...)` -> `pytest.mark.skipif`
    if isinstance(node, ast.Call):
        return _marker_names(node.func)
    # `pytest.mark.integration`
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    ):
        return {node.attr}
    return set()


def _module_markers(tree: ast.Module) -> set[str]:
    """Every marker name declared by a module-level `pytestmark` assignment."""
    markers: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        is_pytestmark = any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets
        )
        if is_pytestmark and node.value is not None:
            markers |= _marker_names(node.value)
    return markers


def _defines_tests(tree: ast.Module) -> bool:
    """True when the module defines at least one test pytest would collect."""
    for node in ast.walk(tree):
        if isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef
        ) and node.name.startswith("test"):
            return True
    return False


def test_integration_dir_exists() -> None:
    """Guard the guard: if the directory moves, this gate must not silently
    pass over an empty file list."""
    assert INTEGRATION_DIR.is_dir(), (
        f"tests/integration/ not found at {INTEGRATION_DIR} -- this marker "
        f"hygiene gate would otherwise pass vacuously."
    )
    assert list(INTEGRATION_DIR.glob("test_*.py")), (
        "tests/integration/ contains no test_*.py files -- either the layout "
        "changed or this gate has become a no-op."
    )


def test_every_integration_module_declares_the_integration_marker() -> None:
    """Every test module under tests/integration/ must carry the marker, or
    `make test-integration` silently deselects it out of every gate."""
    offenders: list[str] = []

    for path in sorted(INTEGRATION_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _defines_tests(tree):
            continue  # helper module, nothing to deselect
        if _MARKER not in _module_markers(tree):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert not offenders, (
        f"{len(offenders)} module(s) under tests/integration/ do not declare "
        f"`pytestmark = pytest.mark.integration`, so `make test-integration` "
        f"(-m integration) deselects them and they run in NO gate target.\n\n"
        f"Add the marker at module level, keeping any existing skipif:\n"
        f"    pytestmark = [pytest.mark.integration, <existing marks...>]\n\n"
        f"Offending modules:\n  " + "\n  ".join(offenders)
    )
