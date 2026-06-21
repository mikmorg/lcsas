"""test_stock_restic_compat.py -- the "standard tools" restore-tier guarantee.

WHY THIS GATE EXISTS
--------------------
LCSAS ships three restore tiers built from code we wrote or bundle (the C89
``lcsas-restore``, the pinned ``rustic-static``, the pure-Python
``standalone_restorer.py``).  A separate, deliberate hedge is that an LCSAS
archive must *also* be recoverable with **only widely-available, externally-
audited tools and zero LCSAS code** -- so a heir who distrusts (or cannot run)
anything we shipped can still get their data back:

  * the on-disc data is restic repository format, so **stock upstream restic**
    (the Go original, the implementation that got a real cryptographic audit)
    restores it -- byte for byte;
  * a Shamir-split repo password is plain SLIP-0039, so **Trezor's reference
    ``shamir-mnemonic``** reconstructs it, and a trivial, documented 2-byte
    length-prefix peel turns the recovered master secret back into the password.

This test pins both facts.  If a future rustic/restic version, a repo-format
change, or a key-codec change ever breaks stock-tool recovery, it fails LOUD --
before the promise in the heir runbook (the "restore with standard tools" tier)
becomes a lie an heir discovers decades from now with no author to ask.

The tools are opt-in (skipif absent) exactly like the wine/qemu cross-arch
gates: set ``LCSAS_RESTIC_BIN`` or put ``restic`` on PATH, and
``pip install shamir-mnemonic`` for the key half.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import diff_trees

pytestmark = pytest.mark.integration


def _stock_restic() -> str | None:
    """Path to a STOCK upstream restic binary (not rustic, not bundled)."""
    return os.environ.get("LCSAS_RESTIC_BIN") or shutil.which("restic")


requires_rustic = pytest.mark.skipif(
    shutil.which("rustic") is None, reason="rustic not installed"
)
requires_stock_restic = pytest.mark.skipif(
    _stock_restic() is None,
    reason="stock restic not installed (set LCSAS_RESTIC_BIN or put it on PATH)",
)


@requires_rustic
@requires_stock_restic
def test_stock_restic_restores_lcsas_repo_byte_identical(tmp_path: Path) -> None:
    """The data tier: a rustic-written LCSAS repo restores byte-identically
    under STOCK restic, with no LCSAS code in the loop."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("hello from lcsas\n")
    (src / "sub" / "b.bin").write_bytes(bytes(range(256)) * 64)  # 16 KiB binary
    (src / "sub" / "note.md").write_text("nested text\n")
    pwfile = tmp_path / "key.txt"
    pwfile.write_text("pw-correct-horse\n")
    repo = tmp_path / "repo"

    # Write the repo with rustic (the LCSAS writer), backing up a RELATIVE
    # path so the restore reproduces ``<target>/src`` deterministically.
    subprocess.run(
        ["rustic", "-r", str(repo), "--password-file", str(pwfile), "init"],
        capture_output=True, check=True, timeout=60,
    )
    subprocess.run(
        ["rustic", "-r", str(repo), "--password-file", str(pwfile),
         "backup", "src"],
        cwd=str(tmp_path), capture_output=True, check=True, timeout=120,
    )

    # Restore with STOCK restic -- not rustic, not lcsas-restore.
    out = tmp_path / "restored"
    env = {**os.environ, "RESTIC_PASSWORD_FILE": str(pwfile)}
    res = subprocess.run(
        [str(_stock_restic()), "-r", str(repo), "restore", "latest",
         "--target", str(out), "--no-lock"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert res.returncode == 0, (
        "stock restic could not restore the rustic-written LCSAS repo -- the "
        f"standard-tools data tier is broken.\nstderr:\n{res.stderr}"
    )
    restored_src = out / "src"
    assert restored_src.is_dir(), (
        f"stock restic restore produced no src/ tree under {out}; "
        f"got: {sorted(p.name for p in out.rglob('*'))[:20]}"
    )
    diffs = diff_trees(src, restored_src)
    assert not diffs, (
        "stock restic restore was NOT byte-identical to the source -- the "
        f"standard-tools data tier is unreliable: {diffs}"
    )


def test_reference_slip39_recovers_lcsas_split() -> None:
    """The key tier: Trezor's reference shamir-mnemonic reconstructs an LCSAS
    Shamir split, and the documented length-prefix peel yields the password --
    no LCSAS code on the heir's critical path for the split case."""
    shamir = pytest.importorskip(
        "shamir_mnemonic",
        reason="Trezor reference shamir-mnemonic not installed "
               "(pip install shamir-mnemonic)",
    )
    from lcsas.keyshare import encode_master_secret, split_secret

    pw = b"correct horse battery staple \xe2\x9c\x93 42"  # arbitrary incl. non-ASCII
    master_secret = encode_master_secret(pw)
    shares = split_secret(master_secret, 2, 3)  # 2-of-3

    # Reference tool recovers the master secret from ANY K=2 of the N=3 shares.
    recovered = shamir.combine_mnemonics([shares[0], shares[2]])
    assert recovered == master_secret, (
        "Trezor reference shamir-mnemonic did NOT recover the LCSAS master "
        "secret -- LCSAS shares are not standard-SLIP-0039-recoverable."
    )

    # Documented trivial decode (codec.py): 2-byte big-endian length prefix,
    # then that many password bytes.  A heir can do this with a 3-line snippet.
    n = int.from_bytes(recovered[:2], "big")
    assert recovered[2:2 + n] == pw, (
        "the length-prefix decode of the reference-recovered master secret did "
        "not reproduce the original password."
    )
