"""test_standard_tools_runbook.py -- keep RESTORE_STANDARD_TOOLS.txt honest.

The "restore with standard tools" runbook is burned onto every disc and is the
heir's instructions for recovering with ONLY third-party audited tools and zero
LCSAS code.  If it drifts from reality -- wrong restic command, wrong on-disc
layout, a key-decode that no longer matches codec.py -- an heir following it
decades from now hits a dead end with no author to ask.  These are pure text
asserts tying the runbook to: the verified stock-restic compat gate, the real
on-disc layout, and the master-secret codec.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNBOOK = REPO_ROOT / "recovery" / "docs" / "RESTORE_STANDARD_TOOLS.txt"
_CODEC = REPO_ROOT / "src" / "lcsas" / "keyshare" / "codec.py"
_COMPAT_TEST = REPO_ROOT / "tests" / "recovery_hardening" / "test_stock_restic_compat.py"

_TEXT = _RUNBOOK.read_text(encoding="utf-8")


def test_runbook_exists_and_is_under_recovery_docs() -> None:
    """It must live under recovery/docs/ so the meta builder burns it onto
    every disc."""
    assert _RUNBOOK.is_file(), f"missing standard-tools runbook at {_RUNBOOK}"


def test_runbook_names_the_standard_tools() -> None:
    """The whole premise is third-party audited tools; name them."""
    for tool in ("restic", "shamir-mnemonic", "par2", "dvdisaster"):
        assert tool in _TEXT, f"runbook no longer mentions the standard tool {tool!r}"


def test_runbook_restic_restore_command_matches_verified_form() -> None:
    """The restore command must match what the compat gate proves works
    (restic -r <repo> restore latest --target ...)."""
    assert "restic -r" in _TEXT
    assert "restore latest" in _TEXT
    assert "--target" in _TEXT


def test_runbook_reassembly_matches_on_disc_layout() -> None:
    """Step 2 must reference the real on-disc layout: a data/ pack tree plus
    per-repo metadata/<id>/{index,snapshots,keys,config}."""
    assert "data/" in _TEXT
    assert "metadata/" in _TEXT
    for sub in ("index", "snapshots", "keys", "config"):
        assert sub in _TEXT, f"runbook reassembly omits the {sub!r} metadata dir"


def test_runbook_key_peel_matches_codec() -> None:
    """The split-key decode the runbook tells the heir to do must match the
    real master-secret encoding in codec.py (2-byte big-endian length prefix)."""
    codec = _CODEC.read_text(encoding="utf-8")
    # codec.py encodes: 2-byte big-endian length prefix + password + zero pad.
    assert '_LENGTH_PREFIX_BYTES' in codec and 'to_bytes(_LENGTH_PREFIX_BYTES, "big")' in codec, (
        "codec.py length-prefix encoding changed; the runbook's decode snippet "
        "must be updated to match."
    )
    # Runbook must instruct the matching decode (2-byte big-endian length).
    assert 'int.from_bytes(ms[:2], "big")' in _TEXT, (
        "runbook's SLIP-0039 password-peel no longer matches codec.py's "
        "2-byte big-endian length prefix."
    )


def test_runbook_does_not_depend_on_lcsas_code() -> None:
    """The tier's point is zero LCSAS code on the critical path: it must say so
    and must not require lcsas-restore / the Python fallback for the core flow."""
    assert "ZERO LCSAS code" in _TEXT or "zero LCSAS code" in _TEXT


def test_runbook_cross_references_the_cascade() -> None:
    """It is one tier among several; it must point the heir at the others."""
    assert "TIERS.txt" in _TEXT
    assert "RECOVER.txt" in _TEXT


def test_compat_gate_exists() -> None:
    """The runbook's promise is only credible because a gate proves it; that
    gate must exist."""
    assert _COMPAT_TEST.is_file(), (
        "the stock-restic compat gate is missing -- the runbook makes an "
        "unverified promise."
    )
