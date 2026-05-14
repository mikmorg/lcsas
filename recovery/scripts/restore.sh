#!/bin/sh
# restore.sh -- POSIX-sh driver for the LCSAS recovery cascade.
#
# Bare-minimum recovery is prebuilt static binaries + POSIX sh ONLY.
# Python is NOT on the bare path; it lives on a separate tier (3) that
# is only reached when every C-based option has failed.
#
# Cascade (bare-minimum path = tiers 1-2):
#
#   Tier 1.  bin/<arch>/lcsas-restore        prebuilt static C89 binary
#   Tier 2.  bin/<arch>/rustic-static        vendored Rust binary (cross-check)
#   ----- Bare minimum stops here.  Neither of the above needs Python. -----
#   Tier 3.  python3 standalone_restorer.py  (only if all above failed)
#
# The script can be invoked two ways:
#
#   1. Auto-locate from script path:
#        sh /path/to/restore.sh [TARGET_DIR] [SNAPSHOT_ID|latest]
#      Recovery root is auto-detected from $0's location.
#
#   2. Explicit:
#        sh restore.sh RECOVERY_ROOT TARGET_DIR [SNAPSHOT_ID|latest]
#
# Inputs:
#   $LCSAS_PASSWORD  -- if set, used as password (otherwise prompts on stdin)
#   $LCSAS_PWFILE    -- if set, path to a password file (overrides above)

set -eu

# ── Argument handling and auto-discovery ──────────────────────────

SCRIPT="$(
    # POSIX-portable realpath approximation.
    cd "$(dirname "$0")" 2>/dev/null && pwd -P
)/$(basename "$0")"
SCRIPT_DIR="$(dirname "$SCRIPT")"

# ── Single-drive guard: relocate to RAM before going further ──────
#
# If this script is being interpreted off a read-only optical medium
# (the meta-disc), `sh` keeps an open file descriptor on it and the
# user cannot eject -- which is fatal for anybody with only ONE
# optical drive.  Detect that case and re-exec ourselves from a
# writable directory after copying the script + the chosen binary
# tier into RAM (or any host writable dir).  Subsequent disc swaps
# then operate freely.
#
# LCSAS_RELOCATED is the sentinel: when set, we are the in-RAM copy
# and its value is the path of the original meta-disc mount.

find_meta_mount() {
    # Print the filesystem mount point covering "$1" on stdout.
    # Falls back to "$1" itself when neither findmnt nor df work.
    if command -v findmnt >/dev/null 2>&1; then
        m="$(findmnt -n -o TARGET --target "$1" 2>/dev/null || true)"
        if [ -n "$m" ]; then printf '%s\n' "$m"; return; fi
    fi
    if command -v df >/dev/null 2>&1; then
        # POSIX df -P prints the mount point in field 6 of line 2.
        m="$(df -P "$1" 2>/dev/null | awk 'NR==2 {
            out=""; for (i=6;i<=NF;i++) out=out (i>6?" ":"") $i; print out
        }')"
        if [ -n "$m" ]; then printf '%s\n' "$m"; return; fi
    fi
    printf '%s\n' "$1"
}

relocate_to_ram() {
    # $1 = original meta-disc mount root.
    orig_mount="$1"; shift
    # Pick a writable scratch dir that is NOT inside the meta-disc.
    ramdir=""
    for cand in "${TMPDIR:-}" "${XDG_RUNTIME_DIR:-}" /tmp /run /var/tmp; do
        [ -n "$cand" ] || continue
        [ -d "$cand" ] || continue
        [ -w "$cand" ] || continue
        case "$cand" in "$orig_mount"|"$orig_mount"/*) continue;; esac
        ramdir="$(mktemp -d "$cand/lcsas-restore.XXXXXX" 2>/dev/null || true)"
        [ -n "$ramdir" ] && [ -d "$ramdir" ] && break
        ramdir=""
    done
    if [ -z "$ramdir" ]; then
        printf '[lcsas-restore] cannot find a writable dir to relocate to; ' >&2
        printf 'continuing from %s (drive will be held)\n' "$orig_mount" >&2
        return 1
    fi

    # Mirror the on-disc layout under $ramdir so the script's own
    # AUTO_RECOVERY logic resolves $ramdir/recovery as RECOVERY.
    mkdir -p "$ramdir/recovery/scripts" "$ramdir/recovery/bin"
    cp -f "$SCRIPT" "$ramdir/recovery/scripts/restore.sh"
    chmod +x "$ramdir/recovery/scripts/restore.sh"
    if [ -f "$SCRIPT_DIR/detect_arch.sh" ]; then
        cp -f "$SCRIPT_DIR/detect_arch.sh" "$ramdir/recovery/scripts/detect_arch.sh"
        chmod +x "$ramdir/recovery/scripts/detect_arch.sh"
    fi
    # Preserve the bin/ tree so tier-1/tier-2 still resolve.
    if [ -d "$SCRIPT_DIR/../bin" ]; then
        cp -R "$SCRIPT_DIR/../bin/." "$ramdir/recovery/bin/" 2>/dev/null || true
    fi
    # Catalog sidecar for prompt hints (small enough to copy).
    for cat_cand in \
        "$SCRIPT_DIR/../catalog.db" \
        "$SCRIPT_DIR/catalog.db" \
        "$SCRIPT_DIR/../../catalog.db"
    do
        [ -f "$cat_cand" ] || continue
        cp -f "$cat_cand" "$ramdir/recovery/catalog.db" 2>/dev/null || true
        break
    done

    printf '[lcsas-restore] copied recovery binaries to %s\n' "$ramdir" >&2
    printf '[lcsas-restore] you may eject the recovery disc when the ' >&2
    printf 'binary prompts for a data disc.\n' >&2

    LCSAS_RELOCATED="$orig_mount"
    export LCSAS_RELOCATED
    # cd outside the meta-disc so the new sh inherits a safe cwd.
    cd / 2>/dev/null || true
    # Re-exec the relocated script with the SAME positional args.
    exec "$ramdir/recovery/scripts/restore.sh" "$@"
}

# Detect read-only / iso9660 / udf / squashfs by writability of
# SCRIPT_DIR.  When LCSAS_RELOCATED is already set (re-exec'd copy)
# or LCSAS_NO_RELOCATE=1 (tests/dev), skip relocation entirely.
if [ -z "${LCSAS_RELOCATED:-}" ] && [ "${LCSAS_NO_RELOCATE:-0}" != "1" ]; then
    relocate_needed=0
    # Strongest signal: the caller (lcsas-init or a test harness)
    # explicitly named the meta-disc and we are running inside it.
    if [ -n "${LCSAS_META_DISC:-}" ]; then
        case "$SCRIPT_DIR" in
            "$LCSAS_META_DISC"|"$LCSAS_META_DISC"/*) relocate_needed=1 ;;
        esac
    fi
    # Fallback: probe for read-only / optical filesystem types.
    if [ "$relocate_needed" = "0" ] && [ ! -w "$SCRIPT_DIR" ]; then
        relocate_needed=1
    fi
    if [ "$relocate_needed" = "0" ] && command -v findmnt >/dev/null 2>&1; then
        fstype="$(findmnt -n -o FSTYPE --target "$SCRIPT_DIR" 2>/dev/null || true)"
        case "$fstype" in
            iso9660|udf|squashfs|cramfs|romfs) relocate_needed=1 ;;
        esac
    fi
    if [ "$relocate_needed" = "1" ]; then
        # Trust an explicit override from the caller (lcsas-init sets
        # LCSAS_META_DISC=/mnt under the initramfs).  Otherwise probe.
        if [ -n "${LCSAS_META_DISC:-}" ]; then
            meta_mount="$LCSAS_META_DISC"
        else
            meta_mount="$(find_meta_mount "$SCRIPT_DIR")"
        fi
        relocate_to_ram "$meta_mount" "$@" || true
        # If relocate_to_ram returned without exec, fall through and
        # continue from the original location -- best effort.
    fi
fi

# Record the meta-disc mount so we can pass it on to lcsas-restore.
# Prefer the post-relocation sentinel; fall back to an explicit env
# var (e.g. set by lcsas-init); otherwise leave empty.
META_DISC="${LCSAS_RELOCATED:-${LCSAS_META_DISC:-}}"

# When invoked as $RECOVERY/scripts/restore.sh, RECOVERY is parent dir.
# When invoked as $META/restore.sh (top-level), RECOVERY is $META/recovery.
AUTO_RECOVERY=""
if [ -d "$SCRIPT_DIR/../scripts" ] && [ "$SCRIPT_DIR" != "$SCRIPT_DIR/../scripts" ]; then
    AUTO_RECOVERY="$(cd "$SCRIPT_DIR/.." && pwd -P)"
elif [ -d "$SCRIPT_DIR/recovery/scripts" ]; then
    AUTO_RECOVERY="$SCRIPT_DIR/recovery"
fi

RECOVERY=""
TARGET=""
SNAP="latest"

# --help / -h short-circuit: print usage to stdout and exit 0 so this
# script is friendly to interactive users and to test harnesses that
# probe for a usage block.
case "${1:-}" in
    -h|--help)
        cat <<EOF
usage: $0 [RECOVERY_ROOT] TARGET_DIR [SNAPSHOT_ID|latest]

  RECOVERY_ROOT (auto-detected when restore.sh is run from inside the
  recovery tree) must contain bin/<arch>/lcsas-restore and/or src/.
  TARGET_DIR is where to write restored files (default: /tmp/restored).

  Password is read from stdin, \$LCSAS_PASSWORD env, or \$LCSAS_PWFILE.
EOF
        exit 0
        ;;
esac

# Pattern 1: first arg looks like a recovery root (has bin/ or src/).
if [ $# -ge 2 ] && [ -d "$1/bin" -o -d "$1/src" ] 2>/dev/null; then
    RECOVERY="$1"
    TARGET="$2"
    SNAP="${3:-latest}"
elif [ $# -ge 1 ] && [ -n "$AUTO_RECOVERY" ]; then
    RECOVERY="$AUTO_RECOVERY"
    TARGET="$1"
    SNAP="${2:-latest}"
elif [ -n "$AUTO_RECOVERY" ]; then
    RECOVERY="$AUTO_RECOVERY"
    TARGET="${TARGET:-/tmp/restored}"
else
    cat >&2 <<EOF
usage: $0 [RECOVERY_ROOT] TARGET_DIR [SNAPSHOT_ID|latest]

  RECOVERY_ROOT (auto-detected when restore.sh is run from inside the
  recovery tree) must contain bin/<arch>/lcsas-restore and/or src/.
  TARGET_DIR is where to write restored files (default: /tmp/restored).

  Password is read from stdin, \$LCSAS_PASSWORD env, or \$LCSAS_PWFILE.
EOF
    exit 2
fi

# ── Architecture detection ────────────────────────────────────────

if [ -x "$RECOVERY/scripts/detect_arch.sh" ]; then
    ARCH="$(sh "$RECOVERY/scripts/detect_arch.sh" 2>/dev/null || uname -m)"
else
    ARCH="$(uname -m)"
fi
case "$ARCH" in
    x86_64|amd64)        ARCH=x86_64 ;;
    aarch64|arm64)       ARCH=aarch64 ;;
    riscv64)             ARCH=riscv64 ;;
    *)
        printf 'unsupported arch: %s\n' "$ARCH" >&2
        exit 1 ;;
esac

# ── Password file ─────────────────────────────────────────────────

PWFILE="${LCSAS_PWFILE:-}"
PWFILE_TMP=""
if [ -z "$PWFILE" ]; then
    PWFILE_TMP="$(mktemp /tmp/lcsas-pw.XXXXXX)"
    chmod 600 "$PWFILE_TMP"
    PWFILE="$PWFILE_TMP"
    if [ -n "${LCSAS_PASSWORD:-}" ]; then
        printf '%s\n' "$LCSAS_PASSWORD" > "$PWFILE"
    else
        printf 'Password: ' >&2
        IFS= read -r pw
        printf '%s\n' "$pw" > "$PWFILE"
    fi
fi
cleanup() {
    [ -n "$PWFILE_TMP" ] && [ -f "$PWFILE_TMP" ] && rm -f "$PWFILE_TMP"
}
trap cleanup EXIT INT TERM

# ── Repo discovery ────────────────────────────────────────────────

REPO=""
for candidate in "$RECOVERY/repo" "$RECOVERY"; do
    if [ -d "$candidate/keys" ] && [ -d "$candidate/index" ]; then
        REPO="$candidate"
        break
    fi
done
if [ -z "$REPO" ]; then
    printf 'no restic repo (with keys/ and index/) found under %s\n' \
           "$RECOVERY" >&2
    exit 1
fi

mkdir -p "$TARGET"

# ── Auto-discover other mounted discs for multi-disc recovery ─────
#
# When packs are split across multiple LCSAS volumes, the user may
# have several discs mounted simultaneously.  We scan the usual mount
# points on macOS, Linux, and BSD and pass each as --pack-search to
# the recovery binary.

PACK_SEARCH_ARGS=""
add_pack_search() {
    # $1: a path that might contain restic data.
    # Skip anything under the meta-disc -- a single-drive user cannot
    # rely on the meta-disc as a pack source AND eject it.
    if [ -n "${META_DISC:-}" ]; then
        case "$1" in
            "$META_DISC"|"$META_DISC"/*) return ;;
        esac
    fi
    # Add it if data/ exists, or if the path itself contains pack files.
    if [ -d "$1/data" ]; then
        PACK_SEARCH_ARGS="$PACK_SEARCH_ARGS --pack-search $1"
        return
    fi
    if [ -d "$1/repo/data" ]; then
        PACK_SEARCH_ARGS="$PACK_SEARCH_ARGS --pack-search $1/repo"
        return
    fi
}

# macOS / BSD: /Volumes/*
if [ -d /Volumes ]; then
    for mnt in /Volumes/*; do
        [ -d "$mnt" ] || continue
        # Don't list the recovery medium itself again.
        [ "$mnt" = "$RECOVERY" ] && continue
        add_pack_search "$mnt"
    done
fi
# Linux: /media/$USER/*, /media/*, /mnt/*
for parent in "/media/$(id -un 2>/dev/null)" /media /mnt; do
    [ -d "$parent" ] || continue
    for mnt in "$parent"/*; do
        [ -d "$mnt" ] || continue
        [ "$mnt" = "$RECOVERY" ] && continue
        add_pack_search "$mnt"
    done
done

# Pass the meta-disc path through so the C-side locator excludes it
# from its own search list and drops cwd outside of it before prompts.
META_DISC_ARG=""
if [ -n "${META_DISC:-}" ]; then
    META_DISC_ARG="--meta-disc $META_DISC"
fi

# Optional --catalog if a catalog.db is present somewhere reachable.
# Used for human-readable volume hints in prompts.
#
# The meta-disc deliberately carries NO catalog.db (it would always be
# stale at burn time -- see src/lcsas/meta/builder.py).  So we scan
# every currently-mounted data disc and pick the FRESHEST catalog by
# mtime, falling back to the recovery tree if nothing better is found.
CATALOG_ARG=""
catalog_pick=""
catalog_pick_mtime=0
catalog_consider() {
    # Note the explicit `return 0` -- under `set -e` an implicit
    # return after a failed test would propagate as a non-zero exit.
    [ -f "$1" ] || return 0
    mt="$(stat -c '%Y' "$1" 2>/dev/null \
        || stat -f '%m' "$1" 2>/dev/null \
        || echo 0)"
    if [ "$mt" -gt "$catalog_pick_mtime" ] 2>/dev/null; then
        catalog_pick="$1"
        catalog_pick_mtime="$mt"
    fi
    return 0
}
# Local recovery-tree candidates (last resort).
for cand in "$RECOVERY/catalog.db" "$REPO/catalog.db" \
            "$RECOVERY/../catalog.db"; do
    catalog_consider "$cand"
done
# Mounted discs: /Volumes/* (macOS), /media/$USER/* /media/* /mnt/* (Linux).
if [ -d /Volumes ]; then
    for mnt in /Volumes/*; do
        [ -d "$mnt" ] || continue
        catalog_consider "$mnt/catalog.db"
    done
fi
for parent in "/media/$(id -un 2>/dev/null)" /media /mnt; do
    [ -d "$parent" ] || continue
    for mnt in "$parent"/*; do
        [ -d "$mnt" ] || continue
        catalog_consider "$mnt/catalog.db"
    done
done
if [ -n "$catalog_pick" ]; then
    CATALOG_ARG="--catalog $catalog_pick"
    printf '[lcsas-restore] using catalog %s\n' "$catalog_pick" >&2
fi

# ── Tier 1: prebuilt lcsas-restore (C89, static, no Python) ───────

RESTORE_BIN="$RECOVERY/bin/$ARCH/lcsas-restore"
if [ -x "$RESTORE_BIN" ]; then
    printf '[tier 1] using prebuilt lcsas-restore (%s)\n' "$ARCH" >&2
    # Drop cwd outside the meta-disc before exec, so the kernel does
    # not hold it through the exec barrier.
    [ -n "${META_DISC:-}" ] && cd / 2>/dev/null || true
    exec "$RESTORE_BIN" --repo "$REPO" --password-file "$PWFILE" \
                       --target "$TARGET" --snapshot "$SNAP" \
                       $PACK_SEARCH_ARGS $CATALOG_ARG $META_DISC_ARG
fi

# ── Tier 2: vendored rustic-static (no Python) ────────────────────

RUSTIC_BIN="$RECOVERY/bin/$ARCH/rustic-static"
if [ -x "$RUSTIC_BIN" ]; then
    printf '[tier 2] using vendored rustic-static (%s)\n' "$ARCH" >&2
    exec "$RUSTIC_BIN" --repository "$REPO" --password-file "$PWFILE" \
                     restore "$SNAP" "$TARGET"
fi

# ─────────────────────────────────────────────────────────────────
# BARE-MINIMUM PATH ENDS HERE.  Everything above is statically-linked
# C/Rust + POSIX sh, with NO Python dependency at any step.
# ─────────────────────────────────────────────────────────────────

# ── Tier 3: Python fallback (LAST RESORT, off the bare path) ─────

if [ "${LCSAS_ALLOW_PYTHON_TIER:-1}" = "1" ]; then
    PYBIN=""
    for p in python3 python; do
        if command -v "$p" >/dev/null 2>&1; then PYBIN="$p"; break; fi
    done
    PYREST=""
    for cand in "$RECOVERY/../standalone_restorer.py" \
                "$RECOVERY/standalone_restorer.py" \
                "$SCRIPT_DIR/../standalone_restorer.py"; do
        if [ -f "$cand" ]; then PYREST="$cand"; break; fi
    done
    if [ -n "$PYBIN" ] && [ -n "$PYREST" ]; then
        printf '[tier 3] falling back to Python (%s + %s)\n' \
               "$PYBIN" "$PYREST" >&2
        exec "$PYBIN" "$PYREST" "$REPO" "$TARGET" --password-file "$PWFILE"
    fi
fi

cat >&2 <<EOF
ERROR: no recovery method available.

The bare-minimum recovery path (tiers 1-2) needs ONE of:
  * a prebuilt $RESTORE_BIN
  * a prebuilt $RUSTIC_BIN

The optional Python tier (tier 3) needs python3 and standalone_restorer.py.

See $RECOVERY/docs/RECOVER.txt for manual recovery instructions.
EOF
exit 1
