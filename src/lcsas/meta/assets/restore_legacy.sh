#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  LCSAS Disc-Only Restore — bootstrap script
#
#  Restores data from LCSAS archive discs using ONLY:
#    1. This meta disc  (tools + source)
#    2. The data discs (or their ISO images)
#    3. Your password file
#
#  On the meta disc this is recovery/scripts/restore_legacy.sh; the
#  disc root's restore.sh is the newer driver.  (A meta disc built
#  without the recovery/ tree has this script as its root restore.sh.)
#
#  Two modes:
#    Single-drive (DEFAULT) — models the disaster scenario: you own one
#      optical drive and a stack of archive discs. Script prompts for
#      each disc by label, reads it in place, and ingests only the
#      packs needed for the target repository.
#
#        restore_legacy.sh --key PASSWORD_FILE --target TARGET [--repo REPO]
#                          [--drive /dev/sr0] [--snapshot ID]
#
#    Directory (opt-in, legacy) — you already have every ISO image on
#      disk. Script extracts them all and runs the classic flow.
#
#        restore_legacy.sh --key PASSWORD_FILE --isos ISO_DIR --target TARGET
#                          [--repo REPO] [--snapshot ID]
#
#  Rustic binary cascade:
#    1. bundled rustic         (dynamically linked)
#    2. bundled rustic-static  (statically linked, no glibc dependency)
#    3. system rustic          (if installed on host)
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# tools/, metadata/ and standalone_restorer.py sit at the disc root.
# This script lives either in recovery/scripts/ or at the root itself.
case "$SCRIPT_DIR" in
    */recovery/scripts) DISC_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)" ;;
    *)                  DISC_ROOT="$SCRIPT_DIR" ;;
esac
TOOLS="$DISC_ROOT/tools"

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

# ── Resolve Python + standalone_restorer.py (tier-3 fallback) ──
PYTHON=""
STANDALONE=""
if [[ -x "${TOOLS}/bin/python3" ]]; then
    PYTHON="${TOOLS}/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON="$(command -v python3)"
fi

# Look for standalone_restorer.py shipped on the meta disc, then
# inside any extracted data disc (it's placed on every disc).
if [[ -f "$DISC_ROOT/standalone_restorer.py" ]]; then
    STANDALONE="$DISC_ROOT/standalone_restorer.py"
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
LCSAS Restore

Single-drive mode (DEFAULT):
  $0 --key PASSWORD_FILE --target TARGET [--repo NAME]
      [--drive /dev/sr0] [--snapshot ID]

  Insert any LCSAS archive disc into the drive. The script reads
  the catalog, tells you which discs to insert next, and restores
  the repository onto disk. Only one disc is mounted at a time.

Directory mode (opt-in, legacy):
  $0 --key PASSWORD_FILE --isos ISO_DIR --target TARGET
      [--repo NAME] [--snapshot ID]

  Use when the ISO image of every data disc is already on disk.

Options:
  --key FILE        (required) Path to the password file
  --target DIR      (required) Where to restore files
  --repo NAME       Repository to restore
  --snapshot ID     Snapshot to restore (default: latest)
  --drive DEV       Optical drive in single-drive mode (default: /dev/sr0)
  --isos DIR        Opt-in: directory of ISO images (legacy mode)
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
[[ ! -f "$KEY_FILE" ]] && { echo "ERROR: Password file not found: $KEY_FILE"; exit 1; }

# Mode selection: --isos present → directory mode; else single-drive.
MODE="single-drive"
if [[ -n "$ISO_DIR" ]]; then
    MODE="directory"
    [[ ! -d "$ISO_DIR" ]] && { echo "ERROR: ISO directory not found: $ISO_DIR"; exit 1; }
fi

# ── Check at least one restore method is available ──────────────
USE_PYTHON_FALLBACK=0
if [[ -z "$RUSTIC" ]]; then
    if [[ -n "$PYTHON" ]] && [[ -n "$STANDALONE" ]]; then
        echo "  WARNING: No rustic/restic binary found."
        echo "  Falling back to the Python reader (slower but functional)."
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
echo "  LCSAS Restore  ($MODE mode)"
echo "═══════════════════════════════════════════════════"
echo "  Password file:  $KEY_FILE"
if [[ "$MODE" == "single-drive" ]]; then
    echo "  Drive:          $DRIVE"
else
    echo "  ISO images:     $ISO_DIR"
fi
echo "  Target:         $TARGET"
echo "  Work dir:       $WORK_DIR"
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

    # Phase 1 — bootstrap. The meta disc carries repository metadata
    # (keys, config) but NO catalog — it would always be stale.
    # The heir inserts any data disc and we bootstrap from its
    # catalog. If a later disc has a fresher catalog, we upgrade
    # organically during Phase 2.
    echo ""
    echo "--- Phase 1: Bootstrap ---"
    # Seed keys/config from meta disc if available (they don't go stale).
    BOOTSTRAP_META=""
    if [[ -d "$DISC_ROOT/metadata" ]]; then
        BOOTSTRAP_META="$DISC_ROOT/metadata"
    fi
    echo "  Insert any LCSAS archive disc to begin."
    echo "  (Tip: start with the disc whose label is newest: the latest"
    echo "  year, then the highest number. Its catalog is the fullest,"
    echo "  so you will need fewer disc swaps.)"
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
            echo "Re-run with: $0 --key $KEY_FILE --target $TARGET --repo NAME"
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
        # This handles the common case where the first disc inserted
        # was not a disc of the newest volume, so its catalog is stale.
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

    # Phase 3 — check completeness and integrity.
    echo ""
    echo "--- Phase 3: Finalize ---"
    "$PYTHON" "$HELPER" finalize --cache "$CACHE_DIR" --verify-integrity
    FINALIZE_RC=$?
    if [[ $FINALIZE_RC -eq 3 ]]; then
        echo ""
        echo "FATAL: some packs have no remaining alternate discs."
        echo "       This restore cannot complete with the discs available."
        echo "       If you have another disc with the same label as a damaged"
        echo "       one (for example, from another location), insert it and run again."
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
    echo "ERROR: No ISO image with a metadata/ directory found"
    exit 1
fi

echo "  Using metadata from: $(basename "$LATEST_VOL")"

# Build list of repos to restore
declare -a REPOS
for repo_dir in "$LATEST_VOL/metadata"/*/; do
    [[ ! -d "$repo_dir" ]] && continue
    repo_name="$(basename "$repo_dir")"
    REPOS+=("$repo_name")
    echo "  Found repository: $repo_name"
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
                # Check that the SHA-256 of the copied pack matches its filename
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
        echo "    WARNING: $PACK_ERRORS packs failed the SHA-256 check"
        echo "    Some data discs may be damaged — try another disc with the same label"
    fi
    echo "    Ingested $PACK_COUNT packs from $ISO_COUNT ISO images"

    # ── Check all required packs were ingested ────────────────
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
        # ── Python-reader (tier 3) restore via standalone_restorer.py ─
        # Find standalone_restorer.py — meta disc copy (already resolved)
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
