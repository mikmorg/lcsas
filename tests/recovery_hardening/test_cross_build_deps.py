r"""Guard the recovery cross-build source dependencies.

The ``bin/<arch>/<tool>`` cross-build targets in ``recovery/Makefile`` are
*file targets*.  If they carry no prerequisites, Make treats an existing
committed binary as up-to-date and ``ecc-arches`` / ``keyshare-arches`` /
``all-arches`` become NO-OPs once the bins exist -- so a ``.c``/``.h`` edit
silently ships STALE binaries (this bit us 2026-06-15: an ``lcsas-ecc
augment`` change kept the pre-change bins until they were deleted by hand).

The fix declares each tool's sources as prerequisites of its cross bins, so
a source change forces a rebuild.  This gate pins that wiring in place: it
asserts every cross-built tool has a ``*_SRCS`` variable (a wildcard over
its source dir) AND a prerequisite line attaching those sources to the
per-arch ``bin/<arch>/<tool>`` targets.  A behavioural check (touch a
source, ``make -n``) would be stronger but mutates mtimes; this static
contract is enough to stop the prerequisite lines from being dropped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MAKEFILE = (
    Path(__file__).resolve().parents[2] / "recovery" / "Makefile"
)

# tool -> (SRCS var, a source-dir fragment that var's wildcard must mention)
TOOLS = {
    "lcsas-ecc": ("ECC_SRCS", "ECC_DIR"),
    "lcsas-keyshare": ("KEYSHARE_SRCS", "KEYSHARE_DIR"),
    # restore's per-arch recipe also emits iso9660 + init, so its source
    # var (ARCH_SRCS) covers SRCDIR (the restore tool dir).
    "lcsas-restore": ("ARCH_SRCS", "SRCDIR"),
}


@pytest.fixture(scope="module")
def makefile_text() -> str:
    assert MAKEFILE.is_file(), f"missing {MAKEFILE}"
    return MAKEFILE.read_text(encoding="utf-8")


@pytest.mark.parametrize("tool,srcs_var,dir_frag", [
    (t, v, d) for t, (v, d) in TOOLS.items()
])
def test_srcs_var_is_a_wildcard_over_the_source_dir(
    makefile_text: str, tool: str, srcs_var: str, dir_frag: str
) -> None:
    """The <TOOL>_SRCS variable must be a $(wildcard ...) over the tool's
    source dir -- so adding a new .c/.h is picked up automatically."""
    m = re.search(rf"(?m)^{re.escape(srcs_var)}\s*:?=\s*(.+(?:\\\n.*)*)",
                  makefile_text)
    assert m, f"{srcs_var} is not defined in recovery/Makefile"
    body = m.group(1)
    assert "$(wildcard" in body, (
        f"{srcs_var} must use $(wildcard ...) so new source files are "
        f"auto-included; got: {body!r}"
    )
    assert dir_frag in body, (
        f"{srcs_var} must wildcard over $({dir_frag}); got: {body!r}"
    )


@pytest.mark.parametrize("tool,srcs_var", [
    (t, v) for t, (v, _d) in TOOLS.items()
])
def test_cross_bins_depend_on_their_sources(
    makefile_text: str, tool: str, srcs_var: str
) -> None:
    """There must be a prerequisite line attaching $(<TOOL>_SRCS) to the
    per-arch bin/<arch>/<tool> targets, or the cross-build is a no-op once
    the committed binary exists."""
    # A line listing >=1 bin/<arch>/<tool>[.exe] target(s) and ending in
    # the source-var prerequisite.
    pat = re.compile(
        rf"(?m)^bin/[^:\n]*/{re.escape(tool)}(?:\.exe)?[^:\n]*:\s*"
        rf"\$\({re.escape(srcs_var)}\)\s*$"
    )
    assert pat.search(makefile_text), (
        f"no prerequisite line makes the bin/<arch>/{tool} cross targets "
        f"depend on $({srcs_var}); without it `{tool.split('-')[1]}-arches` "
        f"/ all-arches is a no-op once the committed bin exists and silently "
        f"ships stale binaries."
    )
