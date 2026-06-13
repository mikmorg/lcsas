"""GATE-03: tier-1 macOS (Mach-O) binary execution coverage.

The two cross-built macOS ``lcsas-restore`` / ``lcsas-keyshare`` binaries
(``recovery/bin/{aarch64-macos,x86_64-macos}/``) are committed and bundled
on every meta disc, but until now NOTHING ever executed them: every "macos"
reference in the test tree is a meta-bundling presence check or a dispatcher
string match.  They are built with ``zig cc -target <arch>-macos`` against
zig's bundled libSystem stubs — exactly the kind of build that can emit a
Mach-O the loader rejects, or link a symbol the stub permits but real
libSystem gates differently.  A zig/SDK regression would ship green through
every Linux/cdemu gate (including the blind restore) while leaving a Mac heir
— arguably the most likely consumer platform decades out — with an unproven
tier-1 artifact.

This module mirrors the qemu (``test_tier1_aarch64_qemu.py``) and wine
(``test_tier1_windows_wine.py``) harnesses: it runs ONLY where
``sys.platform == "darwin"`` (i.e. on the GitHub macOS runners wired up by
``.github/workflows/macos-tier1.yml``, or any future local Mac) and skips
honestly everywhere else, so ``make test-recovery-hardening`` on Linux is
unaffected.

Binary selection follows ``platform.machine()`` (``arm64`` ⇒
``aarch64-macos``, ``x86_64`` ⇒ ``x86_64-macos``), with an
``LCSAS_RESTORE_BIN`` override matching the other harnesses so a deliberately
mis-built artifact can be pointed at the gate during development (acceptance
criterion 3 — a truncated/foreign binary must turn the gate red).

Scope: this is binary-level *execution* proof — the Mach-O loads, parses
args, lists snapshots, restores the committed encrypted fixture
byte-identically, fails cleanly on a wrong password, and the Mach-O
``lcsas-keyshare`` combiner reconstructs a real LCSAS split byte-exact.  The
full macOS *journey* (restore.sh, hdiutil mounts) is UX-08 / INFRA-01.

Gatekeeper note (UX_CONCERNS ID 004 stays open): binaries from a git
checkout carry no quarantine xattr, so green here does NOT prove an heir's
Finder double-click works — only that the binary itself is sound.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _arch_dir() -> str:
    """Map the host CPU to the committed macOS bin directory."""
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "aarch64-macos"
    if machine in ("x86_64", "amd64"):
        return "x86_64-macos"
    # Unknown machine on darwin (shouldn't happen on a GitHub runner);
    # default to arm64 and let the skip/missing-binary logic handle it.
    return "aarch64-macos"


_MACOS_BIN_DIR = REPO_ROOT / "recovery" / "bin" / _arch_dir()

# ``LCSAS_RESTORE_BIN`` overrides the default so a deliberately mis-built
# artifact (e.g. a truncated binary, or a Linux ELF) can be pointed at this
# gate during development to prove it fails loudly — acceptance criterion 3.
RESTORE_BIN = (
    Path(os.environ["LCSAS_RESTORE_BIN"])
    if os.environ.get("LCSAS_RESTORE_BIN")
    else _MACOS_BIN_DIR / "lcsas-restore"
)
KEYSHARE_BIN = _MACOS_BIN_DIR / "lcsas-keyshare"

_BIN_OK = RESTORE_BIN.is_file() and os.access(RESTORE_BIN, os.X_OK)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not _BIN_OK,
    reason=(
        f"macOS tier-1 native coverage requires sys.platform == 'darwin' "
        f"(is {sys.platform!r}) and an executable "
        f"{RESTORE_BIN} (present={_BIN_OK}); runs on the GitHub macOS "
        "runners (.github/workflows/macos-tier1.yml) or a local Mac"
    ),
)

# Native execution; a list-snapshots or restore of the tiny fixture takes
# well under a second, but keep a generous ceiling for loaded runners.
TIMEOUT = 30


def _run(
    *args: str,
    bin_path: Path = RESTORE_BIN,
    env: dict[str, str] | None = None,
    stdin_data: str = "",
    timeout: int = TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [str(bin_path), *args],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=timeout,
    )


def _fixture_repo() -> Path | None:
    candidate = REPO_ROOT / "recovery" / "tests" / "fixtures" / "repo"
    if candidate.is_dir() and (candidate / "keys").is_dir():
        return candidate
    return None


def _make_pwfile(tmp_path: Path, password: str = "test") -> Path:
    pw = tmp_path / "pw"
    pw.write_text(password)
    return pw


def _manifest() -> dict[str, str]:
    repo = _fixture_repo()
    assert repo is not None
    return json.loads((repo / "manifest.json").read_text())


# ── List snapshots ───────────────────────────────────────────────


def test_list_snapshots(tmp_path: Path) -> None:
    """--list-snapshots against the committed fixture exits 0 and prints
    the snapshot path + date — first execution proof that the Mach-O
    loads and runs the read path."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    pwfile = _make_pwfile(tmp_path)
    res = _run(
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--list-snapshots",
        timeout=10,
    )
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "/test" in out, out
    assert "2026-05-21" in out, out


# ── Full restore, byte-identical ─────────────────────────────────


def test_full_restore_byte_identical(tmp_path: Path) -> None:
    """A full restore of the committed fixture must reproduce its files
    byte-for-byte (sha256 == the blob IDs recorded in manifest.json)."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    pwfile = _make_pwfile(tmp_path)
    target = tmp_path / "restored"
    res = _run(
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        timeout=10,
    )
    assert res.returncode == 0, res.stderr

    manifest = _manifest()
    # The restic content-blob IDs in the manifest ARE the sha256 of the
    # plaintext blob, so a byte-identical restore reproduces them exactly.
    expected = {
        "hello.txt": manifest["data_blob_id"],
        "trailing_zeros.bin": manifest["trailing_zeros_blob_id"],
    }
    for name, want_sha in expected.items():
        f = target / name
        assert f.is_file(), f"{name} not restored"
        got = hashlib.sha256(f.read_bytes()).hexdigest()
        assert got == want_sha, (
            f"{name} restored bytes mismatch: got {got}, want {want_sha}"
        )


# ── Wrong password fails cleanly ─────────────────────────────────


def test_wrong_password_fails_cleanly(tmp_path: Path) -> None:
    """An incorrect password must exit non-zero WITHOUT a crash signal
    (no SIGSEGV/SIGABRT) — proves the Mach-O error path is sound, not
    just the happy path."""
    repo = _fixture_repo()
    if repo is None:
        pytest.skip("fixture repo not generated; run gen_fixture.py")
    pwfile = _make_pwfile(tmp_path, password="definitely-not-the-password")
    target = tmp_path / "restored"
    res = _run(
        "--repo", str(repo),
        "--password-file", str(pwfile),
        "--target", str(target),
        timeout=10,
    )
    assert res.returncode != 0, "wrong password unexpectedly succeeded"
    # NOT SIGSEGV (-11/139) or SIGABRT (-6/134).
    assert res.returncode not in (-11, 139, -6, 134), (
        f"binary crashed on wrong password (rc={res.returncode}); "
        f"stderr:\n{res.stderr}"
    )


# ── Mach-O keyshare combiner ─────────────────────────────────────


def test_keyshare_combines_split_byte_exact(tmp_path: Path) -> None:
    """First execution coverage for the Mach-O ``lcsas-keyshare``.

    The committed CLI binary recovers an *LCSAS-framed* password (it
    chains SLIP-0039 recovery with the LCSAS length-prefixed codec — see
    slip39.h ``lcsas_keyshare_decode_master_secret``), so the raw
    SLIP-0039 master-secret vectors in ``tests/fixtures/keyshare/
    vectors.json`` cannot be fed to it directly.  We therefore drive it
    the way it is actually used in recovery: split a known password into
    a 2-of-5 set via ``lcsas key split`` and combine two cards back,
    asserting the byte-exact password.  This mirrors
    ``test_keyshare_binary_cards.py`` but on the Mach-O combiner — which
    no test executes anywhere today (KEY-06 owns its deeper gates).
    """
    if not KEYSHARE_BIN.is_file():
        pytest.skip(f"{KEYSHARE_BIN} not present")
    # Lazily import lcsas so Linux collection (skipped) never depends on
    # the package being installed for this darwin-only module.
    from lcsas.cli.main import main as cli_main

    password = b"correct horse battery staple\x01"
    pw_file = tmp_path / "pw"
    pw_file.write_bytes(password)
    out = tmp_path / "shares"
    assert cli_main([
        "key", "split", "--repo", "alpha",
        "--threshold", "2", "--shares", "5",
        "--password-file", str(pw_file), "--out", str(out),
    ]) == 0
    cards = sorted(out.glob("alpha-share-*-card.txt"))
    assert len(cards) == 5

    proc = subprocess.run(
        [str(KEYSHARE_BIN), str(cards[0]), str(cards[3])],
        capture_output=True,
        timeout=TIMEOUT,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout == password
