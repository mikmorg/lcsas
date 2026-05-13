"""Build orchestration for the C89 recovery toolchain.

Wraps ``recovery/Makefile`` so the Python orchestrator can:

* Build the host-arch lcsas-restore binary on demand.
* Cross-compile for additional architectures when toolchains exist.
* Verify reproducibility.
* Compute a MANIFEST.sha256 over the produced artifacts.

The recovery toolchain is otherwise self-contained: it has no Python
imports.  This module is the *only* bridge from the orchestrator into
the recovery tree.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecoveryArtifacts:
    """Result of a successful recovery toolchain build."""

    arch: str
    lcsas_restore: Path
    lcsas_iso9660: Path | None
    lcsas_init: Path | None

    def to_dict(self) -> dict[str, str]:
        d = {"arch": self.arch, "lcsas_restore": str(self.lcsas_restore)}
        if self.lcsas_iso9660 is not None:
            d["lcsas_iso9660"] = str(self.lcsas_iso9660)
        if self.lcsas_init is not None:
            d["lcsas_init"] = str(self.lcsas_init)
        return d


class RecoveryBuilder:
    """Drive ``recovery/Makefile`` from the Python orchestrator.

    Parameters
    ----------
    recovery_dir : Path
        Root of the standalone ``recovery/`` tree (containing the
        Makefile, src/, vendored/, etc.).
    """

    SUPPORTED_ARCHES = (
        "x86_64", "aarch64", "riscv64",
        "x86_64-windows", "aarch64-windows",
    )

    # Arches that target Windows -- binaries get .exe suffix, build via zig cc.
    _WINDOWS_ARCHES = ("x86_64-windows", "aarch64-windows")

    def __init__(self, recovery_dir: Path) -> None:
        self._dir = recovery_dir.resolve()
        if not (self._dir / "Makefile").is_file():
            raise FileNotFoundError(
                f"Not a recovery tree (missing Makefile): {self._dir}"
            )

    @property
    def root(self) -> Path:
        return self._dir

    def build_host(self, verbose: bool = False) -> RecoveryArtifacts:
        """Build lcsas-restore for the host architecture."""
        arch = self._host_arch()
        env = os.environ.copy()
        env.setdefault("SOURCE_DATE_EPOCH", "1735689600")
        cmd = ["make", "-C", str(self._dir), "all"]
        out = subprocess.run(
            cmd,
            env=env,
            capture_output=not verbose,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            raise RuntimeError(
                f"recovery make failed (rc={out.returncode}):\n"
                f"{out.stderr if not verbose else ''}"
            )

        build = self._dir / "build"
        return RecoveryArtifacts(
            arch=arch,
            lcsas_restore=build / "lcsas-restore",
            lcsas_iso9660=build / "lcsas-iso9660" if (build / "lcsas-iso9660").exists() else None,
            lcsas_init=build / "lcsas-init" if (build / "lcsas-init").exists() else None,
        )

    def cross_build(self, arch: str, cc: str | None = None,
                    verbose: bool = False) -> RecoveryArtifacts:
        """Cross-compile lcsas-restore for ``arch``.

        For Linux arches (x86_64, aarch64, riscv64), ``cc`` defaults to
        ``<arch>-linux-musl-gcc``; the binary is statically linked.

        For Windows arches (x86_64-windows, aarch64-windows), the
        Makefile's dedicated ``windows`` target is invoked (which
        shells out to ``zig cc -target <arch>-windows-gnu``); the
        binary has a ``.exe`` suffix and lcsas-init is not produced
        (it's a Linux-only PID 1).
        """
        if arch not in self.SUPPORTED_ARCHES:
            raise ValueError(f"unsupported arch: {arch}")

        is_windows = arch in self._WINDOWS_ARCHES

        env = os.environ.copy()
        env.setdefault("SOURCE_DATE_EPOCH", "1735689600")

        if is_windows:
            # The Makefile already encodes the zig cc invocation; we
            # just trigger the right target.
            target = f"bin/{arch}/lcsas-restore.exe"
            out = subprocess.run(
                ["make", "-C", str(self._dir), target],
                env=env, capture_output=not verbose, text=True, check=False,
            )
            if out.returncode != 0:
                raise RuntimeError(
                    f"cross-build {arch} failed (rc={out.returncode}):\n"
                    f"{out.stderr if not verbose else ''}"
                )
            bin_dir = self._dir / "bin" / arch
            exe = bin_dir / "lcsas-restore.exe"
            if not exe.is_file():
                raise RuntimeError(f"expected {exe} not produced")
            return RecoveryArtifacts(
                arch=arch,
                lcsas_restore=exe,
                # iso9660 / init not currently cross-built for Windows;
                # they're either unused on Windows (init) or
                # functionally redundant (iso9660 -- Windows can mount
                # ISOs natively).
                lcsas_iso9660=None,
                lcsas_init=None,
            )

        # ── Linux cross-compile path ─────────────────────────────────
        cc = cc or f"{arch}-linux-musl-gcc"
        if shutil.which(cc) is None:
            raise FileNotFoundError(f"cross compiler not on PATH: {cc}")

        out = subprocess.run(
            ["make", "-C", str(self._dir), "clean"],
            env=env, check=False, capture_output=True, text=True,
        )
        out = subprocess.run(
            [
                "make", "-C", str(self._dir), "all",
                f"CC={cc}",
                f"BUILD=build/{arch}",
                "LDFLAGS=-static",
            ],
            env=env, capture_output=not verbose, text=True, check=False,
        )
        if out.returncode != 0:
            raise RuntimeError(
                f"cross-build {arch} failed (rc={out.returncode}):\n"
                f"{out.stderr if not verbose else ''}"
            )

        # Copy into bin/<arch>/.
        bin_dir = self._dir / "bin" / arch
        bin_dir.mkdir(parents=True, exist_ok=True)
        build = self._dir / "build" / arch
        for name in ("lcsas-restore", "lcsas-iso9660", "lcsas-init"):
            src = build / name
            if src.exists():
                shutil.copy2(str(src), str(bin_dir / name))
                os.chmod(str(bin_dir / name), 0o755)

        iso9660_path = bin_dir / "lcsas-iso9660"
        init_path = bin_dir / "lcsas-init"
        return RecoveryArtifacts(
            arch=arch,
            lcsas_restore=bin_dir / "lcsas-restore",
            lcsas_iso9660=iso9660_path if iso9660_path.exists() else None,
            lcsas_init=init_path if init_path.exists() else None,
        )

    def run_tests(self, verbose: bool = False) -> bool:
        """Run the recovery toolchain's unit-test suite.

        Returns True on success.  Does not raise on test failure;
        callers should check the return value.
        """
        out = subprocess.run(
            ["make", "-C", str(self._dir), "test"],
            capture_output=not verbose, text=True, check=False,
        )
        return out.returncode == 0

    def write_manifest(self, manifest_path: Path | None = None) -> Path:
        """Compute SHA-256 of every tracked file under recovery/.

        Excludes the build/ tree (artifacts are deterministic but
        rebuildable; the manifest covers source).  Result is sorted by
        path for deterministic diffs.
        """
        if manifest_path is None:
            manifest_path = self._dir / "MANIFEST.sha256"

        lines: list[str] = []
        for root, dirs, files in os.walk(self._dir):
            dirs[:] = [d for d in dirs if d not in {"build", "__pycache__", ".git"}]
            for fname in files:
                if fname == "MANIFEST.sha256":
                    continue
                fpath = Path(root) / fname
                with fpath.open("rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                rel = fpath.relative_to(self._dir)
                lines.append(f"{digest}  {rel}")
        lines.sort(key=lambda s: s.split("  ", 1)[1])
        manifest_path.write_text("\n".join(lines) + "\n")
        return manifest_path

    def _host_arch(self) -> str:
        import platform
        m = platform.machine().lower()
        if m in ("amd64", "x86_64"):
            return "x86_64"
        if m in ("aarch64", "arm64"):
            return "aarch64"
        if m == "riscv64":
            return "riscv64"
        raise RuntimeError(f"unsupported host arch: {m}")
