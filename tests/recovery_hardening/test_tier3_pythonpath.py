"""Hardening test: restore.sh tier-3 PYTHONPATH wiring for bundled zstandard.

Issue #239 — the bundled CPython at $RECOVERY/bin/$TARGET/python/ does
NOT carry the `zstandard` Python package; that package is bundled
separately at <META>/tools/lib/python3.X/zstandard/ by
`MetaVolumeBuilder._bundle_tools`.  `restore.sh`'s tier-3 exec must
export PYTHONPATH so the bundled CPython can `import zstandard`.

Without this wiring, tier-3 starts (the `import zstandard` guard in
restic_fallback.py degrades to _HAS_ZSTD=False) but immediately fails
on the first zstd-compressed pack — which is every pack in a real
rustic v2 repository.  The blind-restore `tier1-tier2-missing` variant
(issue #236) was a 11/15-14/15 flake because of this.

What this test catches:
  - PYTHONPATH not being exported by restore.sh before tier-3 exec.
  - PYTHONPATH being exported but missing the bundled zstandard dir.
  - The bundled zstandard sibling tree at $META/tools/lib/python3.X/
    being relocated / renamed without the lookup being updated.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"
PY_MINOR = f"python{sys.version_info.major}.{sys.version_info.minor}"


def _install_python_stub(bin_dir: Path) -> tuple[Path, Path]:
    """A `python3` shim that captures argv + env to files and exits 0.

    Returns (argv_log, env_log).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    argv_log = bin_dir / "argv.log"
    env_log = bin_dir / "env.log"
    stub = bin_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        : > {argv_log}
        for a in "$@"; do
            printf '%s\\n' "$a" >> {argv_log}
        done
        env > {env_log}
        exit 0
    """))
    stub.chmod(0o755)
    return argv_log, env_log


def _stage_meta_layout(root: Path) -> Path:
    """Synthesise a fake meta-disc layout, returning the `recovery/` dir.

    Mimics the structure produced by `MetaVolumeBuilder`:

        <root>/
        ├── recovery/
        │   ├── bin/<TARGET>/                  (no tier-1, no tier-2; force tier-3)
        │   └── scripts/
        ├── standalone_restorer.py             (sibling of recovery/)
        └── tools/lib/<py3.X>/zstandard/__init__.py   (the bundle under test)
    """
    recovery = root / "recovery"
    (recovery / "bin" / HOST_TARGET).mkdir(parents=True)
    (recovery / "scripts").mkdir(parents=True)

    # The script lookup tries ../standalone_restorer.py first, so
    # placing the stub there matches the production lookup order.
    (root / "standalone_restorer.py").write_text("# placeholder for tier-3\n")

    # Bundled zstandard tree (matches ToolBundler.bundle_python_package).
    zstd_dir = root / "tools" / "lib" / PY_MINOR / "zstandard"
    zstd_dir.mkdir(parents=True)
    (zstd_dir / "__init__.py").write_text("__version__ = '0.0.0'\n")

    return recovery


def _make_minimal_repo(recovery: Path) -> Path:
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    return repo


def test_tier3_exports_pythonpath_to_bundled_zstandard(tmp_path: Path) -> None:
    """restore.sh must export PYTHONPATH pointing at $META/tools/lib/py3.X/
    so the bundled CPython's `import zstandard` resolves."""
    meta_root = tmp_path / "meta"
    meta_root.mkdir()
    recovery = _stage_meta_layout(meta_root)
    _make_minimal_repo(recovery)

    stub_dir = tmp_path / "stubbin"
    _argv_log, env_log = _install_python_stub(stub_dir)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:" + os.environ.get("PATH", ""),
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
    }
    # Wipe any inherited PYTHONPATH so we know the value comes from
    # restore.sh's tier-3 wiring, not from our test harness.
    env.pop("PYTHONPATH", None)

    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )
    assert res.returncode == 0, (
        f"restore.sh exited {res.returncode}; stderr:\n{res.stderr}"
    )
    assert env_log.is_file(), "tier 3 was not reached"

    captured_env = dict(
        line.split("=", 1) for line in env_log.read_text().splitlines()
        if "=" in line
    )
    pythonpath = captured_env.get("PYTHONPATH", "")
    expected_dir = meta_root / "tools" / "lib" / PY_MINOR
    assert pythonpath, (
        f"PYTHONPATH was not exported by restore.sh tier-3 exec; "
        f"captured env keys: {sorted(captured_env)[:20]}..."
    )
    assert str(expected_dir) in pythonpath.split(":"), (
        f"PYTHONPATH={pythonpath!r} does not contain the bundled "
        f"zstandard parent dir {expected_dir}"
    )

    # And the diagnostic must surface on stderr so the operator/agent
    # can see which path the script picked.
    assert "PYTHONPATH includes bundled zstandard" in res.stderr, (
        f"missing diagnostic line on stderr:\n{res.stderr}"
    )


def test_tier3_pythonpath_can_actually_import_zstandard(tmp_path: Path) -> None:
    """End-to-end: a real `python3` invoked through restore.sh tier-3
    can `import zstandard` from the PYTHONPATH that restore.sh exports."""
    meta_root = tmp_path / "meta"
    meta_root.mkdir()
    recovery = _stage_meta_layout(meta_root)
    _make_minimal_repo(recovery)

    # This time the python stub is a REAL python interpreter that
    # tries `import zstandard` and writes the version to a log.  We
    # don't need argparse handling -- the stub just exits.
    import_log = tmp_path / "import.log"
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    real_py = sys.executable
    stub = stub_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        # We get exec'd as `python3 standalone_restorer.py --repo ...`.
        # Ignore the script + args; just probe the PYTHONPATH-resolved
        # zstandard module.
        exec {real_py} -c '
        import sys, zstandard, pathlib
        pathlib.Path("{import_log}").write_text(zstandard.__version__)
        sys.exit(0)
        '
    """))
    stub.chmod(0o755)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:" + os.environ.get("PATH", ""),
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        # Force the real python to behave as a fresh-host interpreter
        # so the only place it can find zstandard is via the PYTHONPATH
        # restore.sh exports.
        "PYTHONNOUSERSITE": "1",
        "HOME": str(tmp_path),
    }
    env.pop("PYTHONPATH", None)

    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=20,
    )
    assert res.returncode == 0, (
        f"restore.sh exited {res.returncode}; stderr:\n{res.stderr}"
    )
    assert import_log.is_file(), (
        "real python did not run -- tier 3 was not reached or "
        f"PYTHONPATH wiring is broken.  stderr:\n{res.stderr}"
    )
    assert import_log.read_text().strip() == "0.0.0", (
        "The stub zstandard at the bundle dir was NOT the one imported; "
        "PYTHONPATH ordering or contents are wrong."
    )
