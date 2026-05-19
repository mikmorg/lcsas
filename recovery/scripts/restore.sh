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

QUICK START:
  1. Insert ANY data disc into your drive.
  2. Mount it (typically: sudo mount /dev/sr0 /mnt).
  3. Run: sh /mnt/restore.sh ~/restored/ latest
  4. Answer the prompts (repository, password).
  5. When asked to swap discs, do so and press Enter.

  RECOVERY_ROOT (auto-detected when restore.sh is run from inside the
  recovery tree) must contain bin/<arch>/lcsas-restore and/or src/.
  TARGET_DIR is where to write restored files (default: /tmp/restored).

ENVIRONMENT VARIABLES:
  LCSAS_PASSWORD          Encryption password (skips the Password: prompt).
                          Mutually exclusive with LCSAS_PWFILE.
  LCSAS_PWFILE            Path to a file whose contents are the password.
                          Read instead of prompting; preserves any
                          trailing newline in the file.
  LCSAS_REPO              Repository / tenant name to restore.  Skips
                          the multi-tenant prompt on archives with
                          more than one repo.
  LCSAS_TARGET            Override the auto-detected rust-triple (e.g.
                          force x86_64-unknown-linux-musl on a host
                          where uname -m misreports).
  LCSAS_META_DISC         Mount point of the recovery / meta disc.
                          Tells the tier-1 binary not to look for
                          packs there and to chdir out before prompts.
                          Auto-detected via mtab when omitted.
  LCSAS_MOUNT_DIRS        Colon-separated list of directories to scan
                          for mounted data discs (default:
                          /Volumes:/media/<user>:/media:/mnt:/run/media/<user>).
                          Set to '' (empty) to disable the auto-scan.
  LCSAS_PACK_CACHE_DIR    Opportunistic pack cache.  'auto' (default
                          when unset) → \${TMPDIR:-/tmp}/lcsas-pack-cache.<pid>;
                          a path → that path; '' (empty) → cache off.
                          Trades disk space for fewer disc swaps.
  LCSAS_TIER_FALLBACK     0 (default) → tier 1 crash aborts the run.
                          1 → fall through to tier 2 / tier 3 on
                          non-zero exit.  Use when you suspect a bug
                          in a higher tier and want the script to
                          walk the cascade for you.
  LCSAS_ALLOW_NO_PACK_SEARCH
                          1 → suppress the 'no data discs detected'
                          hard-error (advanced / test environments).
  LCSAS_NO_RELOCATE       1 → don't copy the recovery scripts into
                          RAM before exec (testing / development).
  LCSAS_PROGRESS          0 → silence the periodic tier-3 progress
                          stderr lines.  Default ON.

Most operators don't need any of these.  See the meta disc's
README_RESTORE.md and TROUBLESHOOTING.md for the operator-friendly
walkthrough.
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

# ── Target detection (arch + OS) ──────────────────────────────────
#
# Picks one of the six targets bundled by the meta-builder (see
# docs/CROSS_PLATFORM_META_RFC.md §3 and recovery/UPSTREAM.sha256):
#
#   x86_64-unknown-linux-musl        Linux x86_64
#   aarch64-unknown-linux-musl       Linux ARM64
#   armv7-unknown-linux-gnueabihf    Linux 32-bit ARM
#   aarch64-apple-darwin             macOS Apple Silicon
#   x86_64-apple-darwin              macOS Intel
#   x86_64-pc-windows-gnu            Windows (POSIX driver path)
#
# Override with $LCSAS_TARGET if auto-detection misfires.

if [ -x "$RECOVERY/scripts/detect_arch.sh" ]; then
    MACHINE="$(sh "$RECOVERY/scripts/detect_arch.sh" 2>/dev/null || uname -m)"
else
    MACHINE="$(uname -m)"
fi
OS="$(uname -s 2>/dev/null || printf 'Linux\n')"

# IMPORTANT: $TARGET above is the *user-supplied restore directory*
# (positional arg 1 or 2).  From here on we need a separate name for
# the per-platform rust-triple that selects which binary to dispatch
# to under recovery/bin/.  Stuff the user's TARGET into TARGET_DIR
# and free up TARGET to hold the triple — which the rest of the
# script reads when computing $RESTORE_BIN / $RUSTIC_BIN paths.
TARGET_DIR="$TARGET"
TARGET=""

if [ -n "${LCSAS_TARGET:-}" ]; then
    TARGET="$LCSAS_TARGET"
else
    case "$OS" in
        Linux)
            case "$MACHINE" in
                x86_64|amd64)        TARGET="x86_64-unknown-linux-musl" ;;
                aarch64|arm64)       TARGET="aarch64-unknown-linux-musl" ;;
                armv7*|armv6*|arm)   TARGET="armv7-unknown-linux-gnueabihf" ;;
                *)
                    printf 'unsupported Linux machine: %s\n' "$MACHINE" >&2
                    printf '(supported: x86_64, aarch64/arm64, armv7)\n' >&2
                    exit 1 ;;
            esac ;;
        Darwin)
            case "$MACHINE" in
                arm64|aarch64)       TARGET="aarch64-apple-darwin" ;;
                x86_64)              TARGET="x86_64-apple-darwin" ;;
                *)
                    printf 'unsupported macOS machine: %s\n' "$MACHINE" >&2
                    exit 1 ;;
            esac ;;
        MINGW*|MSYS*|CYGWIN*|Windows*)
            case "$MACHINE" in
                x86_64|amd64)        TARGET="x86_64-pc-windows-gnu" ;;
                *)
                    printf 'unsupported Windows machine: %s\n' "$MACHINE" >&2
                    printf '(only x86_64 is supported under POSIX-sh; use restore.bat instead)\n' >&2
                    exit 1 ;;
            esac ;;
        *)
            printf 'unsupported OS: %s\n' "$OS" >&2
            printf '(supported: Linux, Darwin, MINGW/MSYS/CYGWIN/Windows)\n' >&2
            exit 1 ;;
    esac
fi

# Legacy single-axis $ARCH retained for callers reading it post-source.
ARCH="$TARGET"

# Cleanup hook for the temporary password file we may write below.
PWFILE_TMP=""
cleanup() {
    [ -n "$PWFILE_TMP" ] && [ -f "$PWFILE_TMP" ] && rm -f "$PWFILE_TMP"
}
trap cleanup EXIT INT TERM

# ── Repo discovery ────────────────────────────────────────────────
#
# A restic-format repo is a directory containing keys/ and index/
# subdirs.  The historical layout placed it directly at
# ${RECOVERY}/repo (used by restore_legacy.sh).  Modern LCSAS
# archives instead carry per-tenant repos under metadata/<tenant>/
# on every disc — the "holographic" layout where the meta disc and
# every data disc both ship the repo metadata for every backed-up
# tenant.  Probe both layouts, plus any currently-mounted disc.
#
# When multiple candidate repos are found (typical: a multi-tenant
# archive), the LCSAS_REPO environment variable selects one by
# tenant name.  If exactly one candidate exists we use it;
# otherwise we list and exit with a helpful hint.

REPO=""
REPO_CANDIDATES=""
add_repo_candidate() {
    # $1: path to inspect.  If it looks like a restic repo (keys/ +
    # index/), append it to REPO_CANDIDATES (newline-separated).
    [ -d "$1/keys" ] && [ -d "$1/index" ] || return 0
    REPO_CANDIDATES="$REPO_CANDIDATES
$1"
}

# Direct layouts (legacy).
add_repo_candidate "$RECOVERY/repo"
add_repo_candidate "$RECOVERY"
# Holographic layout — per-tenant under metadata/<name>/.
for cand in "$RECOVERY/metadata"/*; do
    [ -d "$cand" ] || continue
    add_repo_candidate "$cand"
done
# Any currently-mounted archive disc (the same directories the
# pack-search scan below uses).  We add the repos themselves here
# so a user can run `sh restore.sh` immediately after mounting a
# single data disc — no manual symlink dance required.
#
# The set of directories scanned is overridable via LCSAS_MOUNT_DIRS
# (colon-separated list), useful for tests and for unusual setups
# (e.g. systemd-mounted /run/media/$USER/).  Default mimics the
# /Volumes /media /mnt convention used elsewhere in the script.
LCSAS_MOUNT_DIRS_DEFAULT="/Volumes:/media/$(id -un 2>/dev/null):/media:/mnt:/run/media/$(id -un 2>/dev/null):/run/media"
LCSAS_MOUNT_DIRS_EFFECTIVE="${LCSAS_MOUNT_DIRS-$LCSAS_MOUNT_DIRS_DEFAULT}"
# Export so the tier-1 binary inherits the SAME list when it
# re-enumerates mount parents on every "press Enter to retry".  If
# the shell and the binary disagree here, a disc auto-mounted under
# (say) /run/media/$USER/ becomes invisible to the C-side locator
# even though the shell already found it.
export LCSAS_MOUNT_DIRS="$LCSAS_MOUNT_DIRS_EFFECTIVE"

# Opportunistic pack cache.  ON by default — without it the tier-1
# binary asks the operator to swap discs once per blob in the worst
# case (the v3 blind run took 16 swaps for 3 discs).  With it, the
# rest of each disc's data/ subtree is drained into a local cache
# on first contact; subsequent packs from the same disc resolve
# locally and the disc-swap count drops to O(N_discs).
#
# Three equivalent values:
#   LCSAS_PACK_CACHE_DIR=/abs/path   use that path (advanced)
#   LCSAS_PACK_CACHE_DIR=auto        auto-allocate under $TMPDIR
#   LCSAS_PACK_CACHE_DIR=            (empty) disable — disk-
#                                    constrained operators only
#
# Default when UNSET: auto.  Default when explicitly empty: off.
if [ "${LCSAS_PACK_CACHE_DIR-auto}" = "auto" ]; then
    LCSAS_PACK_CACHE_DIR="${TMPDIR:-/tmp}/lcsas-pack-cache.$$"
fi
export LCSAS_PACK_CACHE_DIR
OLD_IFS="$IFS"; IFS=":"
for parent in $LCSAS_MOUNT_DIRS_EFFECTIVE; do
    IFS="$OLD_IFS"
    [ -n "$parent" ] && [ -d "$parent" ] || continue
    # The mount point itself may directly contain metadata/* (when
    # /mnt is the disc root), so probe both /mnt itself and any
    # children of /mnt.
    for mnt in "$parent" "$parent"/*; do
        [ -d "$mnt" ] || continue
        for cand in "$mnt/metadata"/*; do
            [ -d "$cand" ] || continue
            add_repo_candidate "$cand"
        done
    done
    IFS=":"
done
IFS="$OLD_IFS"

# De-duplicate REPO_CANDIDATES preserving order, then pick one.
REPO_CANDIDATES="$(printf '%s\n' "$REPO_CANDIDATES" \
                  | awk 'NF && !seen[$0]++')"

# Count non-empty lines (printf "\n" | wc -l returns 1 — useless).
# `grep -c` exits 1 when nothing matches; under `set -e` that would
# kill the script, so wrap in `|| true` and let the count fall to 0.
REPO_COUNT="$(printf '%s\n' "$REPO_CANDIDATES" | grep -c '^.' || true)"
case "$REPO_COUNT" in
    0)
        cat >&2 <<EOF
no restic repo found.

The recovery script looked for a directory with keys/ and index/
subdirs under:

  - $RECOVERY/repo
  - $RECOVERY
  - $RECOVERY/metadata/*/    (the holographic layout LCSAS uses)
  - /mnt/metadata/*/         and any other currently-mounted disc

If you have a data disc, insert it and mount it (typically:
sudo mount /dev/sr0 /mnt), then re-run this script.
EOF
        exit 1
        ;;
    1)
        REPO="$(printf '%s\n' "$REPO_CANDIDATES" | head -n 1)"
        ;;
    *)
        # Multi-tenant archive.  Honour LCSAS_REPO if it matches a
        # candidate's basename (`alpha`, `bravo`, ...); otherwise
        # prompt the operator to pick one.
        REPO_NAMES=""
        for cand in $REPO_CANDIDATES; do
            REPO_NAMES="$REPO_NAMES $(basename "$cand")"
        done
        REPO_NAMES="${REPO_NAMES# }"
        if [ -n "${LCSAS_REPO:-}" ]; then
            for cand in $REPO_CANDIDATES; do
                base="$(basename "$cand")"
                if [ "$base" = "$LCSAS_REPO" ]; then
                    REPO="$cand"
                    break
                fi
            done
            if [ -z "$REPO" ]; then
                printf 'LCSAS_REPO=%s not among available repositories: %s\n' \
                       "$LCSAS_REPO" "$REPO_NAMES" >&2
                exit 1
            fi
        else
            printf 'Multiple repositories on this archive:\n' >&2
            repo_idx=0
            for cand in $REPO_CANDIDATES; do
                repo_idx=$((repo_idx + 1))
                printf '  %d) %s\n' "$repo_idx" "$(basename "$cand")" >&2
            done
            printf 'Choose a repository (number or name): ' >&2
            IFS= read -r repo_choice
            # Number form: positional-arg lookup avoids `eval` on input.
            case "$repo_choice" in
                ''|*[!0-9]*) ;;
                *)
                    # shellcheck disable=SC2086
                    set -- $REPO_CANDIDATES
                    if [ "$repo_choice" -ge 1 ] \
                       && [ "$repo_choice" -le $# ]; then
                        eval "REPO=\${$repo_choice}"
                    fi
                    ;;
            esac
            # Name form (fallback / legacy): match by basename.
            if [ -z "$REPO" ]; then
                for cand in $REPO_CANDIDATES; do
                    base="$(basename "$cand")"
                    if [ "$base" = "$repo_choice" ]; then
                        REPO="$cand"
                        break
                    fi
                done
            fi
            if [ -z "$REPO" ]; then
                printf 'no repository named %s; choose from: %s\n' \
                       "$repo_choice" "$REPO_NAMES" >&2
                exit 1
            fi
        fi
        ;;
esac
printf '[restore.sh] using repository %s\n' "$REPO" >&2

# ── Password file (now that we know which repo we're decrypting) ──

PWFILE="${LCSAS_PWFILE:-}"
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

mkdir -p "$TARGET_DIR"

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

# Walk LCSAS_MOUNT_DIRS_EFFECTIVE (same list the repo-discovery loop
# above honours) so tests and unusual setups can constrain the scan.
OLD_IFS="$IFS"; IFS=":"
for parent in $LCSAS_MOUNT_DIRS_EFFECTIVE; do
    IFS="$OLD_IFS"
    [ -n "$parent" ] && [ -d "$parent" ] || { IFS=":"; continue; }
    for mnt in "$parent"/*; do
        [ -d "$mnt" ] || continue
        # Don't list the recovery medium itself again.
        [ "$mnt" = "$RECOVERY" ] && continue
        add_pack_search "$mnt"
    done
    IFS=":"
done
IFS="$OLD_IFS"

# Hard-error when no data discs were discovered AND the resolved
# $REPO doesn't itself carry a data/ subdir (i.e. it's not a legacy
# self-contained repo).  Without this, the recovery binary will
# eventually fail with a less actionable "no packs found" message
# after the operator has already typed a password.
if [ -z "$PACK_SEARCH_ARGS" ] && [ ! -d "$REPO/data" ] \
   && [ "${LCSAS_ALLOW_NO_PACK_SEARCH:-0}" != "1" ]; then
    cat >&2 <<EOF
ERROR: no data discs detected at any of: $LCSAS_MOUNT_DIRS_EFFECTIVE
       The recovery binary will be unable to find any packs.

       Insert a data disc, mount it (typically:
         sudo mount /dev/sr0 /mnt
       ) and re-run this script.

       To suppress this check (advanced / scripted environments)
       set LCSAS_ALLOW_NO_PACK_SEARCH=1.
EOF
    exit 1
fi

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

# ── Session log helper ───────────────────────────────────────────
#
# Append one ISO-8601 UTC line to $HOME/.lcsas-restore-log so a
# repeat operator can see what worked last time.  Silently skipped
# when $HOME is unset/empty or not writable -- the log is a
# convenience, never a precondition for restore success.
#
# Disc count is best-effort: we count --pack-search dirs that
# existed at start time.  The tier-1 binary may handle additional
# swaps internally; that delta is not visible here.

session_disc_count() {
    # Count `--pack-search` flag tokens in $PACK_SEARCH_ARGS.  awk
    # avoids `set -- $PACK_SEARCH_ARGS`, which would clobber the
    # caller's positional parameters.
    printf '%s\n' "$PACK_SEARCH_ARGS" \
        | awk '{ for (i = 1; i <= NF; i++) if ($i == "--pack-search") n++ }
               END { print n + 0 }'
}

write_session_log() {
    # $1 = tier number (1|2|3).  All other context comes from the
    # script's environment ($REPO, $TARGET_DIR, $SNAP, etc.).
    _tier="$1"
    [ -n "${HOME:-}" ] || return 0
    [ -d "$HOME" ] || return 0
    [ -w "$HOME" ] || return 0
    _logfile="$HOME/.lcsas-restore-log"
    _ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || true)"
    [ -n "$_ts" ] || return 0
    _tenant="$(basename "$REPO" 2>/dev/null || printf 'unknown')"
    _discs="$(session_disc_count)"
    _line="$_ts  tenant=$_tenant  target=$TARGET_DIR  snapshot=$SNAP"
    _line="$_line  tier=$_tier  discs=$_discs"
    printf '%s\n' "$_line" >> "$_logfile" 2>/dev/null || true
}

# ── Tier dispatch ─────────────────────────────────────────────────
#
# Each tier is tried in priority order.  The default behavior is to
# `exec` the first available binary — once tier 1 starts, the shell
# is gone and there is no fall-through if tier 1 crashes mid-restore.
# That matches the bare-minimum recovery story: tier 1 IS the
# recovery, and it has to work.
#
# Opt-in fallback (LCSAS_TIER_FALLBACK=1): run the tier as a
# subprocess, capture exit code, and fall through to the next tier
# on non-zero.  Useful when the operator suspects a bug in a higher
# tier and wants the script to walk the cascade for them.  Tier 3 is
# always `exec`'d (it's the last resort — nothing to fall back to).

RESTORE_BIN="$RECOVERY/bin/$TARGET/lcsas-restore"
RUSTIC_BIN="$RECOVERY/bin/$TARGET/rustic-static"
FALLBACK="${LCSAS_TIER_FALLBACK:-0}"

# ── Tier 1: prebuilt lcsas-restore (C89, static, no Python) ───────

if [ -x "$RESTORE_BIN" ]; then
    printf '[tier 1] using prebuilt lcsas-restore (%s)\n' "$TARGET" >&2
    # Drop cwd outside the meta-disc before exec, so the kernel does
    # not hold it through the exec barrier.
    [ -n "${META_DISC:-}" ] && cd / 2>/dev/null || true
    if [ "$FALLBACK" = "1" ]; then
        # `set -e` would kill the script on any tier-1 non-zero; the
        # `|| true` lets us capture $? and decide whether to advance.
        tier1_rc=0
        "$RESTORE_BIN" --repo "$REPO" --password-file "$PWFILE" \
                       --target "$TARGET_DIR" --snapshot "$SNAP" \
                       $PACK_SEARCH_ARGS $CATALOG_ARG $META_DISC_ARG \
                       || tier1_rc=$?
        if [ $tier1_rc -eq 0 ]; then write_session_log 1; exit 0; fi
        printf '[tier 1] exited %d, falling through to tier 2\n' \
               $tier1_rc >&2
    else
        # We're about to exec -- write the session log anticipatorily.
        # If the binary later crashes mid-restore the log line is a
        # slight lie, but the alternative (no log on the default code
        # path) is worse for the second-time operator UX.
        write_session_log 1
        exec "$RESTORE_BIN" --repo "$REPO" --password-file "$PWFILE" \
                       --target "$TARGET_DIR" --snapshot "$SNAP" \
                       $PACK_SEARCH_ARGS $CATALOG_ARG $META_DISC_ARG
    fi
fi

# ── Tier 2: vendored rustic-static (no Python) ────────────────────

if [ -x "$RUSTIC_BIN" ]; then
    printf '[tier 2] using vendored rustic-static (%s)\n' "$TARGET" >&2
    if [ "$FALLBACK" = "1" ]; then
        tier2_rc=0
        "$RUSTIC_BIN" --repository "$REPO" --password-file "$PWFILE" \
                     restore "$SNAP" "$TARGET_DIR" \
                     || tier2_rc=$?
        if [ $tier2_rc -eq 0 ]; then write_session_log 2; exit 0; fi
        printf '[tier 2] exited %d, falling through to tier 3\n' \
               $tier2_rc >&2
    else
        write_session_log 2
        exec "$RUSTIC_BIN" --repository "$REPO" --password-file "$PWFILE" \
                     restore "$SNAP" "$TARGET_DIR"
    fi
fi

# ─────────────────────────────────────────────────────────────────
# BARE-MINIMUM PATH ENDS HERE.  Everything above is statically-linked
# C/Rust + POSIX sh, with NO Python dependency at any step.
# ─────────────────────────────────────────────────────────────────

# ── Tier 3: Python fallback (LAST RESORT, off the bare path) ─────

if [ "${LCSAS_ALLOW_PYTHON_TIER:-1}" = "1" ]; then
    PYBIN=""
    # Prefer the per-target bundled CPython (from python-build-standalone)
    # over whatever happens to be on $PATH.  This keeps tier 3 working on
    # hosts that don't have python3 packaged.
    for cand in \
        "$RECOVERY/bin/$TARGET/python/bin/python3" \
        "$RECOVERY/bin/$TARGET/python/python.exe" \
        "$RECOVERY/bin/$TARGET/python/bin/python"; do
        if [ -x "$cand" ]; then PYBIN="$cand"; break; fi
    done
    if [ -z "$PYBIN" ]; then
        for p in python3 python; do
            if command -v "$p" >/dev/null 2>&1; then PYBIN="$p"; break; fi
        done
    fi
    PYREST=""
    for cand in "$RECOVERY/../standalone_restorer.py" \
                "$RECOVERY/standalone_restorer.py" \
                "$SCRIPT_DIR/../standalone_restorer.py"; do
        if [ -f "$cand" ]; then PYREST="$cand"; break; fi
    done
    if [ -n "$PYBIN" ] && [ -n "$PYREST" ]; then
        printf '[tier 3] falling back to Python (%s + %s)\n' \
               "$PYBIN" "$PYREST" >&2
        # standalone_restorer.py CLI is flag-based:
        #   --repo DIR --password-file FILE --target DIR [--snapshot ID]
        # See src/lcsas/restore/standalone_builder.py:_cli_main.
        # The non-"latest" sentinel is passed straight through.
        TIER3_SNAP_ARGS=""
        if [ -n "$SNAP" ] && [ "$SNAP" != "latest" ]; then
            TIER3_SNAP_ARGS="--snapshot $SNAP"
        fi
        write_session_log 3
        exec "$PYBIN" "$PYREST" \
             --repo "$REPO" \
             --password-file "$PWFILE" \
             --target "$TARGET_DIR" \
             $TIER3_SNAP_ARGS
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
