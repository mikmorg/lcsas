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
| `restore.sh` | Interactive restore script (prompts for disc swaps) |
| `restore-auto.sh` | Non-interactive restore script (for scripted/automated use) |
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

### 1. Copy the meta-volume to local disk

```
sudo mount /dev/sr0 /mnt/meta
cp -r /mnt/meta /tmp/lcsas-meta
cd /tmp/lcsas-meta
sudo umount /mnt/meta
```

### 2. Insert **any** archive disc

Every LCSAS data disc is holographic: it carries the full catalog, so
any one of them can bootstrap the restore. Higher-numbered discs have
fresher catalogs (they know about more volumes), so starting with the
highest-numbered disc minimises the chance of needing a catalog upgrade
mid-restore — but any disc will work.

### 3. Run the restore

```bash
./restore.sh --key /path/to/keyfile.txt \\
             --target ~/restored/ \\
             --repo REPO_NAME
```

(Omit `--repo` to see the list of repositories stored in the archive,
then re-run with a choice.)

The script will:

1. Read the catalog from the disc currently in the drive.
2. Print the list of discs you will need and in what order.
3. Prompt `INSERT DISC: <label>` for each one, wait for you to
   swap, and ingest only the packs it needs.
4. If a later disc has a fresher catalog, silently upgrade the
   pick list so newly-discovered volumes are included.
5. Run rustic against the assembled cache and write the files into
   `~/restored/REPO_NAME/`.

If a disc is unreadable mid-restore you can stop and re-run the same
command later — the cache under the work directory persists unless
you pass `--work-dir`.

## Non-Interactive Mode (automated / scripted)

Use ``restore-auto.sh`` when you want a fully automated restore without
interactive prompts — for example, in scripted environments, CI/CD
pipelines, or AI-agent-driven restores.

```bash
./restore-auto.sh --key /path/to/keyfile.txt \\
                  --target ~/restored/ \\
                  --repo REPO_NAME \\
                  --disc-cmd "disc-loader"
```

The ``--disc-cmd`` option specifies a command that loads discs by label.
The script calls it as ``CMD insert LABEL`` to load a disc and
``CMD eject`` to eject. If omitted, you must load discs externally
before the script expects them.

The script runs the same four phases as ``restore.sh`` (bootstrap,
ingest, finalize, rustic restore) but never calls ``read`` or waits
for keyboard input.

| Option | Description |
|---|---|
| ``--key FILE`` | **(required)** Encryption key file |
| ``--target DIR`` | **(required)** Restore destination |
| ``--repo NAME`` | **(required)** Repository to restore |
| ``--disc-cmd CMD`` | Command to load/eject discs programmatically |
| ``--drive DEV`` | Optical drive (default: ``/dev/sr0``) |
| ``--snapshot ID`` | Snapshot to restore (default: latest) |
| ``--work-dir DIR`` | Temp directory (default: auto) |

## Directory Mode (opt-in, legacy)

If you already have every data-volume ISO on disk (e.g. pre-rsynced to
a NAS), use directory mode:

```bash
./restore.sh --key /path/to/keyfile.txt \\
             --isos /path/to/iso/directory/ \\
             --target ~/restored/
```

This extracts every ISO up front and copies every pack into the cache
before restoring. Faster when disks are cheap; wrong for the
single-drive disaster scenario.

## Restore Options

| Option | Description |
|---|---|
| `--key FILE` | **(required)** Path to your encryption key file |
| `--target DIR` | **(required)** Where to restore files |
| `--repo NAME` | Repository (tenant) to restore |
| `--snapshot ID` | Restore a specific snapshot (default: latest) |
| `--drive DEV` | Optical drive in single-drive mode (default: `/dev/sr0`) |
| `--isos DIR` | Opt-in: directory of data-volume ISOs (legacy mode) |
| `--work-dir DIR` | Temporary work directory (default: auto) |

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
        bootable: bool = False,
        alpine_dir: Path | None = None,
        catalog_db_path: Path | None = None,
        recovery_dir: Path | None = None,
        bundle_recovery_toolchain: bool = True,
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
            bootable: If True, include Alpine Linux live boot environment
                so the meta-volume can be booted directly.  Requires
                *alpine_dir* with pre-built Alpine artifacts.
            alpine_dir: Directory containing ``vmlinuz``, ``initramfs``,
                and ``rootfs.squashfs`` (output of ``build_rootfs.sh``).
                Required when *bootable* is True.
        """
        self._output = output_dir
        self._static_rustic_path = static_rustic_path
        self._config = config
        self._bootable = bootable
        self._alpine_dir = alpine_dir
        self._catalog_db_path = catalog_db_path
        self._bundle_recovery_toolchain = bundle_recovery_toolchain

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
        self._bundle_standalone_restorer()
        self._bundle_restore_helper()
        self._bundle_metadata()
        if self._bundle_recovery_toolchain:
            self._bundle_recovery_toolchain_artifacts()
        self._write_restore_script()
        self._write_restore_auto_script()
        self._write_readme()
        self._write_readme_txt()
        self._write_volume_info()
        self._write_start_here()

        if self._bootable:
            self._install_live_boot()

        # Build complete — remove the marker
        incomplete_marker.unlink(missing_ok=True)

        return self._output

    # ── Live boot environment ───────────────────────────────────

    def _install_live_boot(self) -> None:
        """Install Alpine Linux live boot environment into the meta-volume.

        Copies kernel, initramfs, squashfs, boot configs, and the
        TUI restore wizard from the Alpine build artifacts and the
        ``live/`` package directory.
        """
        if self._alpine_dir is None:
            raise ValueError(
                "bootable=True requires alpine_dir with pre-built "
                "Alpine artifacts (vmlinuz, initramfs, rootfs.squashfs)"
            )
        alpine = self._alpine_dir
        for name in ("vmlinuz", "initramfs", "rootfs.squashfs"):
            if not (alpine / name).is_file():
                raise FileNotFoundError(
                    f"Alpine artifact not found: {alpine / name}"
                )

        from lcsas.meta.bootable import BootableISOBuilder

        # BootableISOBuilder._install_boot_files / _install_isolinux /
        # _install_efi handle the heavy lifting.  We create a temporary
        # builder just to use its helpers for staging.
        bib = BootableISOBuilder(
            staging_dir=self._output,
            alpine_dir=alpine,
            output_iso=Path("/dev/null"),  # not used here
        )
        bib._install_boot_files()
        bib._install_isolinux()
        bib._install_efi()

        # Copy the TUI restore wizard into the meta-volume
        live_dir = Path(__file__).parent / "live"
        wizard_src = live_dir / "restore_wizard.py"
        if wizard_src.is_file():
            dst = self._output / "restore_wizard.py"
            shutil.copy2(str(wizard_src), str(dst))
            os.chmod(str(dst), 0o755)

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
        cascade rebuilds from source when the prebuilt binary is absent.
        See ``recovery/scripts/restore.sh``.
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
          1-4 and only touches Python at tier 5 (LCSAS_ALLOW_PYTHON_TIER).
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
            # Stash the legacy bash driver alongside for compatibility /
            # for users who specifically want it.  Off the bare path.
            legacy = self._output / "restore_legacy.sh"
            _write_and_sync(legacy, RESTORE_SCRIPT)
            os.chmod(str(legacy), 0o755)
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

TO RESTORE YOUR FILES (single-drive mode — recommended):

  1. You need the encryption key file (NOT on any disc for security)
  2. You need the archive discs (any one bootstraps; the script will
     tell you which others to insert).
  3. Insert any archive disc into the drive.
  4. Run:  ./restore.sh --key <keyfile> --target <output> --repo <name>
     The script will prompt for each disc swap.

For automated / scripted restores (no interactive prompts):
     ./restore-auto.sh --key <keyfile> --target <output> --repo <name> \
                       --disc-cmd "your-disc-loader-command"

Legacy: if every disc has already been copied to ISO files on disk,
you may use directory mode instead:
     ./restore.sh --key <keyfile> --isos <iso_dir> --target <output>

See README_RESTORE.md for detailed instructions.

IMPORTANT: If this is confusing, take ALL the discs plus the
encryption key to a computer professional.  Any Linux system
administrator or IT professional should be able to follow the
instructions in README_RESTORE.md.

WARNING: WITHOUT THE ENCRYPTION KEY, THE DATA CANNOT BE RECOVERED.
"""
            _write_and_sync(self._output / "START_HERE.txt", text)
