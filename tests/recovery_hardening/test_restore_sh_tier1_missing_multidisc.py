"""Hardening test (GATE-06): restore.sh drives the full tier1-missing
multi-disc fallback chain to a byte-identical restore.

The recovery cascade's whole reason for tiers 2 and 3 is the case where
tier 1 (the C ``lcsas-restore`` binary) is absent or won't run on the
heir's machine.  The live blind variant that exercises this --
``tier1-missing`` -- is local-only (sudo + cdemu), cost-gated, and
historically a permanent XFAIL (issue #227), so the most plausible
cascade failure has never been forced to pass.

This $0 deterministic test pins the chain restore.sh itself walks when
tier 1 is missing on a holographic multi-disc archive:

    tier 1 absent (no bin/<arch>/lcsas-restore)
        -> tier 2 present but SKIPPED (no $REPO/data/ -- rustic-static
           cannot drive a multi-disc archive; restore.sh:~1287)
        -> tier 3 (standalone_restorer.py + python3) completes
        -> restored tree is byte-identical to the source fixture.

It complements ``test_tier3_disc_swap.py`` (which pins the restorer's
own swap protocol) by driving ``restore.sh`` end-to-end -- the script's
dispatch logic, not the restorer in isolation.

Requires ``rustic`` (to build a real rustic-format repo) and the
``zstandard`` package (rustic v2 packs are zstd-compressed; the pure-
Python decoder is too slow / not exercised here).  Honest skip when
either is missing.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from lcsas.restore.standalone_builder import build_standalone

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"

pytestmark = [
    pytest.mark.skipif(
        shutil.which("rustic") is None,
        reason="rustic not on PATH; need a real rustic-format repo",
    ),
]


def _require_zstandard() -> None:
    pytest.importorskip(
        "zstandard",
        reason="zstandard not importable; tier-3 zstd restore would use the "
        "slow pure-Python decoder (not exercised here)",
    )


def _install_preflight_ok_noop(recovery: Path, name: str) -> None:
    """Install a binary that passes restore.sh's bin_preflight_ok
    (``--help`` exits 0, file is non-empty) but LOUDLY marks stdout if
    it is ever actually invoked for a restore -- so the multi-disc skip
    can be proven by the marker's ABSENCE."""
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        case "$1" in --help) exit 0 ;; esac
        echo "RAN_{name}_SHOULD_NOT_HAPPEN"
        exit 0
    """))
    stub.chmod(0o755)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _build_multidisc_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Build a holographic multi-disc fixture for restore.sh.

    Layout produced::

        recovery/
          metadata/alpha/{config,keys,index,snapshots}   (NO data/)
          bin/<HOST_TARGET>/rustic-static                 (preflight-OK noop)
        standalone_restorer.py                            (real tier-3)
        mounts/disc1/data/<XX>/<hex>                       (some packs)
        mounts/disc2/data/<XX>/<hex>                       (the rest)

    Returns ``(recovery, mounts, src, pwfile)``.
    """
    src = tmp_path / "src"
    src.mkdir()
    # Several distinct medium files -> rustic emits >=2 data packs under
    # >=2 hex prefixes, which we then split across two "discs".
    for i in range(4):
        (src / f"file{i}.bin").write_bytes(
            bytes((i * 37 + j * 13) % 256 for j in range(40000))
        )

    pwfile = tmp_path / "pw"
    pwfile.write_text("test-password\n")

    repo = tmp_path / "rustic_repo"
    subprocess.run(
        ["rustic", "-r", str(repo), "init", "--password-file", str(pwfile)],
        capture_output=True, check=True, timeout=60,
    )
    subprocess.run(
        ["rustic", "-r", str(repo), "backup", str(src),
         "--password-file", str(pwfile)],
        capture_output=True, check=True, timeout=120,
    )

    recovery = tmp_path / "recovery"
    meta_repo = recovery / "metadata" / "alpha"
    meta_repo.mkdir(parents=True)
    # Holographic repo metadata WITHOUT data/ (multi-disc layout).
    shutil.copy2(repo / "config", meta_repo / "config")
    for sub in ("keys", "index", "snapshots"):
        shutil.copytree(repo / sub, meta_repo / sub)

    # Split every pack across two disc mounts, preserving the
    # data/<XX>/<hex> layout the restorer probes.
    packs = sorted(p for p in (repo / "data").rglob("*") if p.is_file())
    assert len(packs) >= 2, (
        f"rustic produced {len(packs)} pack(s); need >=2 to span two discs"
    )
    mounts = tmp_path / "mounts"
    for idx, pack in enumerate(packs):
        disc = mounts / ("disc1" if idx % 2 == 0 else "disc2")
        dest = disc / "data" / pack.parent.name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pack, dest / pack.name)
    # Both discs must be non-empty for the split to be meaningful.
    assert any((mounts / "disc1").rglob("*")), "disc1 got no packs"
    assert any((mounts / "disc2").rglob("*")), "disc2 got no packs"

    # tier 2 present (so the SKIP branch, not the absent branch, fires)
    # but NO tier 1 binary for the host target.
    _install_preflight_ok_noop(recovery, "rustic-static")
    assert not (recovery / "bin" / HOST_TARGET / "lcsas-restore").exists()

    # Real tier-3 restorer next to recovery/.
    (recovery.parent / "standalone_restorer.py").write_text(build_standalone())

    return recovery, mounts, src, pwfile


def test_tier1_missing_multidisc_falls_to_tier3_byte_identical(
    tmp_path: Path,
) -> None:
    """No tier-1 binary + multi-disc archive: restore.sh must skip tier 2
    (rustic can't drive multi-disc) and complete via tier 3, restoring
    every file byte-identically to the source fixture."""
    _require_zstandard()
    recovery, mounts, src, pwfile = _build_multidisc_fixture(tmp_path)
    target = tmp_path / "restored"

    # Do NOT override HOME -- the host's user-site ``zstandard`` must
    # stay importable for tier 3.  Constrain mount discovery to our
    # fixture so the scan can't wander onto the real machine.
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": str(mounts),
        "LCSAS_REPO": "alpha",
        "LCSAS_TIER_FALLBACK": "1",
        "LCSAS_PASSWORD": "test-password",
        "LCSAS_PACK_CACHE_DIR": "",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert res.returncode == 0, (
        f"expected the tier1-missing chain to complete via tier 3; "
        f"rc={res.returncode}\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )

    # Tier 1 never ran (no binary present): its success banner must be
    # absent.  (restore.sh prints no banner at all for an absent tier-1
    # binary -- it silently skips both arms of the dispatch.)
    assert "[tier 1] using prebuilt lcsas-restore" not in res.stderr, (
        f"tier 1 unexpectedly ran; stderr:\n{res.stderr}"
    )
    # Tier 2 was SKIPPED for the multi-disc layout (issue #227 logic).
    assert "[tier 2] skipped" in res.stderr and "multi-disc" in res.stderr, (
        f"expected the tier-2 multi-disc skip diagnostic; stderr:\n{res.stderr}"
    )
    # The tier-2 noop stub must NOT have been invoked for a restore.
    assert "RAN_rustic-static_SHOULD_NOT_HAPPEN" not in res.stdout, (
        f"tier 2 ran on a multi-disc fixture; stdout:\n{res.stdout}"
    )
    # Tier 3 took over and completed.
    assert "[tier 3] falling back to Python" in res.stderr, (
        f"tier 3 did not dispatch; stderr:\n{res.stderr}"
    )
    assert "Restore complete." in res.stderr, (
        f"tier 3 did not report completion; stderr:\n{res.stderr}"
    )

    # Byte-identical: every source file must be present and hash-equal
    # somewhere under the restored tree (rustic restores under the
    # absolute source path inside target/).
    restored = {p.name: p for p in target.rglob("*") if p.is_file()}
    for original in sorted(src.iterdir()):
        assert original.name in restored, (
            f"{original.name} missing from restored tree: "
            f"{sorted(restored)}"
        )
        assert _sha256(original) == _sha256(restored[original.name]), (
            f"{original.name} restored bytes differ from source"
        )
