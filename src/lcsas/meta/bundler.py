"""Binary bundler — captures executables and shared-library dependencies.

Used by MetaVolumeBuilder to create self-contained tool bundles that
run without system-installed dependencies.  Shared libraries are
discovered via ``ldd`` and copied alongside the binaries;  wrapper
scripts set ``LD_LIBRARY_PATH`` so the dynamic linker finds them.

Only Linux x86_64 is supported (same architecture as the build host).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

_logger = logging.getLogger(__name__)

# ── glibc-family libraries that must NOT be bundled ──────────────────
# Mixing bundled glibc with the host's dynamic linker causes crashes.
# These are universal on any Linux system and safe to omit.
_SYSTEM_LIB_PREFIXES = (
    "linux-vdso",
    "ld-linux",
    "libc.so",
    "libpthread.so",
    "libdl.so",
    "libm.so",
    "librt.so",
    "libutil.so",
    "libnsl.so",
    "libresolv.so",
    "libcrypt.so",
    "libnss_",
    "libmvec.so",
)


def _should_bundle_lib(name: str) -> bool:
    """Return True if *name* is NOT a glibc-family library."""
    return not any(name.startswith(prefix) for prefix in _SYSTEM_LIB_PREFIXES)


# ── Public helpers ───────────────────────────────────────────────────


def resolve_binary(name: str) -> Path | None:
    """Find *name* on ``$PATH`` and resolve symlinks."""
    found = shutil.which(name)
    if found is None:
        return None
    return Path(found).resolve()


def get_shared_libs(binary: Path) -> list[Path]:
    """Parse ``ldd`` output for *binary* and return real library paths.

    Only non-glibc libraries are returned (see ``_should_bundle_lib``).
    """
    try:
        result = subprocess.run(
            ["ldd", str(binary)],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        _logger.warning(
            "ldd failed for %s — shared libraries will not be bundled",
            binary,
        )
        return []

    libs: list[Path] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if "=>" in parts:
            idx = parts.index("=>")
            if idx + 1 < len(parts) and parts[idx + 1] not in ("not", "("):
                lib_path = Path(parts[idx + 1])
                if lib_path.is_file() and _should_bundle_lib(lib_path.name):
                    libs.append(lib_path.resolve())
        elif len(parts) >= 1 and parts[0].startswith("/"):
            lib_path = Path(parts[0])
            if lib_path.is_file() and _should_bundle_lib(lib_path.name):
                libs.append(lib_path.resolve())

    return libs


def get_python_paths() -> tuple[Path, Path]:
    """Return ``(real_executable, stdlib_dir)`` for the running Python.

    Handles virtualenvs by using ``sys.base_prefix`` to find the real
    interpreter and standard library.

    Raises:
        FileNotFoundError: If the stdlib directory cannot be located.
    """
    real_exe = Path(sys.executable).resolve()
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    base = Path(sys.base_prefix)

    for lib_dir in ("lib", "lib64"):
        candidate = base / lib_dir / version
        if (candidate / "os.py").is_file():
            return real_exe, candidate

    # Fallback: sysconfig
    sc_path = sysconfig.get_path("stdlib")
    if sc_path:
        p = Path(sc_path)
        if (p / "os.py").is_file():
            return real_exe, p

    raise FileNotFoundError(
        f"Cannot locate Python stdlib at {base}/lib/{version}/ "
        f"or {base}/lib64/{version}/"
    )


# ── ToolBundler ──────────────────────────────────────────────────────


class ToolBundler:
    """Bundles binaries and their shared-library deps into a directory.

    Directory structure::

        output_dir/
        ├── bin/
        │   ├── rustic
        │   ├── xorriso
        │   └── python3
        └── lib/
            ├── *.so*              (shared libraries)
            └── python3.X/
                ├── *.py           (stdlib)
                └── lib-dynload/   (C extension modules)
    """

    def __init__(self, output_dir: Path) -> None:
        self._root = output_dir
        self._bin_dir = output_dir / "bin"
        self._lib_dir = output_dir / "lib"
        self._bundled: dict[str, Path] = {}

    @property
    def root(self) -> Path:
        return self._root

    @property
    def bin_dir(self) -> Path:
        return self._bin_dir

    @property
    def lib_dir(self) -> Path:
        return self._lib_dir

    @property
    def bundled(self) -> dict[str, Path]:
        """Map of tool name → path of bundled binary."""
        return dict(self._bundled)

    # ── Binary bundling ──────────────────────────────────────────

    def bundle_binary(
        self,
        name: str,
        binary_path: Path | None = None,
    ) -> Path:
        """Copy a binary and its shared libraries into the bundle.

        Args:
            name: Tool name (used as the filename inside ``bin/``).
            binary_path: Explicit path.  If *None*, searches ``$PATH``.

        Returns:
            Path to the bundled binary.

        Raises:
            FileNotFoundError: If the binary cannot be found.
        """
        binary_path = (
            resolve_binary(name) if binary_path is None else Path(binary_path).resolve()
        )

        if binary_path is None or not binary_path.is_file():
            raise FileNotFoundError(f"Binary not found: {name}")

        self._bin_dir.mkdir(parents=True, exist_ok=True)
        self._lib_dir.mkdir(parents=True, exist_ok=True)

        dest = self._bin_dir / name
        shutil.copy2(str(binary_path), str(dest))
        os.chmod(str(dest), 0o755)

        self._copy_shared_libs(binary_path)
        self._bundled[name] = dest
        return dest

    # ── Python bundling ──────────────────────────────────────────

    def bundle_python(self) -> Path:
        """Bundle the running Python interpreter + standard library.

        The stdlib is copied WITHOUT test suites, IDLE, tkinter, or
        turtle to save space (typically ~30 MB instead of ~100 MB).

        Returns:
            Path to the bundled ``python3`` binary.
        """
        real_exe, stdlib_src = get_python_paths()

        # Bundle the interpreter binary + its shared libs
        python_dest = self.bundle_binary("python3", real_exe)

        # Bundle the standard library
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        stdlib_dest = self._lib_dir / version

        if not stdlib_dest.exists():
            shutil.copytree(
                str(stdlib_src),
                str(stdlib_dest),
                ignore=shutil.ignore_patterns(
                    "test",         # stdlib test suite (~30 MB)
                    "tests",
                    "idle_test",
                    "idlelib",      # IDLE editor
                    "tkinter",      # Tk GUI bindings
                    "turtledemo",   # turtle demos
                    "ensurepip",    # pip bootstrap (not needed)
                    "distutils",    # deprecated packaging
                    "lib2to3",      # Python 2→3 converter
                    "config-*",     # build artifacts (~25 MB)
                    "*.pyc",        # recompiled on import
                    "__pycache__",
                ),
                dirs_exist_ok=True,
            )

        # Bundle shared libs for all C extension modules
        dynload = stdlib_dest / "lib-dynload"
        if dynload.is_dir():
            for so_file in dynload.glob("*.so"):
                self._copy_shared_libs(so_file)

        return python_dest

    # ── Python package bundling ──────────────────────────────────

    def bundle_python_package(self, package_name: str) -> Path | None:
        """Bundle an installed Python package into the meta-volume.

        Copies the package directory (including C extensions and their
        shared libs) into the bundled Python's ``lib/pythonX.Y/``
        directory so that it is importable by the bundled interpreter.

        Args:
            package_name: Top-level package name (e.g. ``"zstandard"``).

        Returns:
            Path to the bundled package directory, or *None* if the
            package is not installed on the build system.
        """
        # Locate the installed package
        pkg_dir = self._find_installed_package(package_name)
        if pkg_dir is None:
            return None

        # Determine destination within bundled stdlib
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        dest_parent = self._lib_dir / version
        dest_parent.mkdir(parents=True, exist_ok=True)
        dest = dest_parent / pkg_dir.name

        if not dest.exists():
            shutil.copytree(
                str(pkg_dir),
                str(dest),
                ignore=shutil.ignore_patterns(
                    "__pycache__",
                    "*.pyc",
                    "tests",
                    "test",
                ),
            )

        # Bundle shared libs for any C extensions in the package
        for so_file in dest.rglob("*.so"):
            self._copy_shared_libs(so_file)

        return dest

    @staticmethod
    def _find_installed_package(package_name: str) -> Path | None:
        """Find the install location of a Python package.

        Returns the directory of the top-level package, or *None* if
        not installed.
        """
        try:
            mod = __import__(package_name)
        except ImportError:
            return None

        # Use the module's __path__ or __file__ to locate the package
        if hasattr(mod, "__path__") and mod.__path__:
            pkg_path = Path(mod.__path__[0])
            if pkg_path.is_dir():
                return pkg_path
        elif hasattr(mod, "__file__") and mod.__file__:
            return Path(mod.__file__).parent

        return None

    # ── Internals ────────────────────────────────────────────────

    def _copy_shared_libs(self, binary: Path) -> None:
        """Copy non-glibc shared libraries required by *binary*."""
        for lib in get_shared_libs(binary):
            dest = self._lib_dir / lib.name
            if not dest.exists():
                shutil.copy2(str(lib), str(dest))
