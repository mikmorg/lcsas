"""Issue #886: the restore contract table, run on tier 1 and tier 3.

Each case in ``tests/contract/restore_contract.py`` is run once on
``lcsas-restore`` (tier 1) and once on the generated
``standalone_restorer.py`` (the Python reader, tier 3).  Two tests per
case:

- ``test_reader_meets_contract[<case>-<reader>]``: the reader's exit
  code, restored tree, RESTORE_FAILURES.txt rows and last lines are
  what the case expects.
- ``test_readers_agree[<case>]``: the two readers did the same thing
  (exit code, tree, failure rows).  A case with a known difference
  names it in ``differs``; its agreement test is then a strict xfail.

Runs by default; a case skips when rustic, the tier-1 binary, root (or
not root) or passwordless ``sudo mount`` is missing, as it says.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.contract.restore_contract import (
    CASES,
    READERS,
    Built,
    Case,
    Outcome,
    Skip,
    build,
    disagreements,
    mismatches,
    run_reader,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"),
                       reason="POSIX names, links and special files; "
                              "the committed tier-1 binary is Linux"),
]

_BUILT: dict[str, Built | Skip] = {}
_RAN: dict[tuple[str, str], Outcome | Skip] = {}


@pytest.fixture(scope="session")
def contract_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("contract")


def _repo_key(case: Case) -> str:
    """Cases that back up the same thing share one repository."""
    return repr((case.repo, sorted(case.tree.items()),
                 sorted(case.before.items()) if case.before else None,
                 case.pack_per_blob, case.faults.missing,
                 case.faults.truncated, case.faults.corrupted))


def _built(case: Case, base: Path) -> Built:
    key = _repo_key(case)
    if key not in _BUILT:
        try:
            _BUILT[key] = build(case, base / _slug(case.name))
        except Skip as exc:
            _BUILT[key] = exc
    got = _BUILT[key]
    if isinstance(got, Skip):
        pytest.skip(str(got))
    return got


def _outcome(case: Case, reader: str, base: Path) -> Outcome:
    key = (case.name, reader)
    if key not in _RAN:
        built = _built(case, base)
        try:
            _RAN[key] = run_reader(reader, case, built,
                                   base / _slug(case.name) / reader)
        except Skip as exc:
            _RAN[key] = exc
    got = _RAN[key]
    if isinstance(got, Skip):
        pytest.skip(f"{reader}: {got}")
    return got


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name)


_IDS = [_slug(c.name) for c in CASES]


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_reader_meets_contract(case: Case, reader: str,
                               contract_dir: Path) -> None:
    got = _outcome(case, reader, contract_dir)
    wrong = mismatches(case, reader, got, _built(case, contract_dir))
    assert not wrong, (
        f"case {case.name!r}, reader {reader}:\n  - " + "\n  - ".join(wrong)
        + f"\n--- {reader} output (last 40 lines) ---\n"
        + "\n".join(got.output.splitlines()[-40:])
    )


def _agree_params() -> list[object]:
    params: list[object] = []
    for case, slug in zip(CASES, _IDS, strict=True):
        marks = []
        if case.differs:
            marks.append(pytest.mark.xfail(
                strict=True, raises=AssertionError,
                reason=f"known difference: {case.why}"))
        params.append(pytest.param(case, id=slug, marks=marks))
    return params


@pytest.mark.parametrize("case", _agree_params())
def test_readers_agree(case: Case, contract_dir: Path) -> None:
    outcomes = {r: _outcome(case, r, contract_dir) for r in READERS}
    differ = disagreements(case, outcomes)
    assert not differ, (
        f"case {case.name!r}: tier 1 and tier 3 differ:\n  - "
        + "\n  - ".join(differ)
    )
