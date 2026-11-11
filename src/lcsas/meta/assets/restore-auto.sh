#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  LCSAS Non-Interactive Restore — automated disc-swap restore
#
#  Drives the same bootstrap → ingest → finalize → rustic pipeline
#  as restore.sh but without interactive prompts.  Designed for:
#    • Scripted / automated restore environments
#    • AI agent-driven restores
#    • CI/CD test harnesses
#
#  Disc loading is delegated to a caller-supplied command via --disc-cmd.
#  The command is called as:  $DISC_CMD insert <LABEL>
#                              $DISC_CMD eject
#
#  If --disc-cmd is omitted, the script assumes the discs will be
#  loaded externally and waits for the drive to become readable.
#
#  On the meta disc this is recovery/scripts/restore-auto.sh.
#
#  Usage:
#    recovery/scripts/restore-auto.sh --key PASSWORD_FILE --target TARGET \
#        --repo NAME [--drive /dev/sr0] [--disc-cmd CMD] \
#        [--snapshot ID] [--work-dir DIR]
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# tools/, metadata/ and standalone_restorer.py sit at the disc root,
# two levels above recovery/scripts/.
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
if [[ -f "$DISC_ROOT/standalone_restorer.py" ]]; then
    STANDALONE="$DISC_ROOT/standalone_restorer.py"
fi

# ── Usage ────────────────────────────────────────────────────────
usage() {
    cat <<EOF
LCSAS Non-Interactive Restore (automated disc-swap)

Usage:
  $0 --key PASSWORD_FILE --target TARGET --repo NAME \\
      [--drive /dev/sr0] [--disc-cmd CMD] \\
      [--snapshot ID] [--work-dir DIR]

Options:
  --key FILE        (required) Path to the password file
  --target DIR      (required) Where to restore files
  --repo NAME       (required) Repository to restore
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
[[ ! -f "$KEY_FILE" ]] && { echo "ERROR: Password file not found: $KEY_FILE"; exit 1; }

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

# ── Check at least one restore method is available ───────────────
USE_PYTHON_FALLBACK=0
if [[ -z "$RUSTIC" ]]; then
    if [[ -n "$PYTHON" ]] && [[ -n "$STANDALONE" ]]; then
        echo "  WARNING: No rustic/restic binary found."
        echo "  Falling back to the Python reader (slower)."
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
    # Check label if possible.
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
echo "  Password file:  $KEY_FILE"
echo "  Drive:          $DRIVE"
echo "  Target:         $TARGET"
echo "  Repo:           $REPO"
echo "  Disc cmd:       ${DISC_CMD:-<manual>}"
echo "  Work dir:       $WORK_DIR"
echo ""

CACHE_DIR="$WORK_DIR/cache"
mkdir -p "$CACHE_DIR"

# ── Seed keys/config from meta disc if available ─────────────────
if [[ -d "$DISC_ROOT/metadata" ]]; then
    echo "  Seeding repository metadata from the meta disc..."
fi

# ═════════════════════════════════════════════════════════════════
#  Phase 1 — Bootstrap
# ═════════════════════════════════════════════════════════════════
echo ""
echo "--- Phase 1: Bootstrap ---"
echo "  Need any data disc to read the catalog."

# Prefer a disc of the newest volume (the latest year in its label,
# then the highest number): its catalog is the fullest, so fewer disc
# swaps are needed. Any data disc still works.
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
    # Get the last LCSAS_CD_* label in sort order: the newest volume
    # (latest year, then highest number), whose catalog is the fullest.
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
    echo "FATAL: some packs have no remaining alternate discs."
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
