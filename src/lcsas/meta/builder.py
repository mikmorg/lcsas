"""Meta-volume builder — assembles a self-contained rescue volume.

A meta-volume contains everything needed to restore data from LCSAS
archive discs, minus only the encryption key file:

* Portable copies of ``rustic``, ``xorriso``, and ``python3``
  with all required shared libraries.
* The full LCSAS source code.
* A ``restore.sh`` bootstrap script that orchestrates the restore
  using only the bundled tools — no system-installed software required.
* Human-readable ``README_RESTORE.md`` with step-by-step instructions.
* Project documentation (``docs/``).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from lcsas.meta.bundler import ToolBundler

# ── Constants ────────────────────────────────────────────────────────

_REQUIRED_TOOLS = ("rustic", "xorriso")
_OPTIONAL_TOOLS = ("dvdisaster",)

# Directories / files to copy from the LCSAS source tree.
_SOURCE_ITEMS = ("src",)
_DOC_ITEMS = ("docs", "README.md", "pyproject.toml")


# ── Restore script (pure bash — no Python needed for basic restore) ─

RESTORE_SCRIPT = r'''#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  LCSAS Disc-Only Restore — bootstrap script
#
#  Restores data from LCSAS archive volumes using ONLY:
#    1. This meta-volume  (tools + source)
#    2. The data-volume ISOs
#    3. Your encryption key file
#
#  Usage:
#    ./restore.sh --key KEY_FILE --isos ISO_DIR --target TARGET_DIR \
#                 [--repo REPO] [--snapshot ID] [--work-dir DIR]
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS="$SCRIPT_DIR/tools"

# ── Configure bundled tools ──────────────────────────────────────
export LD_LIBRARY_PATH="${TOOLS}/lib:${LD_LIBRARY_PATH:-}"
XORRISO="${TOOLS}/bin/xorriso"
RUSTIC="${TOOLS}/bin/rustic"

# ── Usage ────────────────────────────────────────────────────────
usage() {
    cat <<EOF
LCSAS Disc-Only Restore

Required:
  --key FILE        Path to the encryption key file
  --isos DIR        Directory containing LCSAS .iso files
  --target DIR      Directory to restore files into

Optional:
  --repo NAME       Restore only this repository (default: all)
  --snapshot ID     Snapshot to restore (default: latest)
  --work-dir DIR    Working directory for temporary files (default: auto)
  -h, --help        Show this help

Example:
  ./restore.sh --key ~/secret.key --isos /media/discs/ --target ~/restored/
EOF
}

# ── Parse arguments ──────────────────────────────────────────────
KEY_FILE=""
ISO_DIR=""
TARGET=""
REPO=""
SNAPSHOT="latest"
WORK_DIR=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --key)      KEY_FILE="$2";  shift 2 ;;
        --isos)     ISO_DIR="$2";   shift 2 ;;
        --target)   TARGET="$2";    shift 2 ;;
        --repo)     REPO="$2";      shift 2 ;;
        --snapshot) SNAPSHOT="$2";   shift 2 ;;
        --work-dir) WORK_DIR="$2";  shift 2 ;;
        -h|--help)  usage; exit 0  ;;
        *)          echo "ERROR: Unknown option: $1"; usage; exit 1 ;;
    esac
done

[[ -z "$KEY_FILE" ]] && { echo "ERROR: --key is required";    usage; exit 1; }
[[ -z "$ISO_DIR" ]]  && { echo "ERROR: --isos is required";   usage; exit 1; }
[[ -z "$TARGET" ]]   && { echo "ERROR: --target is required"; usage; exit 1; }

[[ ! -f "$KEY_FILE" ]] && { echo "ERROR: Key file not found: $KEY_FILE"; exit 1; }
[[ ! -d "$ISO_DIR" ]]  && { echo "ERROR: ISO directory not found: $ISO_DIR"; exit 1; }

# ── Verify bundled tools ─────────────────────────────────────────
for tool in "$XORRISO" "$RUSTIC"; do
    if [[ ! -x "$tool" ]]; then
        echo "ERROR: Bundled tool missing or not executable: $tool"
        echo "       This meta-volume may be damaged."
        exit 1
    fi
done

# ── Create work directory ────────────────────────────────────────
CLEANUP_WORK=0
if [[ -z "$WORK_DIR" ]]; then
    WORK_DIR="$(mktemp -d -t lcsas-restore-XXXXXX)"
    CLEANUP_WORK=1
else
    mkdir -p "$WORK_DIR"
fi
EXTRACT_DIR="$WORK_DIR/extracted"
mkdir -p "$EXTRACT_DIR" "$TARGET"

echo "═══════════════════════════════════════════════════"
echo "  LCSAS Disc-Only Restore"
echo "═══════════════════════════════════════════════════"
echo "  Key:       $KEY_FILE"
echo "  ISOs:      $ISO_DIR"
echo "  Target:    $TARGET"
echo "  Work dir:  $WORK_DIR"
echo ""

# ═════════════════════════════════════════════════════════════════
#  Step 1: Extract all ISOs
# ═════════════════════════════════════════════════════════════════
echo "--- Step 1: Extracting ISOs ---"
ISO_COUNT=0
for iso in "$ISO_DIR"/*.iso; do
    [[ ! -f "$iso" ]] && continue
    label="$(basename "$iso" .iso)"
    echo "  [$label]"
    dest="$EXTRACT_DIR/$label"
    mkdir -p "$dest"
    "$XORRISO" -indev "$iso" -osirrox on -extract / "$dest" 2>/dev/null
    ISO_COUNT=$((ISO_COUNT + 1))
done

if [[ $ISO_COUNT -eq 0 ]]; then
    echo "ERROR: No .iso files found in $ISO_DIR"
    exit 1
fi
echo "  Extracted $ISO_COUNT ISOs"
echo ""

# ═════════════════════════════════════════════════════════════════
#  Step 2: Discover repositories from disc metadata
# ═════════════════════════════════════════════════════════════════
echo "--- Step 2: Discovering repositories ---"

# Find the latest volume (last sorted — has the most complete metadata)
LATEST_VOL=""
for vol_dir in $(find "$EXTRACT_DIR" -mindepth 1 -maxdepth 1 -type d | sort); do
    [[ -d "$vol_dir/metadata" ]] && LATEST_VOL="$vol_dir"
done

if [[ -z "$LATEST_VOL" ]]; then
    echo "ERROR: No volume with metadata/ directory found"
    exit 1
fi

echo "  Using metadata from: $(basename "$LATEST_VOL")"

# Build list of repos to restore
declare -a REPOS
for repo_dir in "$LATEST_VOL/metadata"/*/; do
    [[ ! -d "$repo_dir" ]] && continue
    repo_name="$(basename "$repo_dir")"
    REPOS+=("$repo_name")
    echo "  Found repo: $repo_name"
done

if [[ -n "$REPO" ]]; then
    found=0
    for r in "${REPOS[@]}"; do
        [[ "$r" == "$REPO" ]] && found=1
    done
    if [[ $found -eq 0 ]]; then
        echo "ERROR: Repository '$REPO' not found in disc metadata"
        echo "  Available: ${REPOS[*]}"
        exit 1
    fi
    REPOS=("$REPO")
    echo "  Filtering to: $REPO"
fi

echo ""

# ═════════════════════════════════════════════════════════════════
# Step 3: Build restore caches and run rustic restore
# ═════════════════════════════════════════════════════════════════
echo "--- Step 3: Restoring ---"

for repo in "${REPOS[@]}"; do
    echo ""
    echo "  ┌─────────────────────────────────────────┐"
    echo "  │  Restoring: $repo"
    echo "  └─────────────────────────────────────────┘"

    CACHE_DIR="$WORK_DIR/cache_$repo"
    mkdir -p "$CACHE_DIR/data"

    # ── Copy metadata from latest volume ──────────────────────
    META_SRC="$LATEST_VOL/metadata/$repo"
    for subdir in index snapshots keys; do
        if [[ -d "$META_SRC/$subdir" ]]; then
            cp -r "$META_SRC/$subdir" "$CACHE_DIR/$subdir"
        fi
    done
    if [[ -f "$META_SRC/config" ]]; then
        cp "$META_SRC/config" "$CACHE_DIR/config"
    fi

    # ── Copy packs from ALL volumes (two-level layout) ────────
    PACK_COUNT=0
    PACK_ERRORS=0
    for vol_dir in "$EXTRACT_DIR"/*/; do
        data_dir="$vol_dir/data"
        [[ ! -d "$data_dir" ]] && continue
        for pack in "$data_dir"/*; do
            [[ ! -f "$pack" ]] && continue
            sha="$(basename "$pack")"
            prefix="${sha:0:2}"
            mkdir -p "$CACHE_DIR/data/$prefix"
            dst="$CACHE_DIR/data/$prefix/$sha"
            if [[ ! -f "$dst" ]]; then
                cp "$pack" "$dst"
                # Verify SHA-256 of copied pack matches its filename
                actual_sha="$(sha256sum "$dst" | cut -d' ' -f1)"
                if [[ "$actual_sha" != "$sha" ]]; then
                    echo "    ✗ SHA-256 MISMATCH: $sha (got $actual_sha)"
                    rm -f "$dst"
                    PACK_ERRORS=$((PACK_ERRORS + 1))
                else
                    PACK_COUNT=$((PACK_COUNT + 1))
                fi
            fi
        done
    done
    if [[ $PACK_ERRORS -gt 0 ]]; then
        echo "    WARNING: $PACK_ERRORS packs failed SHA-256 verification"
        echo "    Some data discs may be damaged — try redundant copies"
    fi
    echo "    Ingested $PACK_COUNT packs from $ISO_COUNT volumes"

    # ── rustic restore ────────────────────────────────────────
    REPO_TARGET="$TARGET/$repo"
    mkdir -p "$REPO_TARGET"

    echo "    Running: rustic restore $SNAPSHOT → $REPO_TARGET"
    if "$RUSTIC" restore "$SNAPSHOT" \
         -r "$CACHE_DIR" \
         --password-file "$KEY_FILE" \
         --no-cache \
         --target "$REPO_TARGET" 2>&1; then
        echo "    ✓ Restore succeeded"
    else
        echo "    ✗ Restore FAILED for $repo"
        echo ""
        echo "  If the error mentions missing packs, you may need"
        echo "  additional data discs for this repository."
        exit 1
    fi
done

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Restore complete!"
echo "  Output directory: $TARGET"
echo "═══════════════════════════════════════════════════"

# ── Cleanup ──────────────────────────────────────────────────────
if [[ "$CLEANUP_WORK" -eq 1 ]]; then
    chmod -R u+w "$WORK_DIR" 2>/dev/null || true
    rm -rf "$WORK_DIR"
fi
'''


README_RESTORE = '''\
# LCSAS Disaster Recovery — Restore from Discs

This volume contains everything you need to restore data archived by
**LCSAS** (Linux Cold Storage Archival Suite) from optical discs or ISOs.
The **only** thing you must provide is your **encryption key file**.

## What's on This Volume

| Path | Description |
|---|---|
| `tools/` | Portable Linux x86_64 binaries: rustic, xorriso, Python 3 |
| `lcsas/` | LCSAS source code (Python, no external dependencies) |
| `docs/` | Architecture documentation |
| `restore.sh` | Automated restore script |
| `README_RESTORE.md` | This file |
| `volume_info.json` | Machine-readable volume metadata |

## Quick Start

### 1. Mount This Volume

Physical disc:
```
sudo mount /dev/sr0 /mnt/meta
```

ISO file:
```
sudo mount -o loop meta.iso /mnt/meta
```

> **Tip:** If binaries fail to execute, remount with exec permissions:
> `sudo mount -o remount,exec /mnt/meta`
> Or copy the meta-volume to local disk first:
> `cp -r /mnt/meta /tmp/lcsas-meta && cd /tmp/lcsas-meta`

### 2. Run the Restore

```bash
./restore.sh --key /path/to/your/keyfile.txt \\
             --isos /path/to/iso/directory/ \\
             --target /path/to/restore/output/
```

### 3. Verify

Your restored files are organized by repository under the target directory.

## Restore Options

| Option | Description |
|---|---|
| `--key FILE` | **(required)** Path to your encryption key file |
| `--isos DIR` | **(required)** Directory containing `.iso` files |
| `--target DIR` | **(required)** Where to restore files |
| `--repo NAME` | Restore only one repository (default: all) |
| `--snapshot ID` | Restore a specific snapshot (default: latest) |
| `--work-dir DIR` | Temporary work directory (default: auto) |

### Examples

Restore everything:
```bash
./restore.sh --key ~/secret.key --isos ./discs/ --target ~/restored/
```

Restore only the "family" repository:
```bash
./restore.sh --key ~/secret.key --isos ./discs/ --target ~/restored/ --repo family
```

## Using the LCSAS CLI (Advanced)

The bundled Python and LCSAS source enable advanced catalog queries:

```bash
export LD_LIBRARY_PATH="$(pwd)/tools/lib:${LD_LIBRARY_PATH:-}"
export PYTHONHOME="$(pwd)/tools"
export PYTHONPATH="$(pwd)/lcsas/src"
./tools/bin/python3 -m lcsas --help
```

## What Is NOT on This Volume

**Your encryption key file** — you must provide this yourself.
Without the key file, the encrypted backup data cannot be decrypted.

> **Important:** Store your key file securely and *separately* from
> your backup discs. Consider printing it on paper and storing in a
> fireproof safe, or splitting it across multiple secure locations.

## About LCSAS

Linux Cold Storage Archival Suite orchestrates Rustic (restic-compatible) backup
repositories onto optical media (Blu-ray, M-DISC) and tape (LTO) for
long-term archival storage. Every data disc is self-describing ("holographic"),
carrying full repository metadata so that any disc can bootstrap a restore
independently.

See `docs/architecture.md` for the complete system architecture.
'''


class MetaVolumeBuilder:
    """Assembles a self-contained rescue volume.

    Usage::

        builder = MetaVolumeBuilder(Path("/tmp/meta"))
        meta_root = builder.build()
        # meta_root is ready for ISO mastering via xorriso

    The meta-volume layout::

        output_dir/
        ├── tools/
        │   ├── bin/          rustic, xorriso, python3
        │   └── lib/          shared libs + python stdlib
        ├── lcsas/
        │   └── src/lcsas/    LCSAS Python package
        ├── docs/             architecture docs
        ├── restore.sh        bootstrap script
        ├── README_RESTORE.md human instructions
        └── volume_info.json  self-describing metadata
    """

    def __init__(
        self,
        output_dir: Path,
        project_root: Path | None = None,
    ) -> None:
        """
        Args:
            output_dir: Where to build the meta-volume directory tree.
            project_root: Root of the LCSAS project (containing ``src/``).
                If *None*, auto-detected from this module's location.
        """
        self._output = output_dir

        if project_root is None:
            # meta/ → lcsas/ → src/ → (project root)
            self._project_root = Path(__file__).resolve().parents[3]
        else:
            self._project_root = project_root.resolve()

    @property
    def output_dir(self) -> Path:
        return self._output

    @property
    def project_root(self) -> Path:
        return self._project_root

    def build(self) -> Path:
        """Build the complete meta-volume.

        Returns:
            Path to the meta-volume root directory.
        """
        self._output.mkdir(parents=True, exist_ok=True)

        self._bundle_tools()
        self._bundle_source()
        self._bundle_docs()
        self._write_restore_script()
        self._write_readme()
        self._write_volume_info()

        return self._output

    # ── Tool bundling ────────────────────────────────────────────

    def _bundle_tools(self) -> None:
        """Bundle rustic, xorriso, and Python with shared libs.

        Also bundles optional tools (dvdisaster) if available on PATH.
        """
        tools_dir = self._output / "tools"
        bundler = ToolBundler(tools_dir)

        for tool in _REQUIRED_TOOLS:
            bundler.bundle_binary(tool)

        for tool in _OPTIONAL_TOOLS:
            import shutil as _shutil
            if _shutil.which(tool):
                bundler.bundle_binary(tool)

        bundler.bundle_python()

    # ── Source bundling ──────────────────────────────────────────

    def _bundle_source(self) -> None:
        """Copy the LCSAS source tree into the meta-volume."""
        lcsas_dir = self._output / "lcsas"

        for item_name in _SOURCE_ITEMS:
            src = self._project_root / item_name
            dst = lcsas_dir / item_name
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(str(dst))
                shutil.copytree(
                    str(src),
                    str(dst),
                    ignore=shutil.ignore_patterns(
                        "__pycache__",
                        "*.pyc",
                        "*.egg-info",
                        ".git",
                    ),
                )
            elif src.is_file():
                lcsas_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))

    def _bundle_docs(self) -> None:
        """Copy documentation into the meta-volume."""
        for item_name in _DOC_ITEMS:
            src = self._project_root / item_name
            dst = self._output / item_name
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(str(dst))
                shutil.copytree(
                    str(src),
                    str(dst),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))

    # ── Script / doc generation ──────────────────────────────────

    def _write_restore_script(self) -> None:
        """Write the bootstrap restore.sh script."""
        script_path = self._output / "restore.sh"
        script_path.write_text(RESTORE_SCRIPT)
        os.chmod(str(script_path), 0o755)

    def _write_readme(self) -> None:
        """Write the human-readable restore instructions."""
        readme_path = self._output / "README_RESTORE.md"
        readme_path.write_text(README_RESTORE)

    def _write_volume_info(self) -> None:
        """Write self-describing volume metadata."""
        # Determine which optional tools were actually bundled
        tools_bin = self._output / "tools" / "bin"
        bundled_tools = list(_REQUIRED_TOOLS) + ["python3"]
        for tool in _OPTIONAL_TOOLS:
            if (tools_bin / tool).exists():
                bundled_tools.append(tool)

        info = {
            "type": "meta",
            "description": "LCSAS rescue volume — tools + source for disaster recovery",
            "created_at": datetime.now(UTC).isoformat(),
            "platform": f"linux-{os.uname().machine}",
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "contents": {
                "tools": bundled_tools,
                "lcsas_source": True,
                "restore_script": "restore.sh",
                "documentation": True,
            },
            "requires": {
                "key_file": "User must provide the encryption key file",
                "data_isos": "LCSAS data-volume ISO files",
            },
        }
        info_path = self._output / "volume_info.json"
        with open(info_path, "w") as f:
            json.dump(info, f, indent=2)
