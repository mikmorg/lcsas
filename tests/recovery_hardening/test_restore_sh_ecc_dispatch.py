"""Hardening tests: restore.sh --check-disc ECC dispatch [FMT-01].

The heir-facing "my disc is scratched" path.  ``restore.sh --check-disc
IMAGE`` must run the bundled in-house ``lcsas-ecc`` tool to *verify* the
image and, on damage, offer/perform a *fix* -- using the per-target
binary resolved the same way every other recovery binary is, and with
NO externally installed dvdisaster/ddrescue.

These tests drive a stub ``lcsas-ecc`` whose verify/fix exit codes are
controlled by env vars, so the script's dispatch + exit-code mapping is
exercised without the real C decoder:

    LCSAS_TEST_ECC_VERIFY_RC   exit code the stub returns for `verify`
    LCSAS_TEST_ECC_FIX_RC      exit code the stub returns for `fix`

Exit-code contract pinned here (script-side, mirroring lcsas-ecc):
    0  clean / repaired      1  uncorrectable / declined
    2  no RS03 header        3  usage / I-O / missing binary
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"

# Default rust-triple the C build chooses on Linux x86_64.
HOST_TARGET = "x86_64-unknown-linux-musl"


def _install_ecc_stub(recovery: Path, target: str = HOST_TARGET) -> Path:
    """Install a stub ``lcsas-ecc`` at recovery/bin/<target>/lcsas-ecc.

    The stub echoes its subcommand to stdout (so the dispatch order is
    observable) and exits with an env-controlled code per subcommand.
    """
    bin_dir = recovery / "bin" / target
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "lcsas-ecc"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        sub="$1"
        printf 'ECC-CALL: %s\\n' "$sub"
        case "$sub" in
            verify) exit "${LCSAS_TEST_ECC_VERIFY_RC:-0}" ;;
            fix)    exit "${LCSAS_TEST_ECC_FIX_RC:-0}" ;;
            *)      exit 3 ;;
        esac
    """))
    stub.chmod(0o755)
    return stub


def _recovery_fixture(tmp_path: Path, *, with_ecc: bool = True) -> Path:
    recovery = tmp_path / "recovery"
    (recovery / "bin").mkdir(parents=True)
    if with_ecc:
        _install_ecc_stub(recovery)
    return recovery


def _image(tmp_path: Path) -> Path:
    img = tmp_path / "disc.iso"
    img.write_bytes(b"\x00" * 4096)
    return img


_BASE_ENV = {
    "LCSAS_NO_RELOCATE": "1",
    "LCSAS_MOUNT_DIRS": "",
    "LCSAS_TARGET": HOST_TARGET,
}

# Same as _BASE_ENV but WITHOUT LCSAS_TARGET, so the script exercises
# its own uname-based triple-detection branch.
_AUTODETECT_ENV = {
    "LCSAS_NO_RELOCATE": "1",
    "LCSAS_MOUNT_DIRS": "",
}


def _run(args: list[str], extra_env: dict[str, str] | None = None,
         stdin: str = "") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_BASE_ENV, **(extra_env or {})}
    return subprocess.run(
        ["sh", str(RESTORE_SH), *args],
        input=stdin, capture_output=True, text=True, env=env, timeout=20,
    )


# ── --check-disc is discoverable from --help ──────────────────────────


def test_check_disc_in_help() -> None:
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0
    assert "--check-disc" in res.stdout
    assert "lcsas-ecc" in res.stdout


def test_check_disc_in_unknown_flag_help() -> None:
    """An unknown flag lists --check-disc among the valid flags."""
    res = _run(["--bogus"])
    assert res.returncode == 2
    assert "--check-disc" in res.stderr


# ── clean disc: verify exit 0 → script exit 0, no fix called ───────────


def test_clean_disc_exits_zero_no_fix(tmp_path: Path) -> None:
    recovery = _recovery_fixture(tmp_path)
    img = _image(tmp_path)
    res = _run(["--check-disc", str(img), str(recovery)],
               {"LCSAS_TEST_ECC_VERIFY_RC": "0"})
    assert res.returncode == 0, res.stderr
    assert "ECC-CALL: verify" in res.stdout
    assert "ECC-CALL: fix" not in res.stdout
    assert "intact" in res.stderr


# ── no ECC header: verify exit 2 → script exit 2 ──────────────────────


def test_no_ecc_header_exits_two(tmp_path: Path) -> None:
    recovery = _recovery_fixture(tmp_path)
    img = _image(tmp_path)
    res = _run(["--check-disc", str(img), str(recovery)],
               {"LCSAS_TEST_ECC_VERIFY_RC": "2"})
    assert res.returncode == 2, res.stderr
    assert "ECC-CALL: fix" not in res.stdout
    assert "no RS03 ECC parity" in res.stderr


# ── damage + autofix success: verify 1, fix 0 → exit 0 ────────────────


def test_damage_autofix_success(tmp_path: Path) -> None:
    recovery = _recovery_fixture(tmp_path)
    img = _image(tmp_path)
    res = _run(
        ["--check-disc", str(img), str(recovery)],
        {
            "LCSAS_TEST_ECC_VERIFY_RC": "1",
            "LCSAS_TEST_ECC_FIX_RC": "0",
            "LCSAS_CHECK_DISC_AUTOFIX": "1",
        },
    )
    assert res.returncode == 0, res.stderr
    assert "ECC-CALL: verify" in res.stdout
    assert "ECC-CALL: fix" in res.stdout
    assert "repair succeeded" in res.stderr


# ── damage + autofix uncorrectable: verify 1, fix 1 → exit 1 ──────────


def test_damage_autofix_uncorrectable(tmp_path: Path) -> None:
    recovery = _recovery_fixture(tmp_path)
    img = _image(tmp_path)
    res = _run(
        ["--check-disc", str(img), str(recovery)],
        {
            "LCSAS_TEST_ECC_VERIFY_RC": "1",
            "LCSAS_TEST_ECC_FIX_RC": "1",
            "LCSAS_CHECK_DISC_AUTOFIX": "1",
        },
    )
    assert res.returncode == 1, res.stderr
    assert "ECC-CALL: fix" in res.stdout
    assert "uncorrectable" in res.stderr


# ── damage, interactive decline: verify 1, answer "n" → exit 1, no fix ─


def test_damage_interactive_decline(tmp_path: Path) -> None:
    recovery = _recovery_fixture(tmp_path)
    img = _image(tmp_path)
    # No autofix; stdin is a pipe (not a TTY) so the script takes the
    # "no terminal to confirm on" branch and refuses to write -> exit 1.
    res = _run(
        ["--check-disc", str(img), str(recovery)],
        {"LCSAS_TEST_ECC_VERIFY_RC": "1"},
    )
    assert res.returncode == 1, res.stderr
    assert "ECC-CALL: fix" not in res.stdout


# ── missing image → exit 3 ────────────────────────────────────────────


def test_missing_image_exits_three(tmp_path: Path) -> None:
    recovery = _recovery_fixture(tmp_path)
    res = _run(["--check-disc", str(tmp_path / "nope.iso"), str(recovery)])
    assert res.returncode == 3, res.stderr
    assert "not found" in res.stderr


# ── missing lcsas-ecc binary → exit 3 ─────────────────────────────────


def test_missing_ecc_binary_exits_three(tmp_path: Path) -> None:
    recovery = _recovery_fixture(tmp_path, with_ecc=False)
    img = _image(tmp_path)
    res = _run(["--check-disc", str(img), str(recovery)])
    assert res.returncode == 3, res.stderr
    assert "lcsas-ecc" in res.stderr


# ── verify I-O error: verify 3 → exit 3 ───────────────────────────────


def test_verify_io_error_exits_three(tmp_path: Path) -> None:
    recovery = _recovery_fixture(tmp_path)
    img = _image(tmp_path)
    res = _run(["--check-disc", str(img), str(recovery)],
               {"LCSAS_TEST_ECC_VERIFY_RC": "3"})
    assert res.returncode == 3, res.stderr
    assert "ECC-CALL: fix" not in res.stdout


# ── docs-vs-reality: the burned manuals name the bundled repair tool ──


_DOCS = REPO_ROOT / "recovery" / "docs"
_RECOVER = _DOCS / "RECOVER.txt"
_RECOVER_WIN = _DOCS / "RECOVER_WINDOWS.txt"
_TIERS = _DOCS / "TIERS.txt"


def test_restore_sh_contains_check_disc_dispatch() -> None:
    """The script really wires --check-disc → lcsas-ecc verify/fix."""
    src = RESTORE_SH.read_text(encoding="utf-8")
    assert "--check-disc" in src
    assert "lcsas-ecc" in src
    assert "verify" in src and "fix" in src


def test_recover_txt_names_bundled_repair_tool() -> None:
    text = _RECOVER.read_text(encoding="utf-8")
    assert "lcsas-ecc" in text, "RECOVER.txt must name the bundled repair tool"
    assert "--check-disc" in text, "RECOVER.txt must point at restore.sh --check-disc"


def test_recover_windows_names_bundled_repair_tool_no_fanmirror() -> None:
    text = _RECOVER_WIN.read_text(encoding="utf-8")
    assert "lcsas-ecc" in text
    assert "--check-disc" in text
    # The abandoned-upstream fan mirror must be gone everywhere.
    assert "jcea.es" not in text.lower()


def test_tiers_txt_names_lcsas_ecc() -> None:
    text = _TIERS.read_text(encoding="utf-8")
    assert "lcsas-ecc" in text


def test_no_jcea_mirror_anywhere_in_recovery_docs() -> None:
    for doc in _DOCS.glob("*.txt"):
        assert "jcea.es" not in doc.read_text(encoding="utf-8").lower(), (
            f"{doc.name} still references the abandoned jcea.es fan mirror"
        )


# ── triple auto-detection: no LCSAS_TARGET → uname-derived triple ─────


def test_check_disc_autodetects_target(tmp_path: Path) -> None:
    """Without LCSAS_TARGET the script derives the triple from uname and
    still resolves the per-target lcsas-ecc binary (host is Linux
    x86_64 in CI, so the stub at HOST_TARGET is found)."""
    recovery = _recovery_fixture(tmp_path)
    img = _image(tmp_path)
    # _run merges _BASE_ENV (which sets LCSAS_TARGET); build a from-scratch
    # env without it so the script's own detection branch runs.
    env = {k: v for k, v in os.environ.items()}
    env.update(_AUTODETECT_ENV)
    env["LCSAS_TEST_ECC_VERIFY_RC"] = "0"
    env.pop("LCSAS_TARGET", None)
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--check-disc", str(img), str(recovery)],
        input="", capture_output=True, text=True, env=env, timeout=20,
    )
    assert res.returncode == 0, res.stderr
    assert "ECC-CALL: verify" in res.stdout


# ── AUTO_RECOVERY: run from inside the recovery tree (no positional) ───


def test_check_disc_auto_recovery_root(tmp_path: Path) -> None:
    """When restore.sh is invoked from inside recovery/scripts/, the
    recovery root is auto-detected -- no RECOVERY_ROOT positional
    needed for --check-disc."""
    import shutil

    recovery = _recovery_fixture(tmp_path)
    scripts = recovery / "scripts"
    scripts.mkdir()
    shutil.copy2(RESTORE_SH, scripts / "restore.sh")
    img = _image(tmp_path)
    env = {k: v for k, v in os.environ.items()}
    env.update(_AUTODETECT_ENV)
    env["LCSAS_TEST_ECC_VERIFY_RC"] = "0"
    env.pop("LCSAS_TARGET", None)
    res = subprocess.run(
        ["sh", str(scripts / "restore.sh"), "--check-disc", str(img)],
        input="", capture_output=True, text=True, env=env, timeout=20,
    )
    assert res.returncode == 0, res.stderr
    assert "ECC-CALL: verify" in res.stdout


# ── no recovery tree at all → exit 3 ──────────────────────────────────


def test_check_disc_no_recovery_tree(tmp_path: Path) -> None:
    """--check-disc with no resolvable recovery root exits 3."""
    img = _image(tmp_path)
    env = {k: v for k, v in os.environ.items()}
    env.update(_AUTODETECT_ENV)
    env.pop("LCSAS_TARGET", None)
    # Invoke the canonical restore.sh but pass a non-recovery dir as the
    # positional so neither the positional nor AUTO_RECOVERY resolves to
    # a tree with bin/lcsas-ecc.  AUTO_RECOVERY from the canonical path
    # WOULD resolve, so force the no-tree branch by pointing at a copy in
    # a bare dir.
    import shutil

    bare = tmp_path / "bare"
    bare.mkdir()
    shutil.copy2(RESTORE_SH, bare / "restore.sh")
    res = subprocess.run(
        ["sh", str(bare / "restore.sh"), "--check-disc", str(img)],
        input="", capture_output=True, text=True, env=env, timeout=20,
    )
    # No bin/ tree anywhere -> either "cannot locate recovery tree" (3)
    # or "no lcsas-ecc binary" (3).  Either way exit 3.
    assert res.returncode == 3, (res.returncode, res.stderr)
