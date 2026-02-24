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
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from lcsas.config.settings import LCSASConfig
from lcsas.meta.bundler import ToolBundler

# ── Constants ────────────────────────────────────────────────────────

_REQUIRED_TOOLS = ("rustic", "xorriso")
_OPTIONAL_TOOLS = ("dvdisaster",)

# Directories / files to copy from the LCSAS source tree.
_SOURCE_ITEMS = ("src",)
_DOC_ITEMS = ("docs", "README.md", "pyproject.toml")


def _write_and_sync(path: Path, content: str) -> None:
    """Write *content* to *path* and fsync to disk."""
    with open(path, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


def _strip_markdown(text: str) -> str:
    """Best-effort conversion of Markdown to plain text.

    Strips ``#`` headings, ``**bold**``, ``*italic*``, ```code fences```,
    ``| table |`` pipes, and ``> blockquotes`` while preserving structure.
    """
    lines: list[str] = []
    in_code_block = False
    for line in text.splitlines():
        stripped = line.strip()
        # Toggle code fences
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                lines.append("")  # blank line before code
            else:
                lines.append("")  # blank line after code
            continue
        if in_code_block:
            lines.append(line)
            continue
        # Headings → plain uppercase text
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            lines.append("")
            lines.append(heading.upper())
            lines.append("-" * len(heading))
            continue
        # Blockquotes
        if stripped.startswith(">"):
            bq = stripped.lstrip("> ").strip()
            bq = re.sub(r"\*\*(.+?)\*\*", r"\1", bq)
            bq = re.sub(r"\*(.+?)\*", r"\1", bq)
            bq = re.sub(r"`([^`]+)`", r"\1", bq)
            lines.append("  " + bq)
            continue
        # Table rows — keep but remove leading/trailing pipes
        if stripped.startswith("|") and stripped.endswith("|"):
            # Skip separator rows like |---|---|
            if re.match(r"^\|[\s\-:|]+\|$", stripped):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Strip inline formatting from each cell
            clean_cells = []
            for cell in cells:
                cell = re.sub(r"\*\*(.+?)\*\*", r"\1", cell)
                cell = re.sub(r"\*(.+?)\*", r"\1", cell)
                cell = re.sub(r"`([^`]+)`", r"\1", cell)
                clean_cells.append(cell)
            lines.append("  " + "  |  ".join(clean_cells))
            continue
        # Inline formatting
        cleaned = line
        cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)  # **bold**
        cleaned = re.sub(r"\*(.+?)\*", r"\1", cleaned)       # *italic*
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)       # `code`
        lines.append(cleaned)
    return "\n".join(lines) + "\n"


def _get_tool_version(tool_path: Path) -> str:
    """Run *tool_path* with common version flags and return the version string.

    Tries ``--version``, then ``version`` (rustic uses bare ``version``).
    Returns ``"unknown"`` if all attempts fail.
    """
    import subprocess

    for args in ([str(tool_path), "--version"], [str(tool_path), "version"]):
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    **os.environ,
                    "LD_LIBRARY_PATH": str(tool_path.parent.parent / "lib")
                    + ":" + os.environ.get("LD_LIBRARY_PATH", ""),
                },
            )
            if result.returncode == 0 and result.stdout.strip():
                # Return first non-empty line
                for line in result.stdout.strip().splitlines():
                    if line.strip():
                        return line.strip()
        except (subprocess.TimeoutExpired, OSError):
            continue
    return "unknown"


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
#
#  ISO extraction cascade:
#    1. mount -o loop  (kernel-native, needs root)
#    2. 7z x           (widely available, no root)
#    3. bundled xorriso (fallback)
#
#  Rustic binary cascade:
#    1. bundled rustic         (dynamically linked)
#    2. bundled rustic-static  (statically linked, no glibc dependency)
#    3. system rustic          (if installed on host)
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS="$SCRIPT_DIR/tools"

# ── Configure bundled tools ──────────────────────────────────────
export LD_LIBRARY_PATH="${TOOLS}/lib:${LD_LIBRARY_PATH:-}"

# ── Resolve rustic binary (cascade) ─────────────────────────────
RUSTIC=""
if [[ -x "${TOOLS}/bin/rustic" ]] && "${TOOLS}/bin/rustic" version &>/dev/null; then
    RUSTIC="${TOOLS}/bin/rustic"
elif [[ -x "${TOOLS}/bin/rustic-static" ]]; then
    RUSTIC="${TOOLS}/bin/rustic-static"
elif command -v rustic &>/dev/null; then
    RUSTIC="$(command -v rustic)"
elif command -v restic &>/dev/null; then
    RUSTIC="$(command -v restic)"
fi

# ── Resolve Python + standalone restorer (fallback) ─────────────
PYTHON=""
STANDALONE=""
if [[ -x "${TOOLS}/bin/python3" ]]; then
    PYTHON="${TOOLS}/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON="$(command -v python3)"
fi

# Look for standalone_restorer.py shipped on meta-volume, then
# inside any extracted data disc (it's placed on every disc).
if [[ -f "$SCRIPT_DIR/standalone_restorer.py" ]]; then
    STANDALONE="$SCRIPT_DIR/standalone_restorer.py"
fi

# ── ISO extraction function (cascade) ───────────────────────────
extract_iso() {
    local iso="$1" dest="$2"
    mkdir -p "$dest"

    # Method 1: kernel mount (fastest, needs root)
    if [[ $EUID -eq 0 ]] || command -v sudo &>/dev/null; then
        local mnt
        mnt="$(mktemp -d -t lcsas-mnt-XXXXXX)"
        if mount -o loop,ro "$iso" "$mnt" 2>/dev/null || \
           sudo mount -o loop,ro "$iso" "$mnt" 2>/dev/null; then
            cp -a "$mnt"/. "$dest"/
            umount "$mnt" 2>/dev/null || sudo umount "$mnt" 2>/dev/null || true
            rmdir "$mnt" 2>/dev/null || true
            return 0
        fi
        rmdir "$mnt" 2>/dev/null || true
    fi

    # Method 2: 7z (no root needed, widely available)
    if command -v 7z &>/dev/null; then
        if 7z x -o"$dest" "$iso" &>/dev/null; then
            return 0
        fi
    fi

    # Method 3: bundled xorriso (fallback)
    if [[ -x "${TOOLS}/bin/xorriso" ]]; then
        if "${TOOLS}/bin/xorriso" -indev "$iso" -osirrox on -extract / "$dest" 2>/dev/null; then
            return 0
        fi
    fi

    # Method 4: system xorriso
    if command -v xorriso &>/dev/null; then
        if xorriso -indev "$iso" -osirrox on -extract / "$dest" 2>/dev/null; then
            return 0
        fi
    fi

    echo "ERROR: Cannot extract ISO: $iso"
    echo "       Tried: mount, 7z, xorriso — all failed."
    echo "       Install one of: p7zip-full, xorriso, or run as root."
    return 1
}

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

# ── Verify at least one restore method is available ─────────────
USE_PYTHON_FALLBACK=0
if [[ -z "$RUSTIC" ]]; then
    if [[ -n "$PYTHON" ]] && [[ -n "$STANDALONE" ]]; then
        echo "  WARNING: No rustic/restic binary found."
        echo "  Falling back to pure-Python restorer (slower but functional)."
        echo "  Using: $PYTHON $STANDALONE"
        USE_PYTHON_FALLBACK=1
    else
        echo "ERROR: No rustic (or restic) binary found, and no Python"
        echo "       fallback available."
        echo "       Bundled tools may be incompatible with this system."
        echo "       Install rustic (https://rustic.cli.rs/) or"
        echo "       restic (https://restic.net/) and try again."
        echo "       Alternatively, install Python 3.10+ and ensure"
        echo "       standalone_restorer.py is available."
        exit 1
    fi
else
    echo "  Using: $RUSTIC"
fi

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

# ── Trap handler — clean up temp directory on exit/interrupt ─────
_cleanup() {
    if [[ "$CLEANUP_WORK" -eq 1 ]] && [[ -n "$WORK_DIR" ]] && [[ -d "$WORK_DIR" ]]; then
        chmod -R u+w "$WORK_DIR" 2>/dev/null || true
        rm -rf "$WORK_DIR"
    fi
}
trap _cleanup EXIT

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
    extract_iso "$iso" "$dest"
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
        # Discs use two-level layout: data/<prefix>/<sha256>
        for prefix_dir in "$data_dir"/*/; do
            [[ ! -d "$prefix_dir" ]] && continue
            for pack in "$prefix_dir"/*; do
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
    done
    if [[ $PACK_ERRORS -gt 0 ]]; then
        echo "    WARNING: $PACK_ERRORS packs failed SHA-256 verification"
        echo "    Some data discs may be damaged — try redundant copies"
    fi
    echo "    Ingested $PACK_COUNT packs from $ISO_COUNT volumes"

    # ── Verify all required packs were ingested ───────────────
    # Count index entries to estimate expected pack count
    EXPECTED_PACKS=0
    if [[ -d "$CACHE_DIR/index" ]]; then
        EXPECTED_PACKS=$(find "$CACHE_DIR/index" -type f | wc -l)
    fi
    ACTUAL_PACKS=$(find "$CACHE_DIR/data" -type f 2>/dev/null | wc -l)
    if [[ $ACTUAL_PACKS -eq 0 ]]; then
        echo "    ERROR: No packs found in cache — cannot restore $repo"
        echo "    Check that the data discs are correct for this repository."
        exit 1
    fi
    echo "    Cache has $ACTUAL_PACKS data packs"

    # ── Restore: rustic/restic or Python fallback ─────────────
    REPO_TARGET="$TARGET/$repo"
    mkdir -p "$REPO_TARGET"

    if [[ "$USE_PYTHON_FALLBACK" -eq 1 ]]; then
        # ── Pure-Python restore via standalone_restorer.py ─────
        # Find standalone_restorer.py — meta-volume copy (already resolved)
        # or search extracted data discs for a copy
        SR="$STANDALONE"
        if [[ -z "$SR" ]]; then
            for vol_dir_sr in "$EXTRACT_DIR"/*/; do
                if [[ -f "$vol_dir_sr/standalone_restorer.py" ]]; then
                    SR="$vol_dir_sr/standalone_restorer.py"
                    break
                fi
            done
        fi
        if [[ -z "$SR" ]]; then
            echo "    ERROR: standalone_restorer.py not found on any disc."
            exit 1
        fi

        echo "    Running: python3 standalone_restorer.py → $REPO_TARGET"

        # Set up PYTHONPATH for bundled zstandard support
        if [[ -d "${TOOLS}/lib/python" ]]; then
            export PYTHONPATH="${TOOLS}/lib/python:${PYTHONPATH:-}"
        fi
        if "$PYTHON" "$SR" \
                --repo "$CACHE_DIR" \
                --password-file "$KEY_FILE" \
                --target "$REPO_TARGET" 2>&1; then
            echo "    ✓ Restore succeeded (Python fallback)"
        else
            echo "    ✗ Restore FAILED for $repo (Python fallback)"
            echo ""
            echo "  If the error mentions missing packs, you may need"
            echo "  additional data discs for this repository."
            exit 1
        fi
    else
        # ── Native rustic/restic restore (preferred) ──────────
        echo "    Running: rustic restore $SNAPSHOT → $REPO_TARGET"
        # rustic uses positional <destination>; restic uses --target <destination>
        RUSTIC_BIN_NAME="$(basename "$RUSTIC")"
        if [[ "$RUSTIC_BIN_NAME" == rustic* ]]; then
            RESTORE_CMD=("$RUSTIC" restore "$SNAPSHOT" "$REPO_TARGET"
                -r "$CACHE_DIR"
                --password-file "$KEY_FILE"
                --no-cache)
        else
            RESTORE_CMD=("$RUSTIC" restore "$SNAPSHOT"
                -r "$CACHE_DIR"
                --password-file "$KEY_FILE"
                --no-cache
                --target "$REPO_TARGET")
        fi
        if "${RESTORE_CMD[@]}" 2>&1; then
            echo "    ✓ Restore succeeded"
        else
            echo "    ✗ Restore FAILED for $repo"
            echo ""
            echo "  If the error mentions missing packs, you may need"
            echo "  additional data discs for this repository."
            exit 1
        fi
    fi
done

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Restore complete!"
echo "  Output directory: $TARGET"
echo "═══════════════════════════════════════════════════"

# ── Cleanup is handled by the EXIT trap (see _cleanup above) ────
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
| `docs/` | Architecture documentation + restic format specification |
| `restore.sh` | Automated restore script |
| `README_RESTORE.md` | This file |
| `volume_info.json` | Machine-readable volume metadata (includes tool versions) |

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

The script automatically finds the best available tools:
- **ISO extraction:** tries kernel mount, then 7z, then bundled xorriso
- **Decryption:** tries bundled rustic, then rustic-static, then system rustic/restic,
  then pure-Python fallback (standalone_restorer.py)

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

## If the Bundled Tools Don't Work

The bundled tools are Linux x86_64 binaries.  If they don't run on your
system (wrong architecture, incompatible libraries), you have options:

1. **Try rustic-static** — a statically-linked binary may be included
   at `tools/bin/rustic-static` (no shared library dependencies).

2. **Install rustic yourself** — https://rustic.cli.rs/ (or the
   compatible `restic` at https://restic.net/).

3. **Use the LCSAS Python CLI** (advanced):
```bash
export LD_LIBRARY_PATH="$(pwd)/tools/lib:${LD_LIBRARY_PATH:-}"
export PYTHONHOME="$(pwd)/tools"
export PYTHONPATH="$(pwd)/lcsas/src"
./tools/bin/python3 -m lcsas --help
```

4. **Read the format specification** — `docs/RESTIC_FORMAT_SPEC.md`
   documents the restic repository format in detail.  A programmer can
   use this to write a decoder in any language.

5. **Run in a virtual machine** — x86_64 Linux can be emulated on any
   platform using QEMU, VirtualBox, or similar.  Install a basic Linux
   distribution (e.g. Ubuntu) in the VM and use these tools there.

## Notes

- **Pure-Python fallback:** If no rustic/restic binary works on your system,
  `restore.sh` will automatically fall back to `standalone_restorer.py` which
  requires only Python 3.10+ (no compiled extensions).  This is slower (~1 MB/s)
  but functional.  For zstd-compressed repositories (rustic v2 default), the
  `zstandard` Python package is bundled in `tools/lib/python/`.  The fallback
  requires ~2 GB of RAM for large repositories.

- **Re-running after failure:** If a restore is interrupted (power loss, Ctrl+C,
  disk full), simply re-run the restore command.  Temporary files are cleaned up
  automatically.  If using `--work-dir`, delete that directory first to ensure a
  clean state.  Do **not** rely on a partially-restored target directory.

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

See `docs/architecture.md` for the complete system architecture, and
`docs/RESTIC_FORMAT_SPEC.md` for the data format specification.
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
        static_rustic_path: Path | None = None,
        config: LCSASConfig | None = None,
    ) -> None:
        """
        Args:
            output_dir: Where to build the meta-volume directory tree.
            project_root: Root of the LCSAS project (containing ``src/``).
                If *None*, auto-detected from this module's location.
            static_rustic_path: Optional path to a statically-linked
                (musl) rustic binary.  Bundled as ``tools/bin/rustic-static``
                to provide a glibc-independent fallback.
            config: Optional LCSAS configuration.  When provided,
                START_HERE.txt and KEY_INFO.txt are generated on the
                meta-volume using the survivability fields.
        """
        self._output = output_dir
        self._static_rustic_path = static_rustic_path
        self._config = config

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

        # Mark incomplete until all steps succeed
        incomplete_marker = self._output / ".incomplete"
        incomplete_marker.write_text("Meta-volume build in progress\n")

        self._bundle_tools()
        self._bundle_source()
        self._bundle_docs()
        self._bundle_standalone_restorer()
        self._write_restore_script()
        self._write_readme()
        self._write_readme_txt()
        self._write_volume_info()
        self._write_start_here()

        # Build complete — remove the marker
        incomplete_marker.unlink(missing_ok=True)

        return self._output

    # ── Tool bundling ────────────────────────────────────────────

    def _bundle_tools(self) -> None:
        """Bundle rustic, xorriso, and Python with shared libs.

        Also bundles optional tools (dvdisaster) if available on PATH,
        a statically-linked rustic binary if provided, and the
        ``zstandard`` Python package for zstd-compressed repo support.
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

        # Bundle zstandard for pure-Python fallback restore of
        # zstd-compressed repos (rustic v2 default).
        bundler.bundle_python_package("zstandard")

        # Bundle static rustic binary (glibc-independent fallback)
        if self._static_rustic_path is not None:
            src = Path(self._static_rustic_path).resolve()
            if not src.is_file():
                raise FileNotFoundError(
                    f"Static rustic binary not found: {src}"
                )
            dst = bundler.bin_dir / "rustic-static"
            shutil.copy2(str(src), str(dst))
            os.chmod(str(dst), 0o755)

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

    def _bundle_standalone_restorer(self) -> None:
        """Place standalone_restorer.py at the meta-volume root.

        This provides a pure-Python restore path when no rustic/restic
        binary is available.  The script is auto-generated from the
        LCSAS source modules and has zero external dependencies
        (except optional ``zstandard`` for zstd-compressed repos).
        """
        from lcsas.restore.standalone_builder import build_standalone

        restorer_path = self._output / "standalone_restorer.py"
        _write_and_sync(restorer_path, build_standalone())
        os.chmod(str(restorer_path), 0o755)

    def _write_restore_script(self) -> None:
        """Write the bootstrap restore.sh script."""
        script_path = self._output / "restore.sh"
        _write_and_sync(script_path, RESTORE_SCRIPT)
        os.chmod(str(script_path), 0o755)

    def _write_readme(self) -> None:
        """Write the human-readable restore instructions."""
        readme_path = self._output / "README_RESTORE.md"
        _write_and_sync(readme_path, README_RESTORE)

    def _write_readme_txt(self) -> None:
        """Write a plain-text version of README_RESTORE.

        Markdown is hard to read on bare terminals.  This converts
        the Markdown to best-effort plain text by stripping formatting.
        """
        txt = _strip_markdown(README_RESTORE)
        _write_and_sync(self._output / "README_RESTORE.txt", txt)

    def _write_volume_info(self) -> None:
        """Write self-describing volume metadata."""
        # Determine which optional tools were actually bundled
        tools_bin = self._output / "tools" / "bin"
        bundled_tools = list(_REQUIRED_TOOLS) + ["python3"]
        for tool in _OPTIONAL_TOOLS:
            if (tools_bin / tool).exists():
                bundled_tools.append(tool)
        if (tools_bin / "rustic-static").exists():
            bundled_tools.append("rustic-static")

        # Collect tool versions
        tool_versions = {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }
        for tool_name in ("rustic", "xorriso", "dvdisaster"):
            tool_path = tools_bin / tool_name
            if tool_path.exists():
                tool_versions[tool_name] = _get_tool_version(tool_path)
        if (tools_bin / "rustic-static").exists():
            tool_versions["rustic-static"] = _get_tool_version(
                tools_bin / "rustic-static"
            )

        info = {
            "type": "meta",
            "description": "LCSAS rescue volume — tools + source for disaster recovery",
            "created_at": datetime.now(UTC).isoformat(),
            "platform": f"linux-{os.uname().machine}",
            "python_version": (
                f"{sys.version_info.major}.{sys.version_info.minor}"
                f".{sys.version_info.micro}"
            ),
            "contents": {
                "tools": bundled_tools,
                "tool_versions": tool_versions,
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
            f.flush()
            os.fsync(f.fileno())

    def _write_start_here(self) -> None:
        """Write START_HERE.txt to the meta-volume.

        Uses the LCSASConfig survivability fields if a config was
        provided; otherwise writes a generic version.
        """
        from lcsas.staging.metadata import HolographicInjector

        if self._config is not None:
            # Use the full START_HERE generator from HolographicInjector
            injector = HolographicInjector(self._output)
            injector.write_start_here(self._config)
            injector.write_key_info(self._config)
            injector.write_config_summary(self._config)
            injector.write_disc_care()
        else:
            # Write a minimal START_HERE.txt without config context
            injector = HolographicInjector(self._output)
            injector.write_disc_care()
            text = """\
╔══════════════════════════════════════════════════════════╗
║                    START HERE                           ║
╚══════════════════════════════════════════════════════════╝

This is the LCSAS META-VOLUME — it contains all the tools needed
to restore data from the LCSAS archive discs.

TO RESTORE YOUR FILES:

  1. You need the encryption key file (NOT on any disc for security)
  2. You need the data-volume discs (ISO files or optical discs)
  3. Run:  ./restore.sh --key <keyfile> --isos <iso_dir> --target <output>

See README_RESTORE.md for detailed instructions.

IMPORTANT: If this is confusing, take ALL the discs plus the
encryption key to a computer professional.  Any Linux system
administrator or IT professional should be able to follow the
instructions in README_RESTORE.md.

WARNING: WITHOUT THE ENCRYPTION KEY, THE DATA CANNOT BE RECOVERED.
"""
            _write_and_sync(self._output / "START_HERE.txt", text)
