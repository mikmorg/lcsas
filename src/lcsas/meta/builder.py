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
from datetime import UTC, date, datetime
from pathlib import Path

from lcsas.config.settings import LCSASConfig
from lcsas.meta.bundler import ToolBundler
from lcsas.meta.required_contents import (
    APPROVED_TARGETS,
    required_meta_paths,
)

# ── Constants ────────────────────────────────────────────────────────


class MetaBuildError(Exception):
    """A meta-volume could not be built completely / safely."""


_REQUIRED_TOOLS = ("rustic", "xorriso")
_OPTIONAL_TOOLS = ("dvdisaster",)

# Directories / files to copy from the LCSAS source tree.
_SOURCE_ITEMS = ("src",)
_DOC_ITEMS = ("docs", "README.md", "pyproject.toml")

# FMT-02: the pinned dvdisaster RS03 source tarball is bundled on every
# meta-volume so a future engineer can re-implement RS03 repair from the
# exact source docs/DVDISASTER_RS03_FORMAT.md transcribes.  The filename
# is the single source of truth in recovery/UPSTREAM.sha256
# (category ``dvdisaster/src/``); we read it from there rather than
# hard-coding the version twice.
_DVDISASTER_SOURCE_SUBDIR = "tools/src"


def pinned_dvdisaster_source_name(recovery_dir: Path) -> str | None:
    """Return the dvdisaster source tarball filename pinned in UPSTREAM.sha256.

    Reads ``recovery/UPSTREAM.sha256`` and returns the basename of the
    single ``dvdisaster/src/<filename>`` entry, or ``None`` if the
    manifest has no dvdisaster source pin.
    """
    manifest = recovery_dir / "UPSTREAM.sha256"
    if not manifest.is_file():
        return None
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        relpath = parts[-1]
        if relpath.startswith("dvdisaster/src/"):
            return relpath.rsplit("/", 1)[-1]
    return None


def _write_and_sync(path: Path, content: str) -> None:
    """Write *content* to *path* and fsync to disk."""
    with open(path, "w", encoding="utf-8") as f:
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
            heading = re.sub(r"\*\*(.+?)\*\*", r"\1", heading)
            heading = re.sub(r"\*(.+?)\*", r"\1", heading)
            heading = re.sub(r"`([^`]+)`", r"\1", heading)
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
#
# Compatibility status: this bash heredoc is the LEGACY driver.  When
# ``recovery/scripts/restore.sh`` (the POSIX-sh / 3-tier driver) is
# available — which is normally the case — meta-builder writes it as
# the meta-volume's primary ``/restore.sh`` and this heredoc is
# written alongside as ``/restore_legacy.sh`` for operators who still
# want the explicit ``--key/--isos/--target`` flag interface.
#
# Two integration tests pin to this legacy contract:
#   * tests/integration/test_meta_volume_restore.py — exercises full
#     end-to-end ISO restore via the flag-based CLI
#   * tests/unit/test_meta_builder.py — asserts the legacy ``ACTUAL_PACKS``
#     pack-count check and the single-drive ``INSERT DISC:`` UX
# Before deleting this constant, retarget those tests at the new driver
# (or delete the legacy-only ones outright).

RESTORE_SCRIPT = r'''#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  LCSAS Disc-Only Restore — bootstrap script
#
#  Restores data from LCSAS archive volumes using ONLY:
#    1. This meta-volume  (tools + source)
#    2. The data-volume discs (or ISOs)
#    3. Your encryption key file
#
#  Two modes:
#    Single-drive (DEFAULT) — models the disaster scenario: you own one
#      optical drive and a stack of archive discs. Script prompts for
#      each disc by label, reads it in place, and ingests only the
#      packs needed for the target repository.
#
#        ./restore.sh --key KEY_FILE --target TARGET [--repo REPO]
#                     [--drive /dev/sr0] [--snapshot ID]
#
#    Directory (opt-in, legacy) — you already have every ISO on disk.
#      Script extracts them all and runs the classic flow.
#
#        ./restore.sh --key KEY_FILE --isos ISO_DIR --target TARGET
#                     [--repo REPO] [--snapshot ID]
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
LCSAS Disaster Recovery Restore

Single-drive mode (DEFAULT):
  ./restore.sh --key KEY_FILE --target TARGET [--repo NAME]
               [--drive /dev/sr0] [--snapshot ID]

  Insert any LCSAS archive disc into the drive. The script reads
  the catalog, tells you which discs to insert next, and restores
  the repository onto disk. Only one disc is mounted at a time.

Directory mode (opt-in, legacy):
  ./restore.sh --key KEY_FILE --isos ISO_DIR --target TARGET
               [--repo NAME] [--snapshot ID]

  Use when every data-volume ISO is already on disk.

Options:
  --key FILE        (required) Path to the encryption key file
  --target DIR      (required) Where to restore files
  --repo NAME       Repository (a.k.a. tenant) to restore
  --snapshot ID     Snapshot to restore (default: latest)
  --drive DEV       Optical drive in single-drive mode (default: /dev/sr0)
  --isos DIR        Opt-in: directory of data-volume ISOs (legacy mode)
  --work-dir DIR    Temp directory (default: auto)
  -h, --help        Show this help
EOF
}

# ── Parse arguments ──────────────────────────────────────────────
KEY_FILE=""
ISO_DIR=""
TARGET=""
REPO=""
SNAPSHOT="latest"
WORK_DIR=""
DRIVE="/dev/sr0"

while [[ $# -gt 0 ]]; do
    case $1 in
        --key)      KEY_FILE="$2";  shift 2 ;;
        --isos)     ISO_DIR="$2";   shift 2 ;;
        --target)   TARGET="$2";    shift 2 ;;
        --repo)     REPO="$2";      shift 2 ;;
        --snapshot) SNAPSHOT="$2";  shift 2 ;;
        --drive)    DRIVE="$2";     shift 2 ;;
        --work-dir) WORK_DIR="$2";  shift 2 ;;
        -h|--help)  usage; exit 0  ;;
        *)          echo "ERROR: Unknown option: $1"; usage; exit 1 ;;
    esac
done

[[ -z "$KEY_FILE" ]] && { echo "ERROR: --key is required";    usage; exit 1; }
[[ -z "$TARGET" ]]   && { echo "ERROR: --target is required"; usage; exit 1; }
[[ ! -f "$KEY_FILE" ]] && { echo "ERROR: Key file not found: $KEY_FILE"; exit 1; }

# Mode selection: --isos present → directory mode; else single-drive.
MODE="single-drive"
if [[ -n "$ISO_DIR" ]]; then
    MODE="directory"
    [[ ! -d "$ISO_DIR" ]] && { echo "ERROR: ISO directory not found: $ISO_DIR"; exit 1; }
fi

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
echo "  LCSAS Disaster Recovery Restore  ($MODE mode)"
echo "═══════════════════════════════════════════════════"
echo "  Key:       $KEY_FILE"
if [[ "$MODE" == "single-drive" ]]; then
    echo "  Drive:     $DRIVE"
else
    echo "  ISOs:      $ISO_DIR"
fi
echo "  Target:    $TARGET"
echo "  Work dir:  $WORK_DIR"
echo ""

# ═════════════════════════════════════════════════════════════════
#  Single-drive mode — handle entirely here, then exit.
# ═════════════════════════════════════════════════════════════════
if [[ "$MODE" == "single-drive" ]]; then
    if [[ -z "$PYTHON" ]]; then
        echo "ERROR: no python3 available — single-drive mode needs"
        echo "       tools/bin/python3 (bundled) or a system python3."
        exit 1
    fi
    HELPER="$TOOLS/restore_single_drive.py"
    if [[ ! -f "$HELPER" ]]; then
        echo "ERROR: single-drive helper not found at $HELPER"
        exit 1
    fi

    MNT="$WORK_DIR/mnt"
    mkdir -p "$MNT"

    _sudo() {
        if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
    }
    mount_drive() {
        _sudo mount -o ro "$DRIVE" "$MNT"
    }
    umount_drive() {
        _sudo umount "$MNT" 2>/dev/null || true
    }
    eject_drive() {
        if command -v eject &>/dev/null; then eject "$DRIVE" &>/dev/null || true; fi
    }
    disc_label() {
        # Try blkid first (needs root on some systems).
        if command -v blkid &>/dev/null; then
            local lbl
            lbl="$(_sudo blkid -o value -s LABEL "$DRIVE" 2>/dev/null || true)"
            if [[ -n "$lbl" ]]; then
                echo "$lbl"
                return
            fi
        fi
        # Fallback: read volume_info.json from the mounted disc.
        if mountpoint -q "$MNT" 2>/dev/null && [[ -f "$MNT/volume_info.json" ]]; then
            "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("label", ""))
' "$MNT/volume_info.json" 2>/dev/null || true
        fi
    }
    DISC_IDX=0
    DISC_TOTAL=0
    PACKS_TOTAL=0
    PACKS_CACHED=0
    set_title() {
        # Set terminal title bar — visible even when minimized.
        printf '\\033]0;LCSAS: %s\\007' "$1" 2>/dev/null || true
    }
    reset_title() {
        printf '\\033]0;%s\\007' "LCSAS restore" 2>/dev/null || true
    }
    show_prompt_block() {
        local want="$1"
        local pct=0
        if [[ "$PACKS_TOTAL" -gt 0 ]]; then
            pct=$(( PACKS_CACHED * 100 / PACKS_TOTAL ))
        fi
        echo ""
        echo "╔═══════════════════════════════════════════════════╗"
        printf '║  INSERT DISC: %-36s ║\\n' "$want"
        printf '║  Drive: %-41s ║\\n' "$DRIVE"
        printf '║  Progress: %d/%d packs (%d%%)%-22s ║\\n' "$PACKS_CACHED" "$PACKS_TOTAL" "$pct" ""
        local remain=$(( DISC_TOTAL - DISC_IDX ))
        printf '║  Discs remaining: %d of %d%-24s ║\\n' "$remain" "$DISC_TOTAL" ""
        echo "╚═��══════════��══════════════════════════════════════╝"
        set_title "insert $want ($pct%)"
    }
    prompt_insert() {
        local want="$1"
        # If the wanted disc is already mounted, skip the swap prompt.
        if [[ -n "$want" ]] && mountpoint -q "$MNT" 2>/dev/null; then
            local cur
            cur="$(disc_label)"
            if [[ "$cur" == "$want" ]]; then
                return 0
            fi
        fi
        while :; do
            umount_drive
            eject_drive
            show_prompt_block "$want"
            local reply=""
            # Re-prompt every 60s so the disc label stays visible.
            while :; do
                local pmsg="Press Enter once loaded (or 'skip' to abort): "
                if read -r -t 60 -p "$pmsg" reply 2>/dev/null; then
                    break
                fi
                # Timeout — reprint the prompt block.
                show_prompt_block "$want"
            done
            if [[ "$reply" == "skip" ]]; then
                echo "  Skipping $want — finalize will report any missing packs."
                reset_title
                return 1
            fi
            if ! mount_drive; then
                echo "WRONG DISC: drive not readable — try again."
                continue
            fi
            local got
            got="$(disc_label)"
            if [[ -n "$want" ]] && [[ -n "$got" ]] && [[ "$got" != "$want" ]]; then
                echo "WRONG DISC: expected $want, got $got"
                continue
            fi
            return 0
        done
    }

    CACHE_DIR="$WORK_DIR/cache"
    mkdir -p "$CACHE_DIR"

    # Phase 1 — bootstrap. The meta disc carries Rustic metadata
    # (keys, config) but NO catalog — it would always be stale.
    # The operator inserts any data disc and we bootstrap from its
    # catalog. If a later disc has a fresher catalog, we upgrade
    # organically during Phase 2.
    echo ""
    echo "--- Phase 1: Bootstrap ---"
    # Seed keys/config from meta disc if available (they don't go stale).
    BOOTSTRAP_META=""
    if [[ -d "$SCRIPT_DIR/metadata" ]]; then
        BOOTSTRAP_META="$SCRIPT_DIR/metadata"
    fi
    echo "  Insert any LCSAS archive disc to begin."
    echo "  (Tip: the highest-numbered disc has the freshest catalog.)"
    prompt_insert ""
    CATALOG="$MNT/catalog.db"
    if [[ ! -f "$CATALOG" ]]; then
        echo "ERROR: $CATALOG not found — this does not look like an"
        echo "       LCSAS archive disc."
        umount_drive
        exit 1
    fi
    BOOTSTRAP_MNT="$MNT"

    BOOTSTRAP_ARGS=(--catalog "$CATALOG" --mount "$BOOTSTRAP_MNT" --cache "$CACHE_DIR")
    [[ -n "$REPO" ]] && BOOTSTRAP_ARGS+=(--repo "$REPO")
    if ! "$PYTHON" "$HELPER" bootstrap "${BOOTSTRAP_ARGS[@]}" > "$WORK_DIR/pick-list.json"; then
        rc=$?
        if [[ $rc -eq 2 ]]; then
            echo ""
            echo "Re-run with: ./restore.sh --key $KEY_FILE --target $TARGET --repo NAME"
            umount_drive
            exit 2
        fi
        echo "ERROR: bootstrap failed (exit $rc)"
        umount_drive
        exit 1
    fi

    # Extract the ordered list of volume labels from the pick list.
    mapfile -t VOLUMES < <(
        "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for v in data["volumes"]:
    print(v["label"])
' "$CACHE_DIR/pick-list.json"
    )
    RESOLVED_REPO="$(
        "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f)["repo"])
' "$CACHE_DIR/pick-list.json"
    )"
    PACKS_TOTAL="$(
        "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("total_packs", 0))
' "$CACHE_DIR/pick-list.json"
    )"
    CATALOG_FRESH="$(
        "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("catalog_freshness", ""))
' "$CACHE_DIR/pick-list.json"
    )"
    echo "  Repository: $RESOLVED_REPO"
    echo "  Discs needed: ${#VOLUMES[@]}"
    echo "  Total packs:  $PACKS_TOTAL"
    for v in "${VOLUMES[@]}"; do echo "    • $v"; done

    # Check for resumed state.
    if [[ -f "$CACHE_DIR/restore-state.json" ]]; then
        PACKS_CACHED="$(
            "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    s = json.load(f)
print(s.get("packs_ingested", 0))
' "$CACHE_DIR/restore-state.json"
        )"
        echo ""
        echo "  Resuming: $PACKS_CACHED/$PACKS_TOTAL packs already cached"
    fi

    # Phase 2 — ingest, one disc at a time. prompt_insert is a no-op if
    # the wanted disc is already the one in the drive. Uses an index-based
    # while loop so that organic catalog upgrades (which may extend VOLUMES)
    # take effect mid-iteration.
    echo ""
    echo "--- Phase 2: Ingest ---"
    DISC_TOTAL=${#VOLUMES[@]}
    IDX=0
    while [[ $IDX -lt ${#VOLUMES[@]} ]]; do
        label="${VOLUMES[$IDX]}"
        IDX=$((IDX + 1))
        DISC_IDX=$IDX
        if ! prompt_insert "$label"; then
            echo "  Skipped disc $label"
            continue
        fi

        # ── Organic catalog upgrade ──
        # If this data disc has a fresher catalog than the one we
        # bootstrapped from, re-bootstrap to get an updated pick list.
        # This handles the common case where the meta disc was burned
        # before the last data discs and its catalog is stale.
        DISC_CATALOG="$MNT/catalog.db"
        if [[ -f "$DISC_CATALOG" ]]; then
            DISC_FRESH="$("$PYTHON" -c "
import sqlite3, sys
c = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro&immutable=1', uri=True)
print(c.execute('SELECT MAX(created_at) FROM volumes').fetchone()[0] or '')
c.close()
" "$DISC_CATALOG")"
            if [[ "$DISC_FRESH" > "$CATALOG_FRESH" ]]; then
                echo "  Fresher catalog on $label — upgrading pick list..."
                if "$PYTHON" "$HELPER" bootstrap \
                    --catalog "$DISC_CATALOG" --mount "$MNT" \
                    --cache "$CACHE_DIR" --repo "$RESOLVED_REPO" \
                    --reseed \
                    > /dev/null 2>"$WORK_DIR/upgrade-err.txt"; then
                    # Success — re-read state from upgraded pick list.
                    mapfile -t VOLUMES < <(
                        "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for v in data["volumes"]:
    print(v["label"])
' "$CACHE_DIR/pick-list.json"
                    )
                    CATALOG_FRESH="$DISC_FRESH"
                    PACKS_TOTAL="$(
                        "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("total_packs", 0))
' "$CACHE_DIR/pick-list.json"
                    )"
                    DISC_TOTAL=${#VOLUMES[@]}
                else
                    echo "  WARNING: catalog upgrade failed, continuing with existing catalog"
                    cat "$WORK_DIR/upgrade-err.txt" 2>/dev/null || true
                fi
            fi
        fi

        "$PYTHON" "$HELPER" ingest --mount "$MNT" --cache "$CACHE_DIR" --disc-label "$label" || {
            echo "  WARNING: ingest phase reported issues for $label"
        }
        # Update progress from state file.
        if [[ -f "$CACHE_DIR/restore-state.json" ]]; then
            PACKS_CACHED="$(
                "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    s = json.load(f)
print(s.get("packs_ingested", 0))
' "$CACHE_DIR/restore-state.json"
            )"
        fi
    done

    umount_drive
    eject_drive
    reset_title

    # Phase 3 — verify completeness and integrity.
    echo ""
    echo "--- Phase 3: Finalize ---"
    "$PYTHON" "$HELPER" finalize --cache "$CACHE_DIR" --verify-integrity
    FINALIZE_RC=$?
    if [[ $FINALIZE_RC -eq 3 ]]; then
        echo ""
        echo "FATAL: some packs have no remaining alternate discs."
        echo "       This restore cannot complete with the available media."
        echo "       Contact your backup administrator."
        exit 3
    elif [[ $FINALIZE_RC -ne 0 ]]; then
        echo ""
        echo "ERROR: cache is incomplete. Re-run restore.sh with the missing"
        echo "       discs available and the helper will pick up where it left off."
        exit 1
    fi

    # Phase 4 — run rustic restore against the assembled cache.
    echo ""
    echo "--- Phase 4: rustic restore ---"
    REPO_TARGET="$TARGET/$RESOLVED_REPO"
    mkdir -p "$REPO_TARGET"

    if [[ "$USE_PYTHON_FALLBACK" -eq 1 ]]; then
        SR="$STANDALONE"
        if [[ -z "$SR" ]]; then
            echo "ERROR: standalone_restorer.py not found and no rustic available."
            exit 1
        fi
        if [[ -d "${TOOLS}/lib/python" ]]; then
            export PYTHONPATH="${TOOLS}/lib/python:${PYTHONPATH:-}"
        fi
        "$PYTHON" "$SR" --repo "$CACHE_DIR" --password-file "$KEY_FILE" --target "$REPO_TARGET"
    else
        RUSTIC_BIN_NAME="$(basename "$RUSTIC")"
        if [[ "$RUSTIC_BIN_NAME" == rustic* ]]; then
            "$RUSTIC" restore "$SNAPSHOT" "$REPO_TARGET" \
                -r "$CACHE_DIR" --password-file "$KEY_FILE" --no-cache
        else
            "$RUSTIC" restore "$SNAPSHOT" \
                -r "$CACHE_DIR" --password-file "$KEY_FILE" --no-cache \
                --target "$REPO_TARGET"
        fi
    fi

    reset_title
    echo ""
    echo "═══════════════════════════════════════════════════"
    echo "  RESTORE COMPLETE"
    echo "  Output: $REPO_TARGET"
    echo "═══════════════════════════════════════════════════"
    exit 0
fi

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


RESTORE_AUTO_SCRIPT = r'''#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  LCSAS Non-Interactive Restore — automated disc-swap restore
#
#  Drives the same bootstrap → ingest → finalize → rustic pipeline
#  as restore.sh but without interactive prompts.  Designed for:
#    • Scripted / automated restore environments
#    • AI agent-driven restores
#    • CI/CD test harnesses
#
#  Disc loading is delegated to a user-supplied command via --disc-cmd.
#  The command is called as:  $DISC_CMD insert <LABEL>
#                              $DISC_CMD eject
#
#  If --disc-cmd is omitted, the script assumes the operator will
#  load discs externally and waits for the drive to become readable.
#
#  Usage:
#    ./restore-auto.sh --key KEY_FILE --target TARGET --repo NAME \
#                      [--drive /dev/sr0] [--disc-cmd CMD] \
#                      [--snapshot ID] [--work-dir DIR]
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
if [[ -f "$SCRIPT_DIR/standalone_restorer.py" ]]; then
    STANDALONE="$SCRIPT_DIR/standalone_restorer.py"
fi

# ── Usage ────────────────────────────────────────────────────────
usage() {
    cat <<EOF
LCSAS Non-Interactive Restore (automated disc-swap)

Usage:
  ./restore-auto.sh --key KEY_FILE --target TARGET --repo NAME \\
                    [--drive /dev/sr0] [--disc-cmd CMD] \\
                    [--snapshot ID] [--work-dir DIR]

Options:
  --key FILE        (required) Path to the encryption key file
  --target DIR      (required) Where to restore files
  --repo NAME       (required) Repository (tenant) to restore
  --snapshot ID     Snapshot to restore (default: latest)
  --drive DEV       Optical drive device (default: /dev/sr0)
  --disc-cmd CMD    Command to load/eject discs. Called as:
                      CMD insert LABEL   — load a disc by label
                      CMD eject          — eject current disc
                    If omitted, discs must be loaded externally.
  --work-dir DIR    Temp directory (default: auto)
  -h, --help        Show this help
EOF
}

# ── Parse arguments ──────────────────────────────────────────────
KEY_FILE=""
TARGET=""
REPO=""
SNAPSHOT="latest"
WORK_DIR=""
DRIVE="/dev/sr0"
DISC_CMD=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --key)      KEY_FILE="$2";  shift 2 ;;
        --target)   TARGET="$2";    shift 2 ;;
        --repo)     REPO="$2";      shift 2 ;;
        --snapshot) SNAPSHOT="$2";  shift 2 ;;
        --drive)    DRIVE="$2";     shift 2 ;;
        --disc-cmd) DISC_CMD="$2";  shift 2 ;;
        --work-dir) WORK_DIR="$2";  shift 2 ;;
        -h|--help)  usage; exit 0  ;;
        *)          echo "ERROR: Unknown option: $1"; usage; exit 1 ;;
    esac
done

[[ -z "$KEY_FILE" ]] && { echo "ERROR: --key is required";    usage; exit 1; }
[[ -z "$TARGET" ]]   && { echo "ERROR: --target is required"; usage; exit 1; }
[[ -z "$REPO" ]]     && { echo "ERROR: --repo is required";   usage; exit 1; }
[[ ! -f "$KEY_FILE" ]] && { echo "ERROR: Key file not found: $KEY_FILE"; exit 1; }

# ── Python required for non-interactive mode ─────────────────────
if [[ -z "$PYTHON" ]]; then
    echo "ERROR: no python3 available — needed for disc-swap helper."
    exit 1
fi
HELPER="$TOOLS/restore_single_drive.py"
if [[ ! -f "$HELPER" ]]; then
    echo "ERROR: restore_single_drive.py not found at $HELPER"
    exit 1
fi

# ── Verify at least one restore method is available ──────────────
USE_PYTHON_FALLBACK=0
if [[ -z "$RUSTIC" ]]; then
    if [[ -n "$PYTHON" ]] && [[ -n "$STANDALONE" ]]; then
        echo "  WARNING: No rustic/restic binary found."
        echo "  Falling back to pure-Python restorer (slower)."
        USE_PYTHON_FALLBACK=1
    else
        echo "ERROR: No rustic/restic binary and no Python fallback."
        exit 1
    fi
fi

# ── Create work directory ────────────────────────────────────────
CLEANUP_WORK=0
if [[ -z "$WORK_DIR" ]]; then
    WORK_DIR="$(mktemp -d -t lcsas-restore-XXXXXX)"
    CLEANUP_WORK=1
else
    mkdir -p "$WORK_DIR"
fi
mkdir -p "$TARGET"

_cleanup() {
    umount_drive 2>/dev/null || true
    if [[ "$CLEANUP_WORK" -eq 1 ]] && [[ -n "$WORK_DIR" ]] && [[ -d "$WORK_DIR" ]]; then
        chmod -R u+w "$WORK_DIR" 2>/dev/null || true
        rm -rf "$WORK_DIR"
    fi
}
trap _cleanup EXIT

# ── Drive helpers ────────────────────────────────────────────────
MNT="$WORK_DIR/mnt"
mkdir -p "$MNT"

_sudo() {
    if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}
mount_drive() {
    _sudo mount -o ro "$DRIVE" "$MNT"
}
umount_drive() {
    _sudo umount "$MNT" 2>/dev/null || true
}
disc_label() {
    # Try blkid first (needs root on some systems).
    if command -v blkid &>/dev/null; then
        local lbl
        lbl="$(_sudo blkid -o value -s LABEL "$DRIVE" 2>/dev/null || true)"
        if [[ -n "$lbl" ]]; then
            echo "$lbl"
            return
        fi
    fi
    # Fallback: read volume_info.json from the mounted disc.
    if mountpoint -q "$MNT" 2>/dev/null && [[ -f "$MNT/volume_info.json" ]]; then
        "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("label", ""))
' "$MNT/volume_info.json" 2>/dev/null || true
    fi
}

load_disc() {
    local label="$1"
    # If the wanted disc is already mounted, skip.
    if mountpoint -q "$MNT" 2>/dev/null; then
        local cur
        cur="$(disc_label)"
        if [[ "$cur" == "$label" ]]; then
            return 0
        fi
    fi
    umount_drive
    if [[ -n "$DISC_CMD" ]]; then
        $DISC_CMD insert "$label"
    fi
    # Wait for the drive to become readable (up to 30s).
    local attempts=0
    while ! mount_drive 2>/dev/null; do
        attempts=$((attempts + 1))
        if [[ $attempts -ge 15 ]]; then
            echo "ERROR: drive not readable after 30s — expected $label"
            return 1
        fi
        sleep 2
    done
    # Verify label if possible.
    local got
    got="$(disc_label)"
    if [[ -n "$got" ]] && [[ "$got" != "$label" ]]; then
        echo "WARNING: expected disc $label, got $got"
    fi
    return 0
}

eject_disc() {
    umount_drive
    if [[ -n "$DISC_CMD" ]]; then
        $DISC_CMD eject 2>/dev/null || true
    elif command -v eject &>/dev/null; then
        eject "$DRIVE" &>/dev/null || true
    fi
}

# ═════════════════════════════════════════════════════════════════
echo "═══════════════════════════════════════════════════"
echo "  LCSAS Non-Interactive Restore"
echo "═══════════════════════════════════════════════════"
echo "  Key:       $KEY_FILE"
echo "  Drive:     $DRIVE"
echo "  Target:    $TARGET"
echo "  Repo:      $REPO"
echo "  Disc cmd:  ${DISC_CMD:-<manual>}"
echo "  Work dir:  $WORK_DIR"
echo ""

CACHE_DIR="$WORK_DIR/cache"
mkdir -p "$CACHE_DIR"

# ── Seed keys/config from meta disc if available ─────────────────
if [[ -d "$SCRIPT_DIR/metadata" ]]; then
    echo "  Seeding repo metadata from meta disc..."
fi

# ═════════════════════════════════════════════════════════════════
#  Phase 1 — Bootstrap
# ═════════════════════════════════════════════════════════════════
echo ""
echo "--- Phase 1: Bootstrap ---"
echo "  Need any data disc to read the catalog."

# Try to find the highest-numbered disc by reading volume_info.json
# from whatever disc is loaded. If no disc is loaded, request the
# first disc alphabetically (the caller/disc-cmd handles it).
FIRST_DISC="${FIRST_DISC:-}"

# If FIRST_DISC is set from env, load and mount it.
if [[ -n "$FIRST_DISC" ]] && ! mountpoint -q "$MNT" 2>/dev/null; then
    echo "  Loading $FIRST_DISC (from FIRST_DISC env)..."
    load_disc "$FIRST_DISC"
fi

# Try whatever disc is already in the drive.
if [[ -z "$FIRST_DISC" ]]; then
    if mount_drive 2>/dev/null; then
        if [[ -f "$MNT/catalog.db" ]]; then
            FIRST_DISC="$(disc_label)"
            echo "  Using disc already in drive: $FIRST_DISC"
        else
            umount_drive
        fi
    fi
fi

# Auto-discover the highest-numbered data disc from disc-cmd.
if [[ -z "$FIRST_DISC" ]] && [[ -n "$DISC_CMD" ]]; then
    echo "  No disc loaded — discovering available data discs..."
    # Get the last LCSAS_CD_* label (highest-numbered = freshest catalog).
    FIRST_DISC="$($DISC_CMD list 2>/dev/null | grep '^LCSAS_CD_' | sort | tail -1 || true)"
    if [[ -n "$FIRST_DISC" ]]; then
        echo "  Auto-selected: $FIRST_DISC"
        load_disc "$FIRST_DISC"
    fi
fi

if [[ -z "$FIRST_DISC" ]] && ! mountpoint -q "$MNT" 2>/dev/null; then
    echo "ERROR: no disc loaded. Either:"
    echo "  - Set FIRST_DISC=<label> and have that disc in the drive"
    echo "  - Use --disc-cmd to provide a disc loading command"
    exit 1
fi

CATALOG="$MNT/catalog.db"
if [[ ! -f "$CATALOG" ]]; then
    echo "ERROR: $CATALOG not found — not an LCSAS data disc."
    exit 1
fi

BOOTSTRAP_ARGS=(--catalog "$CATALOG" --mount "$MNT" --cache "$CACHE_DIR" --repo "$REPO")
if ! "$PYTHON" "$HELPER" bootstrap "${BOOTSTRAP_ARGS[@]}" > "$WORK_DIR/pick-list.json"; then
    rc=$?
    echo "ERROR: bootstrap failed (exit $rc)"
    exit 1
fi

# Extract volume list and state from pick list.
mapfile -t VOLUMES < <(
    "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for v in data["volumes"]:
    print(v["label"])
' "$CACHE_DIR/pick-list.json"
)
PACKS_TOTAL="$(
    "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("total_packs", 0))
' "$CACHE_DIR/pick-list.json"
)"
CATALOG_FRESH="$(
    "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("catalog_freshness", ""))
' "$CACHE_DIR/pick-list.json"
)"
echo "  Repository: $REPO"
echo "  Discs needed: ${#VOLUMES[@]}"
echo "  Total packs:  $PACKS_TOTAL"
for v in "${VOLUMES[@]}"; do echo "    - $v"; done

# ═════════════════════════════════════════════════════════════════
#  Phase 2 — Ingest
# ═════════════════════════════════════════════════════════════════
echo ""
echo "--- Phase 2: Ingest ---"
IDX=0
while [[ $IDX -lt ${#VOLUMES[@]} ]]; do
    label="${VOLUMES[$IDX]}"
    IDX=$((IDX + 1))
    echo "  [$IDX/${#VOLUMES[@]}] Loading $label..."
    if ! load_disc "$label"; then
        echo "  WARNING: could not load $label — skipping"
        continue
    fi

    # ── Organic catalog upgrade ──
    DISC_CATALOG="$MNT/catalog.db"
    if [[ -f "$DISC_CATALOG" ]]; then
        DISC_FRESH="$("$PYTHON" -c "
import sqlite3, sys
c = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro&immutable=1', uri=True)
print(c.execute('SELECT MAX(created_at) FROM volumes').fetchone()[0] or '')
c.close()
" "$DISC_CATALOG")"
        if [[ "$DISC_FRESH" > "$CATALOG_FRESH" ]]; then
            echo "  Fresher catalog on $label — upgrading pick list..."
            if "$PYTHON" "$HELPER" bootstrap \
                --catalog "$DISC_CATALOG" --mount "$MNT" \
                --cache "$CACHE_DIR" --repo "$REPO" \
                --reseed \
                > /dev/null 2>"$WORK_DIR/upgrade-err.txt"; then
                mapfile -t VOLUMES < <(
                    "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for v in data["volumes"]:
    print(v["label"])
' "$CACHE_DIR/pick-list.json"
                )
                CATALOG_FRESH="$DISC_FRESH"
                PACKS_TOTAL="$(
                    "$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get("total_packs", 0))
' "$CACHE_DIR/pick-list.json"
                )"
                echo "  Upgraded — now ${#VOLUMES[@]} discs, $PACKS_TOTAL packs"
            else
                echo "  WARNING: catalog upgrade failed, continuing"
            fi
        fi
    fi

    "$PYTHON" "$HELPER" ingest --mount "$MNT" --cache "$CACHE_DIR" --disc-label "$label" || {
        echo "  WARNING: ingest issues for $label"
    }
done
umount_drive
eject_disc

# ═════════════════════════════════════════════════════════════════
#  Phase 3 — Finalize
# ═════════════════════════════════════════════════════════════════
echo ""
echo "--- Phase 3: Finalize ---"
"$PYTHON" "$HELPER" finalize --cache "$CACHE_DIR" --verify-integrity
FINALIZE_RC=$?
if [[ $FINALIZE_RC -eq 3 ]]; then
    echo "FATAL: unrecoverable missing packs."
    exit 3
elif [[ $FINALIZE_RC -ne 0 ]]; then
    echo "ERROR: cache incomplete — re-run with missing discs."
    exit 1
fi

# ═════════════════════════════════════════════════════════════════
#  Phase 4 — Rustic restore
# ═════════════════════════════════════════════════════════════════
echo ""
echo "--- Phase 4: Restore ---"
REPO_TARGET="$TARGET/$REPO"
mkdir -p "$REPO_TARGET"

if [[ "$USE_PYTHON_FALLBACK" -eq 1 ]]; then
    if [[ -d "${TOOLS}/lib/python" ]]; then
        export PYTHONPATH="${TOOLS}/lib/python:${PYTHONPATH:-}"
    fi
    "$PYTHON" "$STANDALONE" --repo "$CACHE_DIR" --password-file "$KEY_FILE" --target "$REPO_TARGET"
else
    RUSTIC_BIN_NAME="$(basename "$RUSTIC")"
    if [[ "$RUSTIC_BIN_NAME" == rustic* ]]; then
        "$RUSTIC" restore "$SNAPSHOT" "$REPO_TARGET" \
            -r "$CACHE_DIR" --password-file "$KEY_FILE" --no-cache
    else
        "$RUSTIC" restore "$SNAPSHOT" \
            -r "$CACHE_DIR" --password-file "$KEY_FILE" --no-cache \
            --target "$REPO_TARGET"
    fi
fi

echo ""
echo "═══════════════════════════════════════════════════"
echo "  RESTORE COMPLETE"
echo "  Output: $REPO_TARGET"
echo "═══════════════════════════════════════════════════"
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
| `restore.sh` | **Start here. Prompts for password and disc swaps.** |
| `restore-auto.sh` | Non-interactive restore (automation only; use restore.sh instead) |
| `restore_legacy.sh` | Older Bash driver kept for back-compat — superseded by `restore.sh`. |
| `README_RESTORE.md` | This file |
| `volume_info.json` | Machine-readable volume metadata (includes tool versions) |

## Terminology

A **repository** (sometimes called a **tenant**) is one encrypted backup
dataset. Archives may hold several repositories side by side. Pass the
repository name to `--repo`.

## Single-Drive Mode (DEFAULT)

This is the disaster scenario: you own one optical drive and a stack of
archive discs. You do **not** need to rip every disc up front — the
script walks you through the restore one disc at a time.

### 1. Mount the meta-volume and run the restore

```sh
sudo mount /dev/sr0 /mnt
sh /mnt/restore.sh ~/restored/ latest
```

`restore.sh` detects that it is running off a read-only optical disc
and copies itself (plus the recovery binaries) into RAM automatically,
so you can eject the meta-volume when the binary first prompts for a
data disc — no manual `cp -r` step is needed.

> **Advanced override:** set `LCSAS_NO_RELOCATE=1` in the environment
> if you want to keep running directly off the disc (e.g. for tests,
> or when running from a writable copy you placed yourself).

The two positional arguments are:

| Position | Meaning | Default |
|---|---|---|
| 1 | `TARGET_DIR` — where restored files are written | `/tmp/restored` |
| 2 | `SNAPSHOT_ID` or the word `latest` | `latest` |

The script auto-discovers the repository metadata from the meta
volume itself (`metadata/<tenant>/`), so you do **not** need to
insert a data disc before starting — the script will prompt you
for each one as the recovery binary needs it.  The script will:

1. List the repositories on this archive.  If more than one
   tenant is present, it prints `Repository:` and waits for you
   to **type the tenant name** (`alpha`, `bravo`, …) and press
   Enter.  Single-tenant archives skip this prompt.  You can
   also set `LCSAS_REPO=<name>` in the environment to preselect.
2. Print `Password:` and wait for the encryption password on
   stdin.  **Type the contents of your key file** and press
   Enter.  The password is read in the clear (POSIX-sh has no
   silent-read); do not pipe it from `echo` — type it.
3. `exec` the highest-priority recovery tier available
   (`bin/<arch>/lcsas-restore` → `bin/<arch>/rustic-static` →
   bundled CPython + `standalone_restorer.py`).  Only one tier
   runs per invocation.
4. When a pack is missing from the disc currently in the drive,
   the recovery binary stops and prints `INSERT DISC: LCSAS_<label>`
   on stderr.  **Swap the disc in your optical drive and press Enter.**
   Repeat until the binary prints `RESTORE COMPLETE` and exits 0.

> **Single-terminal operators.** You do not need tmux or a second SSH
> session to handle disc swaps — a normal single-terminal flow works.
> When the binary prompts to swap a disc:
>
>   1. Open a second terminal OR press Ctrl+Z to suspend the restore.
>   2. Run `disc-loader insert <label>` (or eject + insert the
>      physical disc by hand, e.g. `sudo eject /dev/sr0`).
>   3. In the second terminal: `disc-loader status` to confirm.
>      In the first terminal: type `fg` to resume.
>   4. Press Enter at the restore prompt.
>
> If `disc-loader` is not installed on your host, the same flow works
> with plain `sudo eject /dev/sr0` and physically swapping the disc.

If a disc is unreadable or the system crashes mid-restore you can
re-run the same command — the pack cache under the work directory
persists across runs.

> **Tip:** if you mount a data disc at `/mnt` (or `/media/...`)
> before starting, the recovery binary uses its on-disc catalog
> for friendlier disc-swap prompts (it prints the disc *label*
> rather than the pack hash) and skips re-prompting for packs
> already on the mounted disc.

> **Automation / CI only.** Human operators recovering from disaster
> must use the interactive `restore.sh` above. The non-interactive
> driver `restore-auto.sh` exists only for scripted pipelines —
> see Appendix A.

## Directory Mode (opt-in, legacy)

If you already have every data-volume ISO on disk (e.g. pre-rsynced to
a NAS) you can side-load them via the legacy bash driver
(`restore_legacy.sh`):

```bash
./restore_legacy.sh --key /path/to/keyfile.txt \\
                    --isos /path/to/iso/directory/ \\
                    --target ~/restored/
```

This extracts every ISO up front and copies every pack into the cache
before restoring. Faster when disks are cheap; wrong for the
single-drive disaster scenario.

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
  disk full), simply re-run the restore command.  The pack cache that
  `restore.sh` builds in a temporary work directory persists across runs,
  so a re-run picks up where the previous attempt stopped.  Do **not**
  rely on a partially-restored target directory.

## What Is NOT on This Volume

**Your encryption key file** — you must provide this yourself.
Without the key file, the encrypted backup data cannot be decrypted.

> **Important:** Store your key file securely and *separately* from
> your backup discs. Consider printing it on paper and storing in a
> fireproof safe, or splitting it across multiple secure locations.

## About LCSAS

Linux Cold Storage Archival Suite orchestrates Rustic (restic-compatible) backup
repositories onto optical media (Blu-ray, M-DISC) for long-term archival
storage. Every data disc is self-describing ("holographic"),
carrying full repository metadata so that any disc can bootstrap a restore
independently.

See `docs/architecture.md` for the complete system architecture, and
`docs/RESTIC_FORMAT_SPEC.md` for the data format specification.

---

## Appendix A — restore-auto.sh (automation only)

> Skip this section unless you are scripting LCSAS into a CI
> pipeline, a backup-validation harness, or another fully
> automated environment. Human operators recovering from disaster
> should use `restore.sh` (Single-Drive Mode above).

`restore-auto.sh` runs the same four phases as `restore.sh`
(bootstrap, ingest, finalize, rustic restore) but never calls
`read` and never waits for keyboard input.  It is the right tool
for an environment where another process can mechanically swap
discs and feed in the key file.

```bash
./restore-auto.sh --key /path/to/keyfile.txt \\
                  --target ~/restored/ \\
                  --repo REPO_NAME \\
                  --disc-cmd "disc-loader"
```

| Option | Description |
|---|---|
| `--key FILE` | **(required)** Encryption key file |
| `--target DIR` | **(required)** Restore destination |
| `--repo NAME` | **(required)** Repository to restore |
| `--disc-cmd CMD` | Robotic loader command (`CMD insert LABEL` / `CMD eject`). |
| `--drive DEV` | Optical drive (default: `/dev/sr0`) |
| `--snapshot ID` | Snapshot to restore (default: latest) |
| `--work-dir DIR` | Temp directory (default: auto) |
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
        catalog_db_path: Path | None = None,
        recovery_dir: Path | None = None,
        bundle_recovery_toolchain: bool = True,
        allow_no_zstd: bool = False,
        allow_no_dvdisaster_source: bool = False,
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
        self._catalog_db_path = catalog_db_path
        self._bundle_recovery_toolchain = bundle_recovery_toolchain
        self._allow_no_zstd = allow_no_zstd
        self._allow_no_dvdisaster_source = allow_no_dvdisaster_source
        # Whether the native ``zstandard`` package was bundled for this
        # build host's arch/CPython.  Recorded in volume_info.json so
        # ``lcsas meta verify`` can report tier-3 native-zstd coverage.
        # (Tier-3 zstd ALSO works via the bundled pure-Python decoder on
        # every target, so a False here is a speed note, not a gap.)
        self._native_zstd_bundled = False

        if project_root is None:
            # meta/ → lcsas/ → src/ → (project root)
            self._project_root = Path(__file__).resolve().parents[3]
        else:
            self._project_root = project_root.resolve()

        if recovery_dir is None:
            self._recovery_dir = self._project_root / "recovery"
        else:
            self._recovery_dir = recovery_dir.resolve()

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
        self._bundle_dvdisaster_source()
        self._bundle_standalone_restorer()
        self._bundle_restore_helper()
        self._bundle_keyshare_combiner()
        self._bundle_metadata()
        if self._bundle_recovery_toolchain:
            self._bundle_recovery_toolchain_artifacts()
        self._write_restore_script()
        self._write_restore_auto_script()
        self._write_readme()
        self._write_readme_txt()
        self._write_volume_info()
        self._write_start_here()

        # Build complete — remove the marker
        incomplete_marker.unlink(missing_ok=True)

        return self._output

    def missing_required_contents(self) -> list[str]:
        """Return required-contents paths absent from the built output.

        RST-05: walks the 2026-06 required-contents contract
        (``required_meta_paths``) against ``self._output``.  A path that
        names a directory (e.g. ``tools``) is satisfied by the directory
        existing; everything else must be a file.  Empty list ⇒ complete.
        """
        missing: list[str] = []
        for rel in required_meta_paths():
            target = self._output / rel
            if rel == "tools":
                if not target.is_dir():
                    missing.append(rel)
            elif not target.is_file():
                missing.append(rel)
        return missing

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

        # Bundle the native ``zstandard`` package for fast tier-3 restore
        # of zstd-compressed repos (rustic v2 default).  This copies the
        # BUILD HOST's arch/CPython-specific C extension, so it only helps
        # recovery hosts that match.  Cross-target / future-CPython hosts
        # fall back to the bundled pure-Python decoder
        # (``lcsas.restore._zstd_pure``), which is slower but always works.
        #
        # RST-04: if the build host has no ``zstandard`` at all the native
        # copy is silently absent; fail loud unless explicitly allowed, so
        # an operator who WANTED the fast path isn't surprised by a
        # pure-Python-only meta disc.
        self._native_zstd_bundled = (
            bundler.bundle_python_package("zstandard") is not None
        )
        if not self._native_zstd_bundled and not self._allow_no_zstd:
            raise MetaBuildError(
                "zstandard is not installed on the build host — the fast "
                "tier-3 restore path for zstd-compressed (default) repos "
                "would not be bundled. The pure-Python decoder still works "
                "(slower), but if you want the native fast path, install it "
                "(pip install zstandard). Pass --allow-no-zstd to build "
                "anyway."
            )

        # Bundle the stdlib-only SLIP-0039 keyshare package (+ wordlist.txt)
        # so the standalone keyshare_combine.py pre-step can reconstruct a
        # split repo password.  Lands as top-level ``keyshare`` under the
        # bundled stdlib (importlib resolves it to .../lcsas/keyshare).
        bundler.bundle_python_package("lcsas.keyshare")

        # Bundle static rustic binary (glibc-independent fallback).
        # If an explicit path was provided, use it.  Otherwise,
        # auto-detect: if the bundled rustic is already statically
        # linked, copy it as rustic-static too.
        static_src: Path | None = None
        if self._static_rustic_path is not None:
            static_src = Path(self._static_rustic_path).resolve()
            if not static_src.is_file():
                raise FileNotFoundError(
                    f"Static rustic binary not found: {static_src}"
                )
        elif (bundler.bin_dir / "rustic").is_file():
            # Auto-detect: check if the bundled rustic has no shared deps
            from lcsas.meta.bundler import get_shared_libs

            bundled_rustic = bundler.bin_dir / "rustic"
            if not get_shared_libs(bundled_rustic):
                static_src = bundled_rustic

        if static_src is not None:
            dst = bundler.bin_dir / "rustic-static"
            if not dst.exists():
                shutil.copy2(str(static_src), str(dst))
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

    def _bundle_dvdisaster_source(self) -> None:
        """Bundle the pinned dvdisaster RS03 source tarball (FMT-02).

        ``docs/DVDISASTER_RS03_FORMAT.md`` is only re-implementable
        against the *exact* source it transcribes; that source must
        therefore travel on the rescue disc, not at a dormant GitHub URL.
        The tarball is pinned in ``recovery/UPSTREAM.sha256`` (category
        ``dvdisaster/src/``) and fetched into the recovery cache by
        ``recovery/scripts/fetch_upstream.sh``; here we copy it to
        ``tools/src/`` on the meta-volume.

        Fail loud if the tarball is pinned but absent from the cache —
        a meta disc whose RS03 spec points at source that isn't on the
        disc is exactly the broken hedge FMT-02 fixes.  Pass
        ``allow_no_dvdisaster_source`` (or ``--allow-no-dvdisaster-source``)
        to build without it.
        """
        name = pinned_dvdisaster_source_name(self._recovery_dir)
        if name is None:
            if self._allow_no_dvdisaster_source:
                return
            raise MetaBuildError(
                "no dvdisaster source tarball is pinned in "
                f"{self._recovery_dir / 'UPSTREAM.sha256'} (category "
                "dvdisaster/src/). docs/DVDISASTER_RS03_FORMAT.md would "
                "point at source that does not travel on the disc. Pin it "
                "or pass --allow-no-dvdisaster-source."
            )

        cache_root_env = os.environ.get("LCSAS_RECOVERY_CACHE")
        if cache_root_env:
            cache_root = Path(cache_root_env)
        else:
            cache_root = Path.home() / ".cache" / "lcsas" / "recovery-binaries"
        src = cache_root / "dvdisaster" / "src" / name

        if not src.is_file():
            if self._allow_no_dvdisaster_source:
                return
            raise MetaBuildError(
                f"pinned dvdisaster source {name} not found in the recovery "
                f"cache ({src}). Run `sh recovery/scripts/fetch_upstream.sh` "
                "to download it, or pass --allow-no-dvdisaster-source to "
                "build a meta disc whose RS03 spec lacks its reference "
                "source."
            )

        dst_dir = self._output / _DVDISASTER_SOURCE_SUBDIR
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst_dir / name))

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

    def _bundle_restore_helper(self) -> None:
        """Copy restore_single_drive.py into the meta-volume tools/ dir.

        The helper is a stdlib-only Python driver for the single-drive
        disc-swap restore flow. ``restore.sh`` shells out to it for the
        bootstrap, ingest, and finalize phases.
        """
        src = Path(__file__).parent / "restore_single_drive.py"
        if not src.is_file():
            raise FileNotFoundError(
                f"restore_single_drive.py missing from source tree: {src}"
            )
        tools_dir = self._output / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        dst = tools_dir / "restore_single_drive.py"
        shutil.copy2(str(src), str(dst))
        os.chmod(str(dst), 0o755)

    def _bundle_keyshare_combiner(self) -> None:
        """Place keyshare_combine.py at the meta-volume root.

        This standalone, stdlib-only pre-step reconstructs a SLIP-0039
        split repo password (any K of N shares) and prints it so an heir
        can feed it to the normal restore flow.  It imports ONLY the
        bundled ``lcsas.keyshare`` package, so reconstruction survives
        even if the rest of LCSAS is broken.

        Manifest pinning: ``keyshare_combine.py`` and the bundled
        ``keyshare/wordlist.txt`` are NOT rows in
        ``recovery/MANIFEST.sha256``.  That "files-we-author" manifest
        is rooted at, and ``find``-verified against, the ``recovery/``
        tree (the tier-1 C binary, vendored sqlite/zstd, recovery
        scripts).  These two artifacts live in the main ``src/`` tree
        and are copied onto the meta-volume at build time -- exactly like
        ``standalone_restorer.py`` and ``restore_single_drive.py``, which
        are likewise absent from that manifest.  They are pinned by git
        (every ``src/`` file is version-controlled) and the wordlist is
        additionally guarded by the 45 official SLIP-0039 conformance
        vectors in the test suite.  Adding ``./src/...`` rows to a
        ``recovery/``-rooted manifest would be flagged as unexpected by
        its own verifier, so the correct treatment is build-time shipping,
        documented here.
        """
        src = Path(__file__).parent / "keyshare_combine.py"
        if not src.is_file():
            raise FileNotFoundError(
                f"keyshare_combine.py missing from source tree: {src}"
            )
        dst = self._output / "keyshare_combine.py"
        shutil.copy2(str(src), str(dst))
        os.chmod(str(dst), 0o755)

    def _bundle_recovery_toolchain_artifacts(self) -> None:
        """Bundle the C89 + POSIX-sh recovery toolchain onto the meta-volume.

        Layout produced under ``output_dir/recovery/``::

            recovery/
            ├── bin/<arch>/lcsas-restore       (if built)
            ├── bin/<arch>/lcsas-iso9660       (if built)
            ├── bin/<arch>/lcsas-init          (if built)
            ├── src/                            C source
            ├── vendored/                       sqlite + zstd amalgamation
            ├── scripts/                        POSIX-sh drivers
            ├── docs/                           plain-text docs
            ├── boot/                           kernel/loader configs
            ├── Makefile
            └── VERSION

        Missing per-arch binaries are silently skipped; the recovery
        cascade falls through to the vendored ``rustic-static`` (tier 2)
        and ultimately the pure-Python fallback (tier 3) when the
        prebuilt binary is absent.  See ``recovery/scripts/restore.sh``.
        """
        src = self._recovery_dir
        if not src.is_dir():
            return  # not a fatal error: recovery toolchain is optional

        dst = self._output / "recovery"
        if dst.exists():
            shutil.rmtree(str(dst))
        shutil.copytree(
            str(src),
            str(dst),
            ignore=shutil.ignore_patterns(
                "build", "build-*", "__pycache__", "*.pyc",
                "*.o", "*.a",
            ),
        )

        # Mirror the new POSIX restore.sh at the meta-volume root so
        # existing automation that looks for /restore.sh finds the new
        # driver too.  (The legacy bash heredoc is still written by
        # _write_restore_script for backward compat.)
        new_restore = dst / "scripts" / "restore.sh"
        if new_restore.is_file():
            top_link = self._output / "restore_c89.sh"
            shutil.copy2(str(new_restore), str(top_link))
            os.chmod(str(top_link), 0o755)

        # Surface restore.bat at the meta-volume root so Windows users
        # who plug in the disc see "restore.bat" in File Explorer and
        # can double-click it without descending into recovery/scripts/.
        new_bat = dst / "scripts" / "restore.bat"
        if new_bat.is_file():
            top_bat = self._output / "restore.bat"
            shutil.copy2(str(new_bat), str(top_bat))

        # Phase 21.1: bundle per-target prebuilt rustic + python from the
        # upstream cache populated by `make fetch-recovery`.  Tolerant of
        # a missing or partial cache so single-arch developer builds
        # keep working without the full ~600 MB download.
        self._bundle_upstream_binaries(dst)

        # Phase 21.10.b: bundle per-target prebuilt lcsas-restore (the
        # tier-1 C89 binary) cross-built by `make build-recovery` or
        # `lcsas recovery build --arch X`.  Tolerant of missing builds
        # for the same reason as the upstream bundler.
        self._bundle_tier1_binaries(dst)

        # Phase 21.4: regenerate the meta-volume's recovery/MANIFEST.sha256
        # so every bundled binary (tier 1 from _bundle_tier1_binaries,
        # tier 2 + 3 from _bundle_upstream_binaries) is part of the
        # single integrity manifest an operator can `sha256sum -c`
        # against.  Must run after BOTH bundlers, so it stays here at
        # the orchestrator level rather than inside either bundler.
        self._regenerate_recovery_manifest(dst)

    def _bundle_upstream_binaries(self, recovery_dst: Path) -> None:
        """Copy cached upstream rustic + python tarball contents per target.

        Reads from ``$LCSAS_RECOVERY_CACHE`` (default
        ``~/.cache/lcsas/recovery-binaries``).  For every approved target
        present in the cache, writes:

            recovery_dst/bin/<target>/rustic-static          (from rustic/<target>/rustic)
            recovery_dst/bin/<target>/python/                (from python/<target>/python/)

        Targets with no rustic binary AND no python tree are skipped
        silently — the recovery cascade in ``restore.sh`` handles the
        empty case (falls through tier 2 → tier 3 → fatal).
        """
        cache_root_env = os.environ.get("LCSAS_RECOVERY_CACHE")
        if cache_root_env:
            cache_root = Path(cache_root_env)
        else:
            cache_root = Path.home() / ".cache" / "lcsas" / "recovery-binaries"

        if not cache_root.is_dir():
            return  # No cache → meta build still works for single-arch path.

        # Approved targets per docs/CROSS_PLATFORM_META_RFC.md §3.  We
        # iterate this list rather than `os.listdir(cache_root)` so a
        # corrupted/extra directory in the cache can't silently extend
        # what we ship.
        for target in (
            "x86_64-unknown-linux-musl",
            "aarch64-unknown-linux-musl",
            "armv7-unknown-linux-gnueabihf",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
            "x86_64-pc-windows-gnu",
        ):
            bin_dst = recovery_dst / "bin" / target
            staged_any = False

            # ── rustic ─────────────────────────────────────────────
            # The fetch script extracts the tarball next to itself; the
            # binary is named "rustic" (or "rustic.exe" on Windows).
            rustic_cache = cache_root / "rustic" / target
            for cand in (rustic_cache / "rustic", rustic_cache / "rustic.exe"):
                if cand.is_file():
                    bin_dst.mkdir(parents=True, exist_ok=True)
                    out_name = "rustic-static.exe" if cand.suffix == ".exe" else "rustic-static"
                    shutil.copy2(str(cand), str(bin_dst / out_name))
                    os.chmod(str(bin_dst / out_name), 0o755)
                    staged_any = True
                    break

            # ── python ─────────────────────────────────────────────
            # python-build-standalone tarballs extract to a "python/"
            # tree (Linux/macOS) or a flat install tree (Windows;
            # python.exe sits at the top).  Copy the whole tree.
            python_cache = cache_root / "python" / target / "python"
            if python_cache.is_dir():
                py_dst = bin_dst / "python"
                bin_dst.mkdir(parents=True, exist_ok=True)
                if py_dst.exists():
                    shutil.rmtree(str(py_dst))
                shutil.copytree(
                    str(python_cache), str(py_dst), symlinks=True,
                )
                staged_any = True
            else:
                # Windows PBS tarballs extract to "python/" directly or
                # to the target root; try both.
                python_alt = cache_root / "python" / target
                python_exe = python_alt / "python.exe"
                if python_exe.is_file():
                    py_dst = bin_dst / "python"
                    bin_dst.mkdir(parents=True, exist_ok=True)
                    if py_dst.exists():
                        shutil.rmtree(str(py_dst))
                    # Copy everything in python_alt EXCEPT the tarball.
                    py_dst.mkdir(parents=True)
                    for entry in python_alt.iterdir():
                        if entry.name.endswith(".tar.gz") or entry.name == ".extracted":
                            continue
                        if entry.is_dir():
                            shutil.copytree(
                                str(entry), str(py_dst / entry.name), symlinks=True,
                            )
                        else:
                            shutil.copy2(str(entry), str(py_dst / entry.name))
                    staged_any = True

            if staged_any:
                # No-op: directory already created by the per-binary
                # branches; documenting intent.
                pass

    def _bundle_tier1_binaries(self, recovery_dst: Path) -> None:
        """Copy cross-built lcsas-restore binaries into per-target dirs.

        ``RecoveryBuilder.cross_build`` (`src/lcsas/recovery/build.py:99`)
        writes the C89 lcsas-restore binary to
        ``recovery/bin/<short-arch>/lcsas-restore[.exe]`` using its
        short-arch convention (``x86_64``, ``aarch64``,
        ``x86_64-windows``, …).  ``recovery/scripts/restore.sh`` (the
        Phase 21.1.c dispatcher) looks for it at
        ``recovery/bin/<rust-triple>/lcsas-restore[.exe]`` using the
        rust-style target triple.

        Phase 21.10.b closes the naming gap at bundle time: for every
        approved rust-triple target whose short-arch mapping has a
        pre-built lcsas-restore in the source recovery tree, copy it
        into the meta volume under the rust-triple path.  Targets
        without a mapping (`armv7`, the two macOS targets) are
        skipped silently; targets whose short-arch directory is
        empty are also skipped (operator hasn't built that arch yet).

        KEY-05 extends the same relocation to ``lcsas-keyshare[.exe]``
        (the static split-key combiner): the heir-facing Windows docs
        name a single on-disc bin dir per target, so the combiner must
        live next to lcsas-restore under the rust triple.  The
        short-arch copies remain on disc too (the whole source
        ``recovery/`` tree is copied verbatim by
        ``_bundle_recovery_toolchain_artifacts``).

        Operators trigger the underlying cross-builds with
        ``lcsas recovery build --arch <short-arch>`` for each desired
        target, or `make build-recovery` to do all reachable targets
        in one shot.

        See `docs/CROSS_PLATFORM_META_RFC.md` §6 Q6 for the gap
        analysis and the Phase 21.11 / 21.12 follow-ups that close
        the remaining targets.
        """
        # rust-triple → (short_arch_dir_name, lcsas-restore_filename)
        # Phase 21.12 closed the last open mapping; every approved
        # target now has a tier-1 path.  Keyed off APPROVED_TARGETS
        # (RST-05) so the map can never drift from the contract.
        tier1_map: dict[str, tuple[str, str] | None] = {
            "x86_64-unknown-linux-musl":     ("x86_64", "lcsas-restore"),
            "aarch64-unknown-linux-musl":    ("aarch64", "lcsas-restore"),
            "armv7-unknown-linux-gnueabihf": ("armv7", "lcsas-restore"),
            "aarch64-apple-darwin":          ("aarch64-macos", "lcsas-restore"),
            "x86_64-apple-darwin":           ("x86_64-macos", "lcsas-restore"),
            "x86_64-pc-windows-gnu":         ("x86_64-windows", "lcsas-restore.exe"),
        }
        assert set(tier1_map) == set(APPROVED_TARGETS), (
            "tier1_map keys drifted from APPROVED_TARGETS "
            "(src/lcsas/meta/required_contents.py)"
        )

        src_recovery = self._recovery_dir
        for rust_triple, mapping in tier1_map.items():
            if mapping is None:
                continue
            short_arch, exe_name = mapping
            # KEY-05: the static key-share combiner is part of the
            # tier-1 binary family — relocate it next to lcsas-restore
            # so the heir docs can name one on-disc Windows path
            # (bin\<rust-triple>\lcsas-keyshare.exe).  Skip-if-absent,
            # same tolerance as lcsas-restore itself.
            suffix = ".exe" if exe_name.endswith(".exe") else ""
            for name in (exe_name, f"lcsas-keyshare{suffix}"):
                src_bin = src_recovery / "bin" / short_arch / name
                if not src_bin.is_file():
                    continue
                dst_bin_dir = recovery_dst / "bin" / rust_triple
                dst_bin_dir.mkdir(parents=True, exist_ok=True)
                dst_bin = dst_bin_dir / name
                shutil.copy2(str(src_bin), str(dst_bin))
                # Preserve the executable bit on Unix targets (Windows
                # binaries don't care about the +x mode, but extra +x
                # never hurts).
                os.chmod(str(dst_bin), 0o755)

    def _regenerate_recovery_manifest(self, recovery_dst: Path) -> None:
        """Merge bundled upstream binaries into recovery/MANIFEST.sha256.

        The source-tree MANIFEST already covers files we author (C
        source, scripts, docs, vendored sqlite/zstd, …).  This step
        adds the per-target rustic + python tree rooted at
        ``bin/<target>/`` so the meta-volume ships with a single
        integrity manifest covering everything under ``recovery/``.

        Idempotent: re-running rewrites the file in place; the source
        entries are preserved, the bin/<target>/ entries are recomputed.

        Skipped (silent) when ``MANIFEST.sha256`` is absent from the
        recovery tree (some older builds didn't ship it) — there's
        nothing to merge into.
        """
        import hashlib

        manifest_path = recovery_dst / "MANIFEST.sha256"
        if not manifest_path.is_file():
            return

        # Read existing entries, keep anything NOT under ./bin/ — those
        # rows are about to be regenerated and stale ones would shadow.
        existing_lines: list[str] = []
        for line in manifest_path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                existing_lines.append(line)
                continue
            # Parse "<sha>  <path>".  Drop bin/* entries (they get
            # regenerated below); keep everything else verbatim.
            parts = stripped.split("  ", 1)
            if len(parts) == 2 and parts[1].startswith("./bin/"):
                continue
            existing_lines.append(line)

        # Walk every file under recovery_dst/bin/ and produce a new
        # entry.  Sort by relative path for deterministic output —
        # makes the manifest reviewable and SHA-stable across builds
        # that bundle the same target set.
        bin_root = recovery_dst / "bin"
        bin_entries: list[str] = []
        if bin_root.is_dir():
            for path in sorted(bin_root.rglob("*")):
                if not path.is_file():
                    continue
                # Path inside recovery/ — manifest format is "./relative/path".
                rel = path.relative_to(recovery_dst).as_posix()
                # Compute SHA-256 streaming so this scales to the ~30 MB
                # python tarballs without slurping into memory.
                h = hashlib.sha256()
                with path.open("rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        h.update(chunk)
                bin_entries.append(f"{h.hexdigest()}  ./{rel}")

        merged = "\n".join(existing_lines + bin_entries) + "\n"
        manifest_path.write_text(merged)

    def _bundle_metadata(self) -> None:
        """Copy per-repo Rustic metadata (keys, config, index, snapshots) onto the meta volume.

        The meta disc does NOT carry a catalog.db — it would always be
        stale (pre-dating data discs burned after the meta disc).
        Instead, the restore script bootstraps from the catalog on the
        first data disc the operator inserts, and upgrades organically
        when it encounters a fresher catalog on a later disc.

        We do bundle Rustic metadata (keys, config, index, snapshots)
        because keys are needed to decrypt packs and don't go stale.
        """
        if self._catalog_db_path is None:
            return
        src = Path(self._catalog_db_path)
        if not src.is_file():
            return

        import sqlite3
        conn = sqlite3.connect(
            f"file:{src}?mode=ro&immutable=1", uri=True
        )
        try:
            rows = conn.execute(
                "SELECT repo_id, mirror_path FROM repositories"
            ).fetchall()
        finally:
            conn.close()

        meta_root = self._output / "metadata"
        meta_root.mkdir(parents=True, exist_ok=True)
        for repo_id, mirror_path in rows:
            mp = Path(mirror_path)
            if not mp.is_dir():
                continue
            dst_repo = meta_root / repo_id
            dst_repo.mkdir(parents=True, exist_ok=True)
            for sub in ("config", "keys", "index", "snapshots"):
                s = mp / sub
                d = dst_repo / sub
                if s.is_file() and not d.exists():
                    shutil.copy2(str(s), str(d))
                elif s.is_dir() and not d.exists():
                    shutil.copytree(str(s), str(d))

    def _write_restore_script(self) -> None:
        """Install the meta-volume's top-level ``restore.sh``.

        Behavior:

        * If the C89 recovery toolchain bundle is available (the new
          POSIX-sh driver in ``recovery/scripts/restore.sh``), copy
          *that* in as ``/restore.sh``.  This is Python-free for tiers
          1-2 and only touches Python at tier 3 (LCSAS_ALLOW_PYTHON_TIER).
        * Otherwise, fall back to the legacy bash heredoc
          (``RESTORE_SCRIPT``), which carries a hard Python dependency
          from earlier days and is kept only for compatibility with
          discs that predate the recovery/ tree.

        The legacy script is *also* written, as ``restore_legacy.sh``,
        so it remains accessible as a manual third option if needed.
        """
        script_path = self._output / "restore.sh"
        new_driver = self._output / "recovery" / "scripts" / "restore.sh"
        if new_driver.is_file():
            shutil.copy2(str(new_driver), str(script_path))
            os.chmod(str(script_path), 0o755)
            # Stamp build SHA + date into the restore script placeholders.
            import subprocess as _sp
            try:
                _sha = _sp.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=str(Path(__file__).resolve().parent),
                    text=True,
                    stderr=_sp.DEVNULL,
                ).strip()
            except Exception:
                _sha = "unknown"
            _build_date = date.today().isoformat()
            _content = script_path.read_text(encoding="utf-8")
            _content = _content.replace("@@BUILD_SHA@@", _sha).replace(
                "@@BUILD_DATE@@", _build_date
            )
            _write_and_sync(script_path, _content)
            # Stash the legacy bash driver alongside for compatibility /
            # for users who specifically want it.  Off the bare path.
            legacy = self._output / "restore_legacy.sh"
            _write_and_sync(legacy, RESTORE_SCRIPT)
            os.chmod(str(legacy), 0o755)
            # Replace the inner copy with a redirect so operators (and
            # agents) who navigate into recovery/scripts/ land on the
            # canonical root-level entry, not a silent duplicate.
            # "$(dirname "$0")/../../restore.sh" resolves to /mnt/restore.sh
            # regardless of where the disc is mounted.
            _write_and_sync(
                new_driver,
                "#!/bin/sh\n"
                "exec \"$(dirname \"$0\")/../../restore.sh\" \"$@\"\n",
            )
            os.chmod(str(new_driver), 0o755)
        else:
            # No recovery/ tree was bundled (e.g. older builds).  Fall
            # back to the historical bash driver.
            _write_and_sync(script_path, RESTORE_SCRIPT)
            os.chmod(str(script_path), 0o755)

    def _write_restore_auto_script(self) -> None:
        """Write the non-interactive restore-auto.sh script."""
        script_path = self._output / "restore-auto.sh"
        _write_and_sync(script_path, RESTORE_AUTO_SCRIPT)
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
                "restore_auto_script": "restore-auto.sh",
                "documentation": True,
            },
            # Tier-3 zstd capability (RST-04).  ``native_zstd`` records
            # whether the build host's ``zstandard`` C extension was bundled
            # (fast path, host-arch/CPython-specific).  ``pure_python_zstd``
            # is always True: the stdlib-only decoder
            # (lcsas.restore._zstd_pure) ships in the LCSAS source and works
            # on every target / CPython minor.
            "zstd_support": {
                "native_zstd": self._native_zstd_bundled,
                "native_zstd_arch": (
                    f"linux-{os.uname().machine}-cp"
                    f"{sys.version_info.major}{sys.version_info.minor}"
                    if self._native_zstd_bundled else None
                ),
                "pure_python_zstd": True,
            },
            "requires": {
                "key_file": "User must provide the encryption key file",
                "data_isos": "LCSAS data-volume ISO files",
            },
        }
        info_path = self._output / "volume_info.json"
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

    def _write_start_here(self) -> None:
        """Write START_HERE.txt to the meta-volume.

        One renderer serves both build variants (UX-05): the
        production (config) build gets the same per-OS dispatch and
        path-qualified commands as the no-config build, plus the
        survivability fields.  The data-disc generator
        (``HolographicInjector.write_start_here``) is deliberately
        NOT used here — its text points at data-disc-only files
        (RESTORE_INSTRUCTIONS.txt) and carries no runnable command.
        """
        from lcsas.staging.metadata import HolographicInjector

        injector = HolographicInjector(self._output)
        injector.write_disc_care(self._config)
        if self._config is not None:
            injector.write_key_info(self._config)
            injector.write_config_summary(self._config)
        text = self._render_meta_start_here(self._config)
        _write_and_sync(self._output / "START_HERE.txt", text)

    def _render_meta_start_here(self, config: LCSASConfig | None) -> str:
        """Render the meta-volume's START_HERE.txt text (UX-05).

        The document always carries the per-OS dispatch (Windows /
        macOS / Linux) with mount-path-qualified commands and the
        honest no-OS routing (UX-03).  When *config* is given, the
        survivability fields (owner, description, key-storage hints,
        technical contact) and the split-key pre-step are merged in.
        Every file the text references must exist on the meta-volume
        (guarded by tests/unit/test_meta_builder.py).
        """
        from lcsas.staging.metadata import _share_recovery_lines

        label = f"{config.label_prefix}_META" if config is not None else "LCSAS_META"

        # No LCSAS disc is bootable (the live-boot path was dropped per
        # BOOT-01/BOOT-07), so the no-OS route must never claim the disc
        # boots (UX-03): heirs get the borrow-a-computer + live-USB
        # routing that works today.
        no_os_block = """\
  >>> No working computer at all <<<
       These discs are NOT bootable, but they do NOT require
       a special computer.  Use any other computer — a
       friend's, a library's, or a cheap second-hand laptop.
       Windows, macOS and Linux all work (pick your section
       above).
       If a computer has no operating system at all, ask a
       helper to make a "live Linux USB stick" (free; search
       for "create Ubuntu live USB"), start the computer
       from it, then follow the macOS/Linux steps above.
       (See recovery/docs/BOOT.txt for the steps.)
"""

        about_block = ""
        key_hints_block = ""
        key_info_line = ""
        split_block = ""
        contact_block = ""
        if config is not None:
            owner = config.archive_owner or "the person who created this archive"
            description = config.archive_description or (
                "digital files backed up using LCSAS"
            )
            repo_line = ""
            if config.repositories:
                repo_names = ", ".join(sorted(config.repositories.keys()))
                repo_line = f"  Repositories in this archive: {repo_names}\n"
            about_block = (
                "WHOSE FILES ARE THESE?\n"
                "\n"
                "  These discs hold backup copies of digital files\n"
                f"  created by {owner}.\n"
                "\n"
                f"  Contents: {description}\n"
                f"{repo_line}"
                "\n"
            )
            if config.key_storage_hints:
                hints = "\n".join(
                    f"      {line.strip()}"
                    for line in config.key_storage_hints.strip().splitlines()
                )
                key_hints_block = f"    Where to find the password:\n{hints}\n"
            key_info_line = (
                "  * KEY_INFO.txt on this disc lists which key unlocks\n"
                "    which repository.\n"
            )
            if config.key_split:
                # Same split-key pre-step text as KEY_INFO.txt — one
                # source so the doc-contract gates cover both.
                split_block = (
                    "\n" + "\n".join(_share_recovery_lines(config)).rstrip() + "\n"
                )
            if config.technical_contact:
                contact_block = (
                    "  The archive owner suggested this technical contact:\n"
                    f"      {config.technical_contact}\n"
                    "\n"
                )

        return f"""\
╔══════════════════════════════════════════════════════════╗
║                    START HERE                           ║
╚══════════════════════════════════════════════════════════╝

This is the LCSAS META-VOLUME — it contains everything needed
to recover the files on the LCSAS archive discs.

{about_block}╔══ Pick the section for your operating system ═══════════╗

  >>> Windows 10 or 11 <<<
       Open this disc in File Explorer and double-click
       restore.bat
       (See recovery/docs/RECOVER_WINDOWS.txt for details.)

  >>> macOS <<<
       Open Terminal, then run:
           sh /Volumes/{label}/restore.sh ~/restored
       (See recovery/docs/RECOVER.txt for details.)

  >>> Linux <<<
       Open a terminal, then run:
           sh /media/$USER/{label}/restore.sh ~/restored
       (or:  sudo mount /dev/sr0 /mnt
        then: sh /mnt/restore.sh ~/restored)
       (See recovery/docs/RECOVER.txt for details.)

{no_os_block}
╚══════════════════════════════════════════════════════════╝

WHAT YOU NEED

  * The password for this archive (the original owner should
    have written it down separately from the discs).
{key_hints_block}{key_info_line}  * Enough free disk space on your computer for the restored
    files.  Typical archives are 10 GB to several TB.
  * A USB Blu-ray reader (or any optical drive that can read
    the disc format used by this archive).
{split_block}
WHAT HAPPENS IF YOU LOSE THE PASSWORD

  The data is unrecoverable.  This is by design — the password
  is the only key to the encryption.  There is no back door,
  no vendor recovery service, no master key held anywhere.

INHERITED A WHOLE STACK OF DISCS?

  Look at the disc labels.  ONE of them will be labelled
  {label} (or similar) — that is THIS disc.  Start with it.
  The recovery process will tell you which numbered data disc
  to insert next.

NEED HELP?

{contact_block}  Take all the discs plus the password to any computer
  professional.  Any system administrator or IT professional
  should be able to follow the instructions in
  recovery/docs/RECOVER.txt.

  The full source code is in recovery/src/, so a sufficiently
  motivated future implementer can rebuild the tooling from
  scratch even if every prebuilt binary on the disc has gone
  unusable.
"""
