"""Build a self-contained standalone restorer from source modules.

Concatenates ``_aes_pure.py`` and ``restic_fallback.py`` into a single
file that can be placed on every data disc.  The output has no imports
from the ``lcsas`` package — it is completely self-contained and can run
with nothing but Python ≥ 3.10 stdlib (plus optional ``zstandard``).

Usage::

    from lcsas.restore.standalone_builder import build_standalone

    text = build_standalone()
    Path("standalone_restorer.py").write_text(text)

The generated script supports CLI usage::

    python3 standalone_restorer.py --repo /path/to/cache \\
        --password-file key.txt --target /output
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent


def build_standalone() -> str:
    """Return the text of a self-contained standalone_restorer.py.

    Reads ``_aes_pure.py`` and ``restic_fallback.py`` from the same
    directory, strips ``from lcsas...`` imports, and concatenates them
    with a ``__main__`` CLI block.
    """
    aes_src = (_SRC_DIR / "_aes_pure.py").read_text()
    fallback_src = (_SRC_DIR / "restic_fallback.py").read_text()

    # ── Process _aes_pure.py ─────────────────────────────────────
    # Strip module docstring and __future__ import (will be at top of output)
    aes_lines = _strip_header(aes_src)
    aes_body = "\n".join(aes_lines)

    # ── Process restic_fallback.py ───────────────────────────────
    # Remove the lcsas import line
    fallback_src = re.sub(
        r"^from lcsas\.restore\._aes_pure import \(\n"
        r"(?:    .*\n)*"
        r"\)\n",
        "",
        fallback_src,
        flags=re.MULTILINE,
    )
    # Also handle single-line import form
    fallback_src = re.sub(
        r"^from lcsas\.restore\._aes_pure import .*$\n",
        "",
        fallback_src,
        flags=re.MULTILINE,
    )
    fallback_lines = _strip_header(fallback_src)
    fallback_body = "\n".join(fallback_lines)

    # ── Assemble ─────────────────────────────────────────────────
    return _HEADER + aes_body + "\n\n" + fallback_body + "\n\n" + _CLI_BLOCK


def _strip_header(source: str) -> list[str]:
    """Strip leading docstring and ``from __future__`` imports."""
    lines = source.splitlines()
    out: list[str] = []
    skipping_docstring = False
    past_header = False

    for line in lines:
        stripped = line.strip()
        # Inside a multi-line docstring — keep skipping until close
        if skipping_docstring:
            if '"""' in stripped:
                skipping_docstring = False
            continue
        # Skip blank lines at top
        if not past_header and not stripped:
            continue
        # Skip module docstring
        if not past_header and stripped.startswith('"""'):
            if stripped.count('"""') >= 2:
                # Single-line docstring
                continue
            skipping_docstring = True
            continue
        # Skip __future__ imports
        if stripped.startswith("from __future__"):
            continue
        past_header = True
        out.append(line)

    return out


_HEADER = textwrap.dedent("""\
    #!/usr/bin/env python3
    # ═══════════════════════════════════════════════════════════════════
    #  LCSAS Standalone Pure-Python Restorer
    #
    #  This file is AUTO-GENERATED from:
    #    - src/lcsas/restore/_aes_pure.py   (AES-256-CTR implementation)
    #    - src/lcsas/restore/restic_fallback.py  (restic repo reader)
    #
    #  It restores data from a restic/rustic repository using ONLY
    #  Python 3 standard library (plus optional zstandard for zstd).
    #
    #  Usage:
    #    python3 standalone_restorer.py --repo /path/to/cache \\\\
    #        --password-file key.txt --target /path/to/output
    #
    #  NO pip packages, NO native binaries, NO internet required.
    #  (zstandard pip package needed only for zstd-compressed repos)
    #
    #  Performance: ~1 MB/s — acceptable for emergency recovery.
    # ═══════════════════════════════════════════════════════════════════
    from __future__ import annotations


""")

_CLI_BLOCK = textwrap.dedent("""\
    # ── CLI entry point ──────────────────────────────────────────────

    def _cli_main() -> None:
        import argparse

        parser = argparse.ArgumentParser(
            description="LCSAS standalone pure-Python restic/rustic repo restorer",
            epilog=(
                "Place this script on every data disc so that data can be "
                "recovered even without the meta-volume or any native binaries."
            ),
        )
        parser.add_argument(
            "--repo", required=True,
            help="Path to assembled restic/rustic repository cache",
        )
        parser.add_argument(
            "--password-file", required=True,
            help="Path to the encryption key/password file",
        )
        parser.add_argument(
            "--target", required=True,
            help="Directory to restore files into",
        )
        parser.add_argument(
            "--snapshot", default=None,
            help="Snapshot ID to restore (default: latest)",
        )
        parser.add_argument(
            "--list-snapshots", action="store_true",
            help="List available snapshots and exit",
        )
        parser.add_argument(
            "--info", action="store_true",
            help="Show repository info and exit",
        )
        parser.add_argument(
            "--mount-point", action="append", default=None,
            help=(
                "Additional root to scan for pack files (each may contain "
                "data/<XX>/<hex> or data/<hex>).  Pass once per data disc; "
                "the disc-swap prompt also re-scans these on retry.  "
                "Defaults to <repo>/data only."
            ),
        )
        parser.add_argument(
            "--interactive", choices=("on", "off", "auto"), default="auto",
            help=(
                "Disc-swap prompt mode.  'on' prompts on missing packs; "
                "'off' raises FileNotFoundError; 'auto' (default) is 'on' "
                "iff stdin is a TTY."
            ),
        )

        args = parser.parse_args()

        repo_path = Path(args.repo)
        pw_file = Path(args.password_file)

        if not repo_path.is_dir():
            parser.error(f"Repository path not found: {repo_path}")
        if not pw_file.is_file():
            parser.error(f"Password file not found: {pw_file}")

        if args.interactive == "auto":
            interactive = sys.stdin.isatty()
        else:
            interactive = args.interactive == "on"

        mount_points: list[Path] | None = None
        if args.mount_point:
            # Search the repo's own data/ first, then each mount point in
            # operator-given order.
            mount_points = [repo_path / "data"] + [Path(m) for m in args.mount_point]

        restorer = PurePythonRestorer(
            repo_path=repo_path,
            password_file=pw_file,
            pack_search_paths=mount_points,
            interactive=interactive,
        )

        if args.info:
            import pprint
            info = restorer.repo_info()
            pprint.pprint(info)
            return

        if args.list_snapshots:
            snaps = restorer.list_snapshots()
            if not snaps:
                print("No snapshots found.", file=sys.stderr)
                return
            for snap in snaps:
                print(
                    f"{snap.snapshot_id[:12]}  {snap.time}  "
                    f"{snap.hostname}  {', '.join(snap.paths)}"
                )
            return

        target = Path(args.target)
        restorer.restore(target=target, snapshot_id=args.snapshot)


    if __name__ == "__main__":
        _cli_main()
""")
