"""Write the integrity manifests a meta disc would carry, for review (#894).

``make -C recovery manifest`` runs this.  The manifests are not kept in
git: ``lcsas meta build`` computes them from the files it bundles.  This
copies recovery/ the way the builder does (git-tracked files, minus the
developer records, plus ``bin/`` as it stands) into a temporary folder,
computes both manifests there, and writes them to the output folder.

Usage: ``python3 -m lcsas.meta.manifest_preview <output-dir> [<recovery-dir>]``
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from lcsas.meta.builder import MetaVolumeBuilder, recovery_manifests


def preview_recovery_manifests(recovery: Path, out_dir: Path) -> list[Path]:
    """Write the manifests a meta disc built from *recovery* would carry.

    Returns the paths written: ``<out_dir>/MANIFEST.sha256`` and, when
    recovery/ has a docs/ folder, ``<out_dir>/docs/MANIFEST.sha256``.
    """
    with tempfile.TemporaryDirectory(prefix="lcsas-manifest-") as tmp:
        staged = Path(tmp) / "recovery"
        MetaVolumeBuilder(Path(tmp))._copy_recovery_tree(recovery, staged)
        manifests = recovery_manifests(staged)
    written: list[Path] = []
    for rel, text in manifests.items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2) or argv[0] in ("-h", "--help"):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    recovery = Path(argv[1]) if len(argv) == 2 else Path("recovery")
    for path in preview_recovery_manifests(recovery, Path(argv[0])):
        rows = len(path.read_text(encoding="utf-8").splitlines())
        print(f"wrote {path} ({rows} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
