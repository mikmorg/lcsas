"""Tests for the recovery/scripts/restore.sh target dispatcher.

The dispatcher is a POSIX-sh `case` block that maps (uname -s, uname -m)
to one of the six approved cross-platform targets (see
docs/history/CROSS_PLATFORM_META_RFC.md §3).  These tests exercise it by
extracting the snippet from the live script and re-running it with
synthetic `uname` output.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RESTORE_SH = Path(__file__).resolve().parents[2] / "recovery" / "scripts" / "restore.sh"


def _host_triple_source() -> str:
    """The host_triple function, cut from the live restore.sh: the one
    table of targets, used by the main dispatch, --check-disc and the
    relocation (#808)."""
    lines = RESTORE_SH.read_text().splitlines()
    start = lines.index("host_triple() {")
    end = lines.index("}", start)
    return "\n".join(lines[start:end + 1])


def _run_dispatcher(
    uname_s: str, uname_m: str, *, target_override: str = ""
) -> tuple[int, str, str]:
    """Run the live host_triple, then the main dispatch's choice
    (``${LCSAS_PLATFORM:-$(host_triple ...)}``) and its messages for an
    unsupported computer, in a fresh /bin/sh.

    ``MACHINE`` and ``OS`` come in as environment variables rather than
    a stubbed ``uname``.  The messages are pinned against the live
    script by ``test_live_restore_sh_contains_full_target_matrix``.

    Returns (returncode, stdout, stderr).  On success, stdout contains
    just the resolved ``TARGET`` value followed by a newline.
    """
    script = _host_triple_source() + r"""
        set -eu
        TARGET="${LCSAS_PLATFORM:-$(host_triple "$OS" "$MACHINE")}"
        if [ -z "$TARGET" ]; then
            case "$OS" in
                Linux)  printf 'unsupported Linux machine: %s\n' "$MACHINE" >&2 ;;
                Darwin) printf 'unsupported macOS machine: %s\n' "$MACHINE" >&2 ;;
                MINGW*|MSYS*|CYGWIN*|Windows*)
                        printf 'unsupported Windows machine: %s\n' "$MACHINE" >&2 ;;
                *)      printf 'unsupported OS: %s\n' "$OS" >&2 ;;
            esac
            exit 1
        fi
        printf '%s\n' "$TARGET"
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "MACHINE": uname_m,
        "OS": uname_s,
        "LCSAS_PLATFORM": target_override,
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


# ── Override path: $LCSAS_PLATFORM wins over auto-detection ──


def test_dispatcher_honors_lcsas_target_override():
    """Setting $LCSAS_PLATFORM forces the dispatcher to skip auto-detection."""
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
    assert "LCSAS_PLATFORM" in content
    # The messages _run_dispatcher prints are the live script's.
    for msg in ("unsupported Linux machine: %s", "unsupported macOS machine: %s",
                "unsupported Windows machine: %s", "unsupported OS: %s"):
        assert msg in content, msg


def test_one_target_table() -> None:
    """#808: one mapping function, used everywhere a triple is chosen
    (the main dispatch, --check-disc, the relocation): no second table."""
    content = RESTORE_SH.read_text()
    assert content.count("host_triple() {") == 1
    assert content.count('host_triple "') == 3
    # No triple is assigned anywhere else (the old tables did).
    for target in ("x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl",
                   "armv7-unknown-linux-gnueabihf", "aarch64-apple-darwin",
                   "x86_64-apple-darwin", "x86_64-pc-windows-gnu"):
        assert content.count(f"echo {target}") == 1, target
        assert f'="{target}"' not in content, target
