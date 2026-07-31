"""Root conftest: prove the tests are exercising THIS checkout (#445).

There are editable installs of ``lcsas`` on developer machines that hard-code
an absolute path to one particular checkout, e.g.::

    $ cat /usr/local/lib/python3.12/dist-packages/__editable__.lcsas-0.1.0.pth
    /home/<user>/git/lcsas/src

A ``.pth`` like that is on ``sys.path`` for *every* interpreter on the box.
So a pytest run started from a **git worktree** — the isolation mechanism the
`/autopilot` and `/pm` workflows use for parallel agents — edits
``<worktree>/src/lcsas/...``, collects ``<worktree>/tests/...``, and then
imports ``lcsas`` from the *original* checkout.  The worktree's new tests run
against the main checkout's unmodified source.

Both failure modes are silent, and both point the wrong way:

* **False green** — the change under test is never executed; the suite passes
  because the *other* tree still holds the old, working code.
* **False red / cross-contamination** — the other tree has unrelated
  uncommitted edits, and those get exercised instead.

Neither is visible in pytest's output.  This guard makes it loud: if the
imported ``lcsas`` package does not live inside the directory holding this
file, the run stops immediately rather than reporting a result that describes
some other source tree.

Documenting the hazard in a prompt was tried first and was not enough — the
trap was walked into within an hour of being written down.  Hence a check the
machine performs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent


def _assert_lcsas_is_ours() -> None:
    try:
        import lcsas
    except ImportError:  # pragma: no cover - a broken env fails later, clearly
        return

    pkg_file = getattr(lcsas, "__file__", None)
    if not pkg_file:  # namespace package or other exotic layout
        return
    pkg_dir = Path(pkg_file).resolve().parent

    try:
        pkg_dir.relative_to(_REPO_ROOT)
    except ValueError:
        pytest.exit(
            "\n"
            "lcsas was imported from OUTSIDE this checkout — the tests would\n"
            "not be exercising the code you are editing (#445).\n"
            f"\n  tests here : {_REPO_ROOT}"
            f"\n  lcsas from : {pkg_dir}\n"
            "\nMost likely an editable install (a .pth file in site-packages\n"
            "or dist-packages) pins `lcsas` to a different checkout. If you\n"
            "are running from a git worktree, prepend this tree's src/ to\n"
            "PYTHONPATH:\n"
            f"\n  PYTHONPATH={_REPO_ROOT / 'src'} python3 -m pytest ...\n"
            "\nRefusing to run rather than report a result about some other\n"
            "source tree.",
            returncode=4,
        )


_assert_lcsas_is_ours()
