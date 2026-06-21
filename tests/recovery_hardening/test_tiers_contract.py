"""
test_tiers_contract.py -- docs-vs-reality contract gate for TIERS.txt.

FAILURE MODE CAUGHT
-------------------
recovery/docs/TIERS.txt is the operator-facing description of the recovery
cascade.  It is burned onto every meta disc and is the document an heir reads
to understand which tool restores their data.  Until this gate existed it was
UNGUARDED: nothing tied its tier names, ordering, binary paths, or the
disc-integrity layer back to recovery/scripts/restore.sh (the actual
dispatcher).  A rename in the script -- ``lcsas-restore`` -> something else,
``rustic-static`` -> something else, the tier order, the
``standalone_restorer.py`` last-resort tier, or the ``--check-disc`` /
``lcsas-ecc`` RS03 entry point -- would silently desync the doc from reality,
and the heir following the doc decades from now would be reading fiction.

WHAT THIS GATE ASSERTS
----------------------
  * TIERS.txt names tier 1 as the prebuilt C89 ``lcsas-restore`` and tier 2 as
    the vendored ``rustic-static`` -- and restore.sh actually dispatches to
    those binary names under ``bin/<arch>/`` (``$RECOVERY/bin/$TARGET/...``).
  * TIERS.txt names tier 3 as the pure-Python ``standalone_restorer.py`` last
    resort -- and restore.sh actually has a tier-3 Python dispatch to it.
  * The tier ORDER stated by TIERS.txt (1 -> 2 -> bare-path-boundary -> 3) is
    the order restore.sh dispatches in (tier 1 block precedes tier 2 block
    precedes tier 3 block).
  * The RS03 disc-integrity layer TIERS.txt documents (``lcsas-ecc`` via
    ``restore.sh --check-disc``) actually exists in restore.sh.
  * The bare-path verifier reference and the ``LCSAS_ALLOW_PYTHON_TIER=0``
    opt-out TIERS.txt cites are real (the path and the env var match the
    script).

Tests are static text extraction + string asserts -- no subprocess, no
optical hardware.  Modelled on test_boot_docs_reality.py and
test_disc_swap_docs.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
_TIERS_TXT = REPO_ROOT / "recovery" / "docs" / "TIERS.txt"
_RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"

# Read each file once at module level -- these are static string checks only.
_TIERS_TEXT = _TIERS_TXT.read_text(encoding="utf-8")
_RESTORE_TEXT = _RESTORE_SH.read_text(encoding="utf-8")


def test_tiers_and_restore_sh_both_present() -> None:
    """Both the doc and its source-of-truth script must exist on disc."""
    assert _TIERS_TXT.is_file(), f"missing TIERS.txt at {_TIERS_TXT}"
    assert _RESTORE_SH.is_file(), f"missing restore.sh at {_RESTORE_SH}"


def test_tier1_lcsas_restore_named_and_dispatched() -> None:
    """TIERS.txt must name tier-1 ``lcsas-restore`` (C89) and the script must
    actually dispatch to that binary under bin/<arch>/."""
    assert "lcsas-restore" in _TIERS_TEXT, (
        "TIERS.txt no longer names the tier-1 binary `lcsas-restore`."
    )
    assert "C89" in _TIERS_TEXT, (
        "TIERS.txt no longer states tier-1 lcsas-restore is C89 -- the "
        "ABI-stability claim that justifies it as the durable primary."
    )
    # The script dispatches tier 1 via $RECOVERY/bin/$TARGET/lcsas-restore.
    assert 'RESTORE_BIN="$RECOVERY/bin/$TARGET/lcsas-restore"' in _RESTORE_TEXT, (
        "restore.sh no longer dispatches tier 1 to "
        "$RECOVERY/bin/$TARGET/lcsas-restore -- TIERS.txt's tier-1 binary "
        "path is now fiction."
    )


def test_tier2_rustic_static_named_and_dispatched() -> None:
    """TIERS.txt must name tier-2 ``rustic-static`` and the script must
    actually dispatch to that binary."""
    assert "rustic-static" in _TIERS_TEXT, (
        "TIERS.txt no longer names the tier-2 binary `rustic-static`."
    )
    assert 'RUSTIC_BIN="$RECOVERY/bin/$TARGET/rustic-static"' in _RESTORE_TEXT, (
        "restore.sh no longer dispatches tier 2 to "
        "$RECOVERY/bin/$TARGET/rustic-static -- TIERS.txt's tier-2 binary "
        "path is now fiction."
    )


def test_tier3_standalone_restorer_named_and_dispatched() -> None:
    """TIERS.txt must name tier-3 ``standalone_restorer.py`` (Python last
    resort) and the script must actually have a tier-3 Python dispatch."""
    assert "standalone_restorer.py" in _TIERS_TEXT, (
        "TIERS.txt no longer names the tier-3 script `standalone_restorer.py`."
    )
    assert "standalone_restorer.py" in _RESTORE_TEXT, (
        "restore.sh no longer references standalone_restorer.py -- TIERS.txt's "
        "tier-3 last-resort is now fiction."
    )


def test_tier_order_matches_restore_sh_dispatch() -> None:
    """The 1 -> 2 -> 3 order TIERS.txt states must be the order restore.sh
    dispatches in (tier-1 block before tier-2 block before tier-3 block)."""
    i_t1 = _RESTORE_TEXT.find("# ── Tier 1:")
    i_t2 = _RESTORE_TEXT.find("# ── Tier 2:")
    i_t3 = _RESTORE_TEXT.find("# ── Tier 3:")
    assert i_t1 != -1 and i_t2 != -1 and i_t3 != -1, (
        "restore.sh no longer has the three '# ── Tier N:' dispatch headers; "
        "cannot confirm cascade order against TIERS.txt."
    )
    assert i_t1 < i_t2 < i_t3, (
        f"restore.sh tier dispatch order changed (tier1@{i_t1}, tier2@{i_t2}, "
        f"tier3@{i_t3}); TIERS.txt states the cascade runs 1 -> 2 -> 3."
    )
    # The doc's table must list the tiers in the same numeric order.
    d_t1 = _TIERS_TEXT.find("lcsas-restore")
    d_t2 = _TIERS_TEXT.find("rustic-static")
    d_t3 = _TIERS_TEXT.find("standalone_")
    assert d_t1 < d_t2 < d_t3, (
        "TIERS.txt introduces the tier binaries out of cascade order "
        "(expected lcsas-restore, then rustic-static, then standalone_*)."
    )


def test_bare_path_boundary_is_after_tier2_before_tier3() -> None:
    """TIERS.txt's 'BARE MINIMUM PATH ENDS HERE' boundary must sit between the
    tier-2 and tier-3 rows -- the invariant the bare-path test pins."""
    assert "BARE MINIMUM PATH ENDS HERE" in _TIERS_TEXT, (
        "TIERS.txt no longer marks where the bare-minimum (Python-free) path "
        "ends; the tiers 1-2 vs tier-3 distinction is the doc's core claim."
    )
    i_boundary = _TIERS_TEXT.find("BARE MINIMUM PATH ENDS HERE")
    i_t2 = _TIERS_TEXT.find("rustic-static")
    i_t3 = _TIERS_TEXT.find("standalone_")
    assert i_t2 < i_boundary < i_t3, (
        "TIERS.txt's 'BARE MINIMUM PATH ENDS HERE' boundary is no longer "
        "between the tier-2 (rustic-static) and tier-3 (standalone_*) rows."
    )


def test_python_tier_opt_out_env_var_matches_script() -> None:
    """The opt-out env var TIERS.txt documents must be the one restore.sh
    honours."""
    assert "LCSAS_ALLOW_PYTHON_TIER=0" in _TIERS_TEXT, (
        "TIERS.txt no longer documents LCSAS_ALLOW_PYTHON_TIER=0 to disable "
        "the Python tier."
    )
    assert "LCSAS_ALLOW_PYTHON_TIER" in _RESTORE_TEXT, (
        "restore.sh no longer honours LCSAS_ALLOW_PYTHON_TIER -- TIERS.txt's "
        "opt-out instructions would silently do nothing."
    )


def test_disc_integrity_layer_lcsas_ecc_matches_script() -> None:
    """TIERS.txt's RS03 disc-integrity layer (lcsas-ecc via --check-disc) must
    correspond to a real path in restore.sh."""
    # Doc side: names the in-house RS03 tool and the heir entry point.
    assert "lcsas-ecc" in _TIERS_TEXT, (
        "TIERS.txt no longer names the in-house RS03 repair tool `lcsas-ecc`."
    )
    assert "RS03" in _TIERS_TEXT, (
        "TIERS.txt no longer describes the RS03 ECC disc-integrity layer."
    )
    assert "--check-disc" in _TIERS_TEXT, (
        "TIERS.txt no longer names the `restore.sh --check-disc` heir entry "
        "point for the scratched-disc repair path."
    )
    # Script side: --check-disc dispatches lcsas-ecc verify/fix.
    assert "--check-disc" in _RESTORE_TEXT, (
        "restore.sh no longer implements --check-disc; TIERS.txt's "
        "disc-integrity entry point is fiction."
    )
    assert "lcsas-ecc" in _RESTORE_TEXT, (
        "restore.sh no longer invokes lcsas-ecc; TIERS.txt's RS03 repair "
        "claim is fiction."
    )


def test_bare_path_verifier_reference_is_real() -> None:
    """The bare-path verifier TIERS.txt cites must exist at the path it gives
    and the run command must target the recovery/ sub-make."""
    assert "recovery/tests/test_bare_path.sh" in _TIERS_TEXT, (
        "TIERS.txt no longer points at recovery/tests/test_bare_path.sh as "
        "the bare-minimum (Python-free) verifier."
    )
    bare = REPO_ROOT / "recovery" / "tests" / "test_bare_path.sh"
    assert bare.is_file(), (
        f"TIERS.txt references {bare} but it does not exist on disc."
    )
    assert "make -C recovery test-bare-path" in _TIERS_TEXT, (
        "TIERS.txt's run command for the bare-path verifier is wrong: the "
        "target lives in recovery/Makefile, so it is "
        "`make -C recovery test-bare-path`, not a root `make test-bare-path`."
    )


def test_tier_preflight_helper_named_matches_script() -> None:
    """TIERS.txt's pre-flight note must name the real helper in restore.sh."""
    assert "bin_preflight_ok" in _TIERS_TEXT, (
        "TIERS.txt no longer names the bin_preflight_ok helper that guards "
        "against bit-rotted tier binaries being exec'd as empty no-ops."
    )
    assert "bin_preflight_ok" in _RESTORE_TEXT, (
        "restore.sh no longer defines bin_preflight_ok -- TIERS.txt's "
        "pre-flight section describes a function that no longer exists."
    )


def test_stock_restic_tier_present_in_script_and_doc() -> None:
    """The stock-restic auto-fallback (tier 2b) must exist in restore.sh AND
    be described in TIERS.txt -- they must not drift apart."""
    # restore.sh dispatches a stock restic/rustic step using restic's flag
    # form (RESTIC_PASSWORD_FILE + -r REPO restore SNAP --target DIR).
    assert "tier 2b" in _RESTORE_TEXT, (
        "restore.sh no longer has the tier-2b stock-restic auto-fallback."
    )
    assert "RESTIC_PASSWORD_FILE" in _RESTORE_TEXT and "--no-lock" in _RESTORE_TEXT, (
        "restore.sh's stock-restic invocation lost its restic-form flags "
        "(RESTIC_PASSWORD_FILE / --no-lock)."
    )
    # It prefers the per-target bundled restic, then PATH.
    assert 'bin/$TARGET/restic' in _RESTORE_TEXT, (
        "restore.sh no longer prefers the bundled per-target restic."
    )
    # TIERS.txt must document the tier so the burned-to-disc cascade doc is
    # honest, and must point at the manual standard-tools runbook.
    assert "TIER 2b" in _TIERS_TEXT or "tier 2b" in _TIERS_TEXT, (
        "TIERS.txt no longer documents the stock-restic tier 2b."
    )
    assert "RESTORE_STANDARD_TOOLS.txt" in _TIERS_TEXT, (
        "TIERS.txt no longer points at the manual standard-tools runbook."
    )
