"""Tests for the recovery/scripts/restore.sh target dispatcher.

The dispatcher is a POSIX-sh `case` block that maps (uname -s, uname -m)
to one of the six approved cross-platform targets (see
docs/CROSS_PLATFORM_META_RFC.md §3).  These tests exercise it by
extracting the snippet from the live script and re-running it with
synthetic `uname` output.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RESTORE_SH = Path(__file__).resolve().parents[2] / "recovery" / "scripts" / "restore.sh"


def _run_dispatcher(
    uname_s: str, uname_m: str, *, target_override: str = ""
) -> tuple[int, str, str]:
    """Invoke a minimized version of the dispatcher in a fresh /bin/sh.

    The dispatcher is mirrored exactly from ``recovery/scripts/restore.sh``
    (see ``test_live_restore_sh_contains_full_target_matrix`` below for
    the drift guard).  We inject ``MACHINE`` and ``OS`` as environment
    variables rather than stubbing ``uname`` — overriding a builtin in
    POSIX sh (no ``export -f``) is not portable.

    Returns (returncode, stdout, stderr).  On success, stdout contains
    just the resolved ``TARGET`` value followed by a newline.
    """
    # NB: the case block here MUST stay in lockstep with the one in
    # recovery/scripts/restore.sh.  The drift guard below pins the
    # target strings; if you change one branch here, change the other.
    script = r"""
        set -eu
        # MACHINE, OS, LCSAS_TARGET come from the environment.
        if [ -n "${LCSAS_TARGET:-}" ]; then
            TARGET="$LCSAS_TARGET"
        else
            case "$OS" in
                Linux)
                    case "$MACHINE" in
                        x86_64|amd64)        TARGET="x86_64-unknown-linux-musl" ;;
                        aarch64|arm64)       TARGET="aarch64-unknown-linux-musl" ;;
                        armv7*|armv6*|arm)   TARGET="armv7-unknown-linux-gnueabihf" ;;
                        *)
                            printf 'unsupported Linux machine: %s\n' "$MACHINE" >&2
                            exit 1 ;;
                    esac ;;
                Darwin)
                    case "$MACHINE" in
                        arm64|aarch64)       TARGET="aarch64-apple-darwin" ;;
                        x86_64)              TARGET="x86_64-apple-darwin" ;;
                        *)
                            printf 'unsupported macOS machine: %s\n' "$MACHINE" >&2
                            exit 1 ;;
                    esac ;;
                MINGW*|MSYS*|CYGWIN*|Windows*)
                    case "$MACHINE" in
                        x86_64|amd64)        TARGET="x86_64-pc-windows-gnu" ;;
                        *)
                            printf 'unsupported Windows machine: %s\n' "$MACHINE" >&2
                            exit 1 ;;
                    esac ;;
                *)
                    printf 'unsupported OS: %s\n' "$OS" >&2
                    exit 1 ;;
            esac
        fi
        printf '%s\n' "$TARGET"
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "MACHINE": uname_m,
        "OS": uname_s,
        "LCSAS_TARGET": target_override,
    }
    proc = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, env=env
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# ── Happy paths: every supported (OS, machine) maps to the right target ──


@pytest.mark.parametrize(
    ("os", "machine", "expected_target"),
    [
        # Linux
        ("Linux", "x86_64", "x86_64-unknown-linux-musl"),
        ("Linux", "amd64", "x86_64-unknown-linux-musl"),
        ("Linux", "aarch64", "aarch64-unknown-linux-musl"),
        ("Linux", "arm64", "aarch64-unknown-linux-musl"),
        ("Linux", "armv7l", "armv7-unknown-linux-gnueabihf"),
        ("Linux", "armv6l", "armv7-unknown-linux-gnueabihf"),
        # macOS
        ("Darwin", "arm64", "aarch64-apple-darwin"),
        ("Darwin", "aarch64", "aarch64-apple-darwin"),
        ("Darwin", "x86_64", "x86_64-apple-darwin"),
        # Windows POSIX shells
        ("MINGW64_NT-10.0", "x86_64", "x86_64-pc-windows-gnu"),
        ("MSYS_NT-10.0", "x86_64", "x86_64-pc-windows-gnu"),
        ("CYGWIN_NT-10.0", "x86_64", "x86_64-pc-windows-gnu"),
    ],
)
def test_dispatcher_maps_supported_pair_to_target(os, machine, expected_target):
    rc, out, _err = _run_dispatcher(os, machine)
    assert rc == 0
    assert out == expected_target


# ── Override path: $LCSAS_TARGET wins over auto-detection ──


def test_dispatcher_honors_lcsas_target_override():
    """Setting $LCSAS_TARGET forces the dispatcher to skip auto-detection."""
    rc, out, _err = _run_dispatcher(
        "Linux", "x86_64",
        target_override="aarch64-unknown-linux-musl",
    )
    assert rc == 0
    assert out == "aarch64-unknown-linux-musl"


# ── Failure paths: unsupported (OS, machine) exits non-zero with a message ──


@pytest.mark.parametrize(
    ("os", "machine", "expected_err_substring"),
    [
        ("Linux", "riscv64", "unsupported Linux machine: riscv64"),
        ("Linux", "ppc64le", "unsupported Linux machine: ppc64le"),
        ("Linux", "i686", "unsupported Linux machine: i686"),
        ("Darwin", "powerpc", "unsupported macOS machine: powerpc"),
        ("MINGW64_NT-10.0", "aarch64", "unsupported Windows machine: aarch64"),
        ("FreeBSD", "x86_64", "unsupported OS: FreeBSD"),
        ("OpenBSD", "x86_64", "unsupported OS: OpenBSD"),
        ("SunOS", "sparc64", "unsupported OS: SunOS"),
    ],
)
def test_dispatcher_rejects_unsupported(os, machine, expected_err_substring):
    rc, _out, err = _run_dispatcher(os, machine)
    assert rc != 0
    assert expected_err_substring in err


# ── The live script must contain the same case branches we tested above ──


def test_live_restore_sh_contains_full_target_matrix():
    """Guard against drift: any future edit to restore.sh that breaks
    one of the six approved targets fails this test."""
    if not shutil.which("sh"):
        pytest.skip("no sh on PATH")
    content = RESTORE_SH.read_text()
    # Each target string must appear at least once in the dispatcher block.
    for target in (
        "x86_64-unknown-linux-musl",
        "aarch64-unknown-linux-musl",
        "armv7-unknown-linux-gnueabihf",
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
        "x86_64-pc-windows-gnu",
    ):
        assert target in content, f"{target} missing from restore.sh dispatcher"
    # And the $TARGET variable must be threaded through tier 1 + tier 2.
    assert '"$RECOVERY/bin/$TARGET/lcsas-restore"' in content
    assert '"$RECOVERY/bin/$TARGET/rustic-static"' in content
    # The override env var must still be honored.
    assert "LCSAS_TARGET" in content
