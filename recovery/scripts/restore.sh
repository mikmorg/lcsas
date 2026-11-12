#!/bin/sh
# restore.sh -- POSIX-sh driver for the LCSAS recovery cascade.
#
# Bare-minimum restore is prebuilt static binaries + POSIX sh ONLY.
# Python is NOT on the bare path; it lives on a separate tier (3) that
# is only reached when every C-based option has failed.
#
# Cascade (bare-minimum path = tiers 1-2):
#
#   Tier 1.  bin/<arch>/lcsas-restore        prebuilt static C89 binary
#   Tier 2.  bin/<arch>/rustic-static        vendored Rust binary (cross-check)
#   Tier 2b. bin/<arch>/restic (or PATH)     stock restic/rustic — independent
#                                            audited reader of the same format
#   ----- Bare minimum stops here.  None of the above needs Python. -----
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
#   $LCSAS_SHARES_DIR -- if set, a folder of share card files: the password
#                       is rebuilt from them (after the two above)
# With none of these, the script asks: for the share cards when the set's
# marker (metadata/<id>/LCSAS_REPO.txt) says its password is split, else
# for the password, again after a wrong one.
#
# Documented exit codes:
#   0    success
#   1    generic failure (no repository, unsupported OS/arch, etc.)
#   2    usage error
#   64   no recovery binary available at any tier (issue #225)
#   65   the reader stopped at a question this script asked
#   75   the reader typed q at the disc prompt of tier 1 (#615) or tier 3
#   77   a repository key rejected the password, or the reader stopped at
#        the password or share card prompt
#   130  stopped with Ctrl-C (143: killed with TERM)
#
# Tools folder (#609): run off a disc, the script copies itself, this
# computer's binaries, the docs and the meta disc's metadata/ into
# $LCSAS_TOOLS_DIR (default $HOME/lcsas-restore-tools) and runs from
# there.  The pack cache, a copy of the catalog and resume.sh live
# there too.  It is removed after a restore that succeeds and kept
# after any other exit, so `sh <tools>/resume.sh` can carry on.

set -eu

# Test-infrastructure hook (issue #213): when `LCSAS_SHELL_TRACE` is
# set to a writable path, the script emits a `bash -x`-style trace
# of every executed line to that file.  Honoured only when bash is
# the interpreter (BASH_XTRACEFD is bash-specific); dash/POSIX-sh
# ignore the hook and run normally.  No production-path effect.
# The BASH_VERSION check is what makes "ignore" true (#569): the copy
# relocated to RAM re-runs under /bin/sh with the variable still set,
# and dash has no LINENO, so PS4's $LINENO failed `set -u` there.
if [ -n "${LCSAS_SHELL_TRACE:-}" ] && [ -n "${BASH_VERSION:-}" ]; then
    # 9 is a high enough fd to avoid clashing with stdin/stdout/stderr
    # or any of the script's later `exec N>...` redirections.
    exec 9>>"$LCSAS_SHELL_TRACE"
    # shellcheck disable=SC2154,SC3028
    BASH_XTRACEFD=9
    # shellcheck disable=SC3043
    PS4='+ $LINENO '
    set -x
fi

# Stamped at meta disc build time by src/lcsas/meta/builder.py.
LCSAS_RESTORE_BUILD_SHA="@@BUILD_SHA@@"
LCSAS_RESTORE_BUILD_DATE="@@BUILD_DATE@@"

# ── Argument handling and auto-discovery ──────────────────────────

SCRIPT="$(
    # POSIX-portable realpath approximation.
    cd "$(dirname "$0")" 2>/dev/null && pwd -P
)/$(basename "$0")"
SCRIPT_DIR="$(dirname "$SCRIPT")"

# ── Single-drive guard: relocate to RAM before going further ──────
#
# If this script is being interpreted off a read-only optical disc
# (the meta disc), `sh` keeps an open file descriptor on it and the
# heir cannot eject -- which is fatal for anybody with only ONE
# optical drive.  Detect that case and re-exec ourselves from a
# writable directory after copying the script + the chosen binary
# tier into RAM (or any host writable dir).  Subsequent disc swaps
# then operate freely.
#
# LCSAS_RELOCATED is the sentinel: when set, we are the in-RAM copy
# and its value is the path of the original meta disc mount.

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

# Print this host's rust triple (the bin/<triple>/ dir restore.sh runs
# from), or nothing when the platform isn't one we ship.  The same
# matrix as the main dispatch below, minus its error exits: relocation
# only uses it to copy less.
_host_triple() {
    _ht_m="$(uname -m 2>/dev/null)"
    case "$(uname -s 2>/dev/null)" in
        Linux)
            case "$_ht_m" in
                x86_64|amd64)      echo x86_64-unknown-linux-musl ;;
                aarch64|arm64)     echo aarch64-unknown-linux-musl ;;
                armv7*|armv6*|arm) echo armv7-unknown-linux-gnueabihf ;;
            esac ;;
        Darwin)
            case "$_ht_m" in
                arm64|aarch64)     echo aarch64-apple-darwin ;;
                x86_64)            echo x86_64-apple-darwin ;;
            esac ;;
    esac
}

# target_fstype DIR: the FSTYPE of the filesystem holding DIR; empty when
# undetectable.  Never returns non-zero (set -e safety).
# LCSAS_PROC_MOUNTS (test seam) points the fallback parser at an
# alternate mount table AND bypasses findmnt so the awk path is
# exercisable on hosts that have findmnt.  Used for the restore target
# (BOOT-05) and for the tools folder (a live USB's $HOME is RAM).
target_fstype() {
    if [ -z "${LCSAS_PROC_MOUNTS:-}" ] && command -v findmnt >/dev/null 2>&1; then
        findmnt -n -o FSTYPE --target "$1" 2>/dev/null || true
        return 0
    fi
    _tft_table="${LCSAS_PROC_MOUNTS:-/proc/mounts}"
    [ -r "$_tft_table" ] || return 0
    # Longest-prefix match of the target's canonical path against the
    # mount-point column; same-length later entries win (overmounts
    # append to the table).
    awk -v p="$(cd "$1" 2>/dev/null && pwd -P)" '
        {
            pre = ($2 == "/") ? "/" : $2 "/"
            if (p != "" && (p == $2 || index(p, pre) == 1) &&
                length($2) >= l) { l = length($2); t = $3 }
        }
        END { print t }' "$_tft_table" 2>/dev/null || true
    return 0
}

human_kb() {
    # KB count -> "N.N GB" / "N MB" (GB collapses to 0.0 below 1 GB).
    awk -v kb="$1" 'BEGIN {
        if (kb >= 1048576) printf "%.1f GB", kb / 1048576;
        else printf "%d MB", (kb + 1023) / 1024;
    }'
}

# make_writable PATH: give the owner write permission on PATH and all
# under it.  A disc is mastered read-only (xorriso -as mkisofs -r: every
# folder r-x, every file r--) and cp -R keeps those modes, so a copy
# made from a disc could be neither replaced nor removed.  Every copy
# into the tools folder goes through this.
make_writable() {
    if [ -e "$1" ] && [ ! -L "$1" ]; then
        chmod -R u+w "$1" 2>/dev/null || true
    fi
    return 0
}

# rm_tree PATH...: remove copies this script made, read-only or not.
# 1 when one of them could not be removed.
rm_tree() {
    _rt_rc=0
    for _rt in "$@"; do
        if [ -e "$_rt" ] || [ -L "$_rt" ]; then
            make_writable "$_rt"
            rm -rf "$_rt" 2>/dev/null || _rt_rc=1
        fi
    done
    return "$_rt_rc"
}

# path_inside A B: 0 when the folder A is the folder B or is under it
# (both must exist; compared by their real paths).
path_inside() {
    _pi_a="$(cd "$1" 2>/dev/null && pwd -P)" || return 1
    _pi_b="$(cd "$2" 2>/dev/null && pwd -P)" || return 1
    if [ -z "$_pi_a" ] || [ -z "$_pi_b" ]; then return 1; fi
    case "$_pi_a/" in "${_pi_b%/}"/*) return 0 ;; esac
    return 1
}

# sh_quote S: S as one single-quoted shell word.
sh_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# sh_word S: S as a shell word to copy and paste: as it is when the shell
# would neither split nor expand it, else single-quoted.  For the
# commands this script prints, such as the resume command.
# shellcheck disable=SC2317  # runs from the EXIT trap
sh_word() {
    case "$1" in
        ''|*[!A-Za-z0-9_./+,:@%=-]*) sh_quote "$1" ;;
        *) printf '%s' "$1" ;;
    esac
}

# ── The tools folder (C6, #609) ───────────────────────────────────
#
# One fixed folder per user holds everything a restore needs once the
# meta disc is out of the drive.  A marker file says a folder is ours:
# the script only ever empties or deletes a folder that carries it.
TOOLS_MARK=".lcsas-restore-tools"

# tools_dir_claim DIR: make DIR a tools folder.  0 = DIR now carries the
# marker.  An existing folder is claimed only when it is empty or
# already carries the marker, so a folder holding the reader's own files
# (LCSAS_TOOLS_DIR=$HOME, say) is never used and never deleted.
tools_dir_claim() {
    if [ -d "$1" ]; then
        [ -f "$1/$TOOLS_MARK" ] && return 0
        [ -z "$(ls -A "$1" 2>/dev/null)" ] || return 1
    else
        mkdir -p "$1" 2>/dev/null || return 1
    fi
    : > "$1/$TOOLS_MARK" 2>/dev/null || return 1
    return 0
}

# dir_runs_programs DIR: 0 when a program in DIR can be run (#496: a
# "noexec" mount refuses them).
dir_runs_programs() {
    printf '#!/bin/sh\nexit 0\n' > "$1/.exec-probe" 2>/dev/null || return 1
    chmod +x "$1/.exec-probe" 2>/dev/null || true
    if "$1/.exec-probe" 2>/dev/null; then
        rm -f "$1/.exec-probe"
        return 0
    fi
    rm -f "$1/.exec-probe"
    return 1
}

# room_for DIR KB: 0 unless df says DIR has less than KB free; then it
# says so and returns 1.  Unknown sizes count as room.
room_for() {
    _free_kb="$(df -Pk "$1" 2>/dev/null | awk 'NR==2 { print $4 }')"
    case "$2:$_free_kb" in
        *[!0-9:]*|:*|*:) return 0 ;;
    esac
    [ "$_free_kb" -lt "$2" ] || return 0
    printf '[lcsas-restore] %s has %s KB free, the recovery ' "$1" "$_free_kb" >&2
    printf 'binaries need %s KB; trying the next folder\n' "$2" >&2
    return 1
}

# What a tools folder holds besides the marker, all of it made by this
# script: .lcsas-build is the build line of the restore.sh that filled
# it, .lcsas-sets the ids of the META disc's backup sets it was filled
# for, .lcsas-copy-ok says the copy off the disc finished.
TOOLS_FILES="recovery metadata catalog.db pack-cache resume.sh standalone_restorer.py tools .lcsas-build .lcsas-sets .lcsas-copy-ok .exec-probe"

build_line() {
    grep '^LCSAS_RESTORE_BUILD_SHA=' "$1" 2>/dev/null | sed -n 1p
    return 0
}

# tools_dir_build DIR: the build line of the restore.sh that filled DIR.
tools_dir_build() {
    if [ -f "$1/.lcsas-build" ]; then
        sed -n 1p "$1/.lcsas-build"
    elif [ -f "$1/recovery/scripts/restore.sh" ]; then
        build_line "$1/recovery/scripts/restore.sh"
    fi
    return 0
}

# tools_dir_replace DIR REASON KEEP_CACHE: DIR holds what ANOTHER run
# left (another build, a copy that did not finish, another archive's
# sets).  Remove all of it but the marker; keep the pack cache only when
# KEEP_CACHE is 1 (packs are named by their hash, so the same build's
# cache is still good).
tools_dir_replace() {
    _tr_any=0
    _tr_ifs="$IFS"; IFS=' '
    for _tr in $TOOLS_FILES; do
        if [ -e "$1/$_tr" ]; then _tr_any=1; fi
    done
    if [ "$_tr_any" = 1 ]; then
        printf '[lcsas-restore] %s holds %s; replacing it\n' "$1" "$2" >&2
        for _tr in $TOOLS_FILES; do
            [ "$_tr" != pack-cache ] || [ "$3" != 1 ] || continue
            rm_tree "$1/$_tr" || true
        done
    fi
    IFS="$_tr_ifs"
    return 0
}

# tools_dir_remove DIR: the restore is done; remove what this script put
# in DIR, then DIR itself.  Never a blanket rm -rf: anything else that
# came to be in the folder stays, and so does the folder.
# shellcheck disable=SC2317  # runs from the EXIT trap
tools_dir_remove() {
    _tr_ifs="$IFS"; IFS=' '
    for _tr in $TOOLS_FILES; do
        rm_tree "$1/$_tr" || true
    done
    IFS="$_tr_ifs"
    rm -f "$1/$TOOLS_MARK" 2>/dev/null || true
    if [ -d "$1" ] && ! rmdir "$1" 2>/dev/null \
       && [ -n "$(ls -A "$1" 2>/dev/null)" ]; then
        printf '[restore.sh] %s holds other files, so the folder is kept\n' "$1" >&2
    fi
    return 0
}

# meta_sets MD: the ids of the backup sets in the metadata folder MD,
# one a line.
meta_sets() {
    for _ms in "$1"/*; do
        if [ -d "$_ms/keys" ]; then printf '%s\n' "${_ms##*/}"; fi
    done
    return 0
}

# tools_other_archive DIR MD: 0 when the tools folder DIR was filled for
# ANOTHER archive: none of the backup sets in the META disc's metadata
# folder MD is among the META disc sets DIR was filled for (.lcsas-sets).
# Set ids are unique, so a newer or older META disc of the same archive
# always shares one.  A folder with no .lcsas-sets says nothing.
tools_other_archive() {
    [ -s "$1/.lcsas-sets" ] || return 1
    _toa_any=0
    for _toa in "$2"/*; do
        [ -d "$_toa/keys" ] || continue
        _toa_any=1
        if grep -qxF -- "${_toa##*/}" "$1/.lcsas-sets" 2>/dev/null; then return 1; fi
    done
    [ "$_toa_any" = 1 ]
}

# copy_tree SRC DST: copy the folder SRC to DST whole or not at all: into
# a hidden .<name>.new beside DST, swapped in only when every file was
# copied.  1, with DST as it was, when the copy failed (a scratched
# disc, a full disk, a disc taken out).
copy_tree() {
    _ct_new="${2%/*}/.${2##*/}.new"
    rm_tree "$_ct_new" || true
    if cp -R "$1" "$_ct_new" 2>/dev/null; then
        make_writable "$_ct_new"
        rm_tree "$2" || true
        if mv "$_ct_new" "$2" 2>/dev/null; then return 0; fi
    fi
    rm_tree "$_ct_new" || true
    return 1
}

relocate_to_ram() {
    # $1 = original meta disc mount root.
    orig_mount="$1"; shift
    # Preserve the bin/ tree so tier-1/tier-2 still resolve.  The
    # script lives at one of two valid locations on the meta disc:
    #   (a) $META/restore.sh                — top-level entry; the
    #       sibling tree is $META/recovery/bin/
    #   (b) $META/recovery/scripts/restore.sh — canonical RECOVERY
    #       layout; the sibling tree is $META/recovery/bin/ (i.e.
    #       one level up from $SCRIPT_DIR)
    # In case (a), $SCRIPT_DIR/../bin resolves to /bin (the HOST's
    # /bin -- the original blind-restore regression).  Try both and
    # pick the one that actually carries the recovery binaries.
    src_bin=""
    if [ -d "$SCRIPT_DIR/recovery/bin" ]; then
        src_bin="$SCRIPT_DIR/recovery/bin"
    elif [ -d "$SCRIPT_DIR/../bin" ]; then
        # Cheap sanity check: the recovery bin/ has per-target
        # subdirs (e.g. x86_64-unknown-linux-musl/).  Host /bin
        # does not.  If neither pattern is present, skip the copy
        # so the heir gets the actionable "no recovery method
        # available" message later rather than a silent flat copy
        # of /bin into the ramdir.
        if ls -d "$SCRIPT_DIR/../bin"/*-*-* >/dev/null 2>&1 \
           || ls -d "$SCRIPT_DIR/../bin"/x86_64* "$SCRIPT_DIR/../bin"/aarch64* \
                    "$SCRIPT_DIR/../bin"/armv7* >/dev/null 2>&1; then
            src_bin="$SCRIPT_DIR/../bin"
        fi
    fi
    # Copy only this host's bin/<triple>/ (#508): restore.sh never reads
    # another target's dir, and the whole tree is ~750 MB against ~130 MB
    # for one target.  Unknown platform or no such dir: copy everything.
    _copy_from="$src_bin"
    _copy_to="bin"
    _reloc_triple="${LCSAS_TARGET:-$(_host_triple)}"
    if [ -n "$src_bin" ] && [ -n "$_reloc_triple" ] \
       && [ -d "$src_bin/$_reloc_triple" ]; then
        _copy_from="$src_bin/$_reloc_triple"
        _copy_to="bin/$_reloc_triple"
    fi
    _bin_kb=""
    [ -n "$src_bin" ] && _bin_kb="$(du -sk "$_copy_from" 2>/dev/null | awk '{print $1}')"
    # The meta disc's own metadata/ (each backup set's keys, index and
    # snapshots) is at the disc ROOT, not under recovery/.
    case "$SCRIPT_DIR" in
        */recovery/scripts) _meta_md="${SCRIPT_DIR%/recovery/scripts}/metadata" ;;
        *)                  _meta_md="$SCRIPT_DIR/metadata" ;;
    esac
    # Where the copy goes (#609).  First the fixed tools folder, so a
    # stopped restore can carry on from it and a re-run reuses it: when
    # it already holds a copy made by this same restore.sh, nothing is
    # copied again.  When it cannot be used, a temporary folder, as
    # before: not inside the meta disc, with room for the bin/ tree, and
    # allowed to run programs (#496).  A small tmpfs (/run/user/<uid> is
    # 10% of RAM by default) used to take a partial copy silently, and a
    # "noexec" /tmp (common on hardened hosts) made every relocated tier
    # fail with "Permission denied".  The copy itself stays best-effort:
    # one unreadable file on a scratched disc must not block relocation.
    ramdir=""
    _reuse=0
    _td="${LCSAS_TOOLS_DIR:-${HOME:+$HOME/lcsas-restore-tools}}"
    case "$_td" in ''|/*) ;; *) _td="$(pwd)/$_td" ;; esac
    # Reused only when an earlier run copied ALL of it off a disc of this
    # same build and archive: the copy finished (.lcsas-copy-ok), its
    # restore.sh and tier-1 binary match the disc's byte for byte, and
    # its sets are this META disc's.  A copy that stopped part-way (a
    # scratched disc, a full disk, Ctrl-C) is made again, never trusted.
    _td_why="an older copy of the recovery tools"
    _td_keep=0
    if [ -n "$_td" ] && [ -f "$_td/$TOOLS_MARK" ]; then
        if tools_other_archive "$_td" "$_meta_md"; then
            _td_why="the copies of another archive's backup sets"
        elif [ "$(tools_dir_build "$_td")" = "$(build_line "$SCRIPT")" ]; then
            _td_keep=1
            if ! cmp -s "$SCRIPT" "$_td/recovery/scripts/restore.sh"; then
                :   # another restore.sh: an older copy
            elif [ -f "$_td/.lcsas-copy-ok" ] && [ -d "$_td/recovery/$_copy_to" ] \
                 && { [ ! -f "$_copy_from/lcsas-restore" ] \
                      || cmp -s "$_copy_from/lcsas-restore" \
                                "$_td/recovery/$_copy_to/lcsas-restore"; }; then
                _reuse=1
                ramdir="$_td"
            else
                _td_why="a copy of the recovery tools that is not complete"
            fi
        fi
    fi
    if [ "$_reuse" = 0 ] && [ -n "$_td" ]; then
        _td_probe="$_td"
        [ -d "$_td_probe" ] || _td_probe="$(dirname "$_td")"
        if room_for "$_td_probe" "$_bin_kb" && tools_dir_claim "$_td" \
           && dir_runs_programs "$_td"; then
            tools_dir_replace "$_td" "$_td_why" "$_td_keep"
            ramdir="$_td"
        else
            printf '[lcsas-restore] cannot use %s for the recovery tools; ' "$_td" >&2
            printf 'trying a temporary folder\n' >&2
        fi
    fi
    for cand in "${TMPDIR:-}" "${XDG_RUNTIME_DIR:-}" /tmp /run /var/tmp \
                "${HOME:-}"; do
        [ -z "$ramdir" ] || break
        [ -n "$cand" ] || continue
        [ -d "$cand" ] || continue
        [ -w "$cand" ] || continue
        case "$cand" in "$orig_mount"|"$orig_mount"/*) continue;; esac
        room_for "$cand" "$_bin_kb" || continue
        ramdir="$(mktemp -d "$cand/lcsas-restore.XXXXXX" 2>/dev/null || true)"
        if [ -z "$ramdir" ] || [ ! -d "$ramdir" ]; then
            ramdir=""
            continue
        fi
        if ! dir_runs_programs "$ramdir"; then
            rm -rf "$ramdir"
            ramdir=""
            continue
        fi
        # The temporary folder is this run's tools folder: kept for a
        # resume until the computer is turned off, removed on success.
        : > "$ramdir/$TOOLS_MARK"
    done
    unset _free_kb _reloc_triple _td _td_probe _td_why _td_keep
    _copy_ok=1
    if [ -z "$ramdir" ]; then
        printf '[lcsas-restore] cannot find a writable dir to relocate to; ' >&2
        printf 'continuing from %s (drive will be held)\n' "$orig_mount" >&2
        return 1
    fi

    if [ "$_reuse" = 1 ]; then
        printf '[lcsas-restore] copied recovery binaries to %s on an earlier run; ' "$ramdir" >&2
        printf 'using that copy\n' >&2
    else
        # Mirror the on-disc layout under $ramdir so the script's own
        # AUTO_RECOVERY logic resolves $ramdir/recovery as RECOVERY.
        rm -f "$ramdir/.lcsas-copy-ok"
        mkdir -p "$ramdir/recovery/scripts" "$ramdir/recovery/$_copy_to"
        if [ -n "$src_bin" ]; then
            # #612: say what is happening BEFORE a copy that takes minutes
            # on a slow drive; it used to run in silence.
            printf '[lcsas-restore] copying the recovery tools for this computer (about %s)\n' \
                   "$(human_kb "${_bin_kb:-0}")" >&2
            printf '                from the disc to %s; on a slow drive this takes a few minutes...\n' \
                   "$ramdir" >&2
            cp -R "$_copy_from/." "$ramdir/recovery/$_copy_to/" 2>/dev/null \
                || _copy_ok=0
            # A copy of the disc on a FAT/exFAT stick (or a noexec mount) can
            # leave the binaries without their executable bits (#496).
            chmod -R u+x "$ramdir/recovery/bin" 2>/dev/null || true
            # The restore manuals travel with the tools (C6).
            if [ -d "$src_bin/../docs" ]; then
                cp -R "$src_bin/../docs" "$ramdir/recovery/" 2>/dev/null || _copy_ok=0
            fi
        fi
        cp -f "$SCRIPT" "$ramdir/recovery/scripts/restore.sh"
        chmod +x "$ramdir/recovery/scripts/restore.sh"
        if [ -f "$SCRIPT_DIR/detect_arch.sh" ]; then
            cp -f "$SCRIPT_DIR/detect_arch.sh" "$ramdir/recovery/scripts/detect_arch.sh"
            chmod +x "$ramdir/recovery/scripts/detect_arch.sh"
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
        # Issue #234 — copy standalone_restorer.py for tier 3 too.  Without
        # this, post-relocation the tier-3 fall-through can't find the
        # script (the orig location was under the now-ejected meta disc).
        # The tier-3 search looks at $RECOVERY/../standalone_restorer.py
        # (which resolves to $ramdir/standalone_restorer.py post-reloc).
        for pyrest_cand in \
            "$SCRIPT_DIR/../standalone_restorer.py" \
            "$SCRIPT_DIR/standalone_restorer.py" \
            "$SCRIPT_DIR/../../standalone_restorer.py"
        do
            [ -f "$pyrest_cand" ] || continue
            cp -f "$pyrest_cand" "$ramdir/standalone_restorer.py" 2>/dev/null || true
            break
        done

        # Issue #284 — copy the bundled zstandard package so tier-3 PYTHONPATH
        # resolution works post-relocation.  PR #239 wired PYTHONPATH to
        # $META_ROOT/tools/lib/python*/zstandard/ but that path disappears
        # when the meta disc is ejected.  Mirror the directory into the RAM
        # dir so the existing PYTHONPATH search finds it at
        # $ramdir/tools/lib/python*/zstandard/.
        for _zstd_src in \
            "$SCRIPT_DIR/../../tools/lib/python"*/zstandard \
            "$SCRIPT_DIR/tools/lib/python"*/zstandard
        do
            [ -d "$_zstd_src" ] || continue
            _zstd_pyver="$(basename "$(dirname "$_zstd_src")")"
            mkdir -p "$ramdir/tools/lib/$_zstd_pyver"
            cp -R "$_zstd_src" "$ramdir/tools/lib/$_zstd_pyver/zstandard" \
                2>/dev/null || _copy_ok=0
            break
        done
        unset _zstd_src _zstd_pyver

        printf '[lcsas-restore] copied recovery binaries to %s\n' "$ramdir" >&2
    fi

    # Copy the meta disc's metadata/ so the restore can find the sets once
    # the disc is out (#616).  Each set is copied whole or not at all
    # (copy_tree).  A reused copy keeps the sets an earlier run brought up
    # to date from a data disc (tagged in metadata/.from/); every other
    # set is taken from this META disc again.
    if [ -d "$_meta_md" ]; then
        if [ "$_reuse" = 0 ]; then
            rm_tree "$ramdir/metadata" || true
            _meta_kb="$(du -sk "$_meta_md" 2>/dev/null | awk '{print $1}')"
            printf '[lcsas-restore] copying the backup-set metadata (about %s) to %s/metadata...\n' \
                   "$(human_kb "${_meta_kb:-0}")" "$ramdir" >&2
        fi
        mkdir -p "$ramdir/metadata"
        for _meta_set in "$_meta_md"/*; do
            [ -d "$_meta_set" ] || continue
            if [ -f "$ramdir/metadata/.from/${_meta_set##*/}" ] \
               && [ -d "$ramdir/metadata/${_meta_set##*/}" ]; then
                continue
            fi
            copy_tree "$_meta_set" "$ramdir/metadata/${_meta_set##*/}" || _copy_ok=0
        done
    fi
    # Everything copied off the disc can be replaced and removed later.
    make_writable "$ramdir"
    if [ "$_reuse" = 0 ]; then
        build_line "$SCRIPT" > "$ramdir/.lcsas-build"
        meta_sets "$_meta_md" > "$ramdir/.lcsas-sets"
        if [ "$_copy_ok" = 1 ]; then
            : > "$ramdir/.lcsas-copy-ok"
        else
            printf '[lcsas-restore] some files could not be copied from the disc; this\n' >&2
            printf '                copy is used for this run, and copied again next time.\n' >&2
        fi
    fi
    unset _bin_kb _meta_md _meta_kb _meta_set _reuse _copy_ok

    # A live USB stick keeps $HOME (and /tmp) in RAM.
    case "$(target_fstype "$ramdir")" in
        tmpfs|ramfs)
            cat >&2 <<EOF
[lcsas-restore] note: $ramdir is in RAM (normal on a live USB stick).
                It is lost when this computer is turned off, so a
                stopped restore can be carried on only until then.
EOF
            ;;
    esac
    printf '[lcsas-restore] you may eject the meta disc when the ' >&2
    printf 'binary prompts for a data disc.\n' >&2

    LCSAS_RELOCATED="$orig_mount"
    LCSAS_TOOLS_DIR="$ramdir"
    export LCSAS_RELOCATED LCSAS_TOOLS_DIR
    # cd outside the meta disc so the new sh inherits a safe cwd.
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
    # explicitly named the meta disc and we are running inside it.
    if [ -n "${LCSAS_META_DISC:-}" ]; then
        case "$SCRIPT_DIR" in
            "$LCSAS_META_DISC"|"$LCSAS_META_DISC"/*) relocate_needed=1 ;;
        esac
    fi
    # Fallback: probe for read-only / optical filesystem types.
    if [ "$relocate_needed" = "0" ] && [ ! -w "$SCRIPT_DIR" ]; then
        relocate_needed=1
    fi
    # #496: tier binaries that exist but can't be run from here (a
    # FAT/exFAT copy has no executable bits; a noexec mount refuses
    # them).  The relocated copy gets its bits back and an exec-able home.
    if [ "$relocate_needed" = "0" ]; then
        for _nx_bin in "$SCRIPT_DIR"/recovery/bin/*/lcsas-restore \
                       "$SCRIPT_DIR"/../bin/*/lcsas-restore; do
            if [ -s "$_nx_bin" ] && [ ! -x "$_nx_bin" ]; then
                relocate_needed=1
                break
            fi
        done
        unset _nx_bin
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

# Record the meta disc mount so we can pass it on to lcsas-restore.
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
TARGET_FLAG=""
TARGET_FROM_POS=0
SNAP="latest"
# --check-disc IMAGE: the heir-facing "my disc is scratched" path.  When
# set, restore.sh runs the bundled in-house RS03 ECC tool (lcsas-ecc) on
# IMAGE to check and, on damage, offer/perform a repair -- then exits.
# No password, no repository discovery, no restore.  [FMT-01]
CHECK_DISC_IMG=""

# Flag parsing: strip named flags before positional-arg parsing so
# heirs can write `sh restore.sh --repo alpha RECOVERY TARGET` or
# `sh restore.sh --help` without knowing about LCSAS_* env vars.
# The `*) break ;;` stops the loop at the first non-flag argument so
# the existing positional-arg if/elif block below still works unchanged.
# Unknown `-*' flags are REJECTED loudly (exit 2): the burned-disc heir
# docs reference these flags verbatim, and a silently-misparsed flag
# used to become an uninterpretable snapshot-not-found failure [KEY-02].
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat <<EOF
usage: $0 [--repo NAME] [--target DIR] [--key FILE] [--version]
         [--check-disc IMAGE] [RECOVERY_ROOT] [TARGET_DIR] [SNAPSHOT_ID|latest]

SCRATCHED / DAMAGED DISC?
  sh restore.sh --check-disc /path/to/disc.iso
  Runs the bundled in-house RS03 ECC tool (lcsas-ecc) to scan the disc
  image for damage and, if any is found, offers to REPAIR it using the
  ~15%% parity that was burned alongside your data.  No externally
  installed dvdisaster or ddrescue is required -- the repair tool ships
  on this meta disc.  (Image a damaged optical disc first with, e.g.,
  'dd if=/dev/sr0 of=disc.iso conv=noerror,sync bs=2048'.)

QUICK START:
  1. Insert the disc labelled LCSAS_META into your drive.
  2. Mount it (typically: sudo mount /dev/sr0 /mnt).
  3. Run: sh /mnt/restore.sh ~/restored/ latest
  4. Answer the prompts: put in your newest data disc (or type S to
     skip), choose the backup set, then type its password (or the
     words of its share cards).
  5. When asked to swap discs, do so and press Enter.
  To stop, type q at a disc prompt (or press Ctrl-C).  The tools stay
  in ~/lcsas-restore-tools; run 'sh ~/lcsas-restore-tools/resume.sh'
  to carry on later.

  RECOVERY_ROOT (auto-detected when restore.sh is run from inside the
  recovery tree) must contain bin/<arch>/lcsas-restore and/or src/.
  TARGET_DIR is where to write restored files (default: /tmp/restored).

FLAGS AND ENVIRONMENT VARIABLES:
  --repo NAME             Backup set to restore, by its name or its id
                          (same as LCSAS_REPO=NAME).  Skips the
                          repository selection prompt.
  --target DIR            Where to write restored files.  Equivalent to
                          the positional TARGET_DIR; give one or the
                          other, not both.
  --key FILE              Path to a file whose contents are the password
                          (same as LCSAS_PWFILE=FILE).  Skips the
                          Password: prompt.
  --version               Print the build commit SHA and date, then exit.
  LCSAS_PASSWORD          Repository password (skips the Password: prompt).
                          Mutually exclusive with LCSAS_PWFILE.
  LCSAS_PWFILE            Path to a file whose contents are the password.
                          Read instead of prompting; preserves any
                          trailing newline in the file.
  LCSAS_SHARES_DIR        Folder of share card files, one card per file.
                          The password is rebuilt from them with the
                          bundled lcsas-keyshare instead of prompting.
  LCSAS_REPO              Backup set to restore, by name or id.  Skips
                          the repository prompt on archives with more
                          than one repository.
  LCSAS_NEWEST_DISC_PROMPT
                          0 → skip the "put in your NEWEST data disc"
                          step at the start: use the discs mounted now,
                          else the copies on the META disc.
  LCSAS_ANOTHER_SET       0 → do not ask "Restore another backup set?"
                          after a restore that succeeds.
  LCSAS_TARGET            Override the auto-detected rust-triple (e.g.
                          force x86_64-unknown-linux-musl on a host
                          where uname -m misreports).
  LCSAS_META_DISC         Mount point of the meta disc.
                          Tells the tier-1 binary not to look for
                          packs there and to chdir out before prompts.
                          Auto-detected via mtab when omitted.
  LCSAS_MOUNT_DIRS        Colon-separated list of directories to scan
                          for mounted data discs (default:
                          /Volumes:/media/<user>:/media:/mnt:/run/media/<user>).
                          Set to '' (empty) to disable the auto-scan.
  LCSAS_TOOLS_DIR         Tools folder (default: \$HOME/lcsas-restore-tools).
                          Run off a disc, the script copies itself, this
                          computer's binaries, the docs and the disc's
                          metadata/ there, keeps the pack cache, a copy
                          of the catalog and resume.sh there, and runs
                          from there.  Removed after a restore that
                          succeeds; kept otherwise, so that
                          'sh <tools>/resume.sh' carries on (no META
                          disc needed).
  LCSAS_PACK_CACHE_DIR    Opportunistic pack cache.  'auto' (default
                          when unset) → <tools>/pack-cache;
                          a path → that path; '' (empty) → cache off.
                          Trades disk space for fewer disc swaps.
  LCSAS_INTERACTIVE       on / off / auto, passed to tier 1 as
                          --interactive.  auto (default) asks at the
                          disc prompt only on a terminal.  The script's
                          own extra questions (newest data disc, a
                          wrong password, another backup set) follow
                          it too: on asks them even on a pipe.
  LCSAS_TIER_FALLBACK     0 (default) → tier 1 crash aborts the run.
                          1 → fall through to tier 2 / tier 3 on
                          non-zero exit.  Use when you suspect a bug
                          in a higher tier and want the script to
                          walk the cascade for you.
  LCSAS_ALLOW_NO_PACK_SEARCH
                          1 → suppress the 'no data discs detected'
                          hard-error (advanced / test environments).
  LCSAS_ALLOW_TMPFS_TARGET
                          1 → skip the 'restore into RAM anyway?'
                          confirmation prompt when TARGET_DIR sits on
                          a RAM-backed filesystem (tmpfs/ramfs).  The
                          warning still prints.
  LCSAS_FORBID_TMPFS_TARGET
                          1 → refuse a RAM-backed TARGET_DIR outright,
                          even with no TTY attached.  For automation
                          that must never restore into RAM.
  LCSAS_SKIP_SPACE_CHECK  1 → skip the free-space preflight that
                          compares the archive size (from the catalog)
                          against free space at TARGET_DIR and the
                          pack cache (twice the size when both are on
                          one disk) before any password/disc prompt.
                          Default: check ON when the size is derivable.
  LCSAS_FORCE_NONEMPTY_TARGET
                          1 → skip the 'TARGET_DIR is non-empty and not
                          a previous LCSAS restore -- overwrite?' prompt.
                          Restoring into such a folder otherwise prompts
                          (and refuses with exit 65 when no terminal is
                          attached).  Re-runs of a set into its prior
                          LCSAS target are silent regardless (a hidden
                          marker file, .lcsas-restore-marker, names the
                          sets restored there); another set into that
                          folder prompts too.
  LCSAS_MAX_JSON_MIB      Max token-buffer memory (MiB) the tier-1
                          binary allocates to parse any single JSON
                          structure (index/snapshot/tree).  Default 256;
                          parsing is otherwise size-adaptive.  Over the
                          ceiling fails loud (names the file, points at
                          tier-2), never silently skips.  Lower on tiny
                          32-bit hosts.
  LCSAS_MAX_TREE_DEPTH    Max directory-tree recursion depth the tier-1
                          binary will walk.  Default 1000 (~6 MB of the
                          default 8 MB stack).  A deeper tree fails loud
                          with a named error instead of crashing; raise
                          this AND run 'ulimit -s unlimited' to restore a
                          genuinely deeper layout, or use tier-2.
  LCSAS_NO_RELOCATE       1 → don't copy the recovery scripts into
                          RAM before exec (testing / development).
  LCSAS_PROGRESS          0 → silence the periodic tier-3 progress
                          stderr lines.  Default ON.

Most heirs don't need any of these.  See the meta disc's
recovery/docs/RECOVER.txt for the heir-friendly walkthrough, and
docs/TROUBLESHOOTING.md when something goes wrong.
EOF
            exit 0 ;;
        --repo)
            LCSAS_REPO="${2:?--repo requires a NAME argument}"
            shift 2 ;;
        --target)
            TARGET_FLAG="${2:?--target requires a DIR argument}"
            shift 2 ;;
        --key)
            LCSAS_PWFILE="${2:?--key requires a FILE argument}"
            # Validate NOW -- before any password/disc prompts -- so a
            # typo'd path fails in one obvious line, not mid-restore.
            if [ ! -f "$LCSAS_PWFILE" ] || [ ! -r "$LCSAS_PWFILE" ]; then
                printf "restore.sh: --key: cannot read '%s'\n" \
                       "$LCSAS_PWFILE" >&2
                exit 2
            fi
            export LCSAS_PWFILE
            shift 2 ;;
        --version)
            printf 'lcsas-restore.sh %s (built %s)\n' \
                "$LCSAS_RESTORE_BUILD_SHA" "$LCSAS_RESTORE_BUILD_DATE"
            exit 0 ;;
        --check-disc)
            CHECK_DISC_IMG="${2:?--check-disc requires an IMAGE argument}"
            shift 2 ;;
        --) shift; break ;;
        -*)
            printf "restore.sh: unknown flag '%s'\n" "$1" >&2
            printf 'valid flags: --repo NAME, --target DIR, --key FILE, --version, --check-disc IMAGE, --help\n' >&2
            exit 2 ;;
        *) break ;;
    esac
done

# ── --check-disc: scratched-disc repair path [FMT-01] ─────────────
#
# Self-contained "my disc is scratched" entry point.  Resolves the
# recovery root + per-target triple, locates the bundled in-house
# lcsas-ecc binary, runs `lcsas-ecc verify`, and -- on damage (exit
# 1) -- offers/performs `lcsas-ecc fix`.  Never touches the
# repository, password, or restore machinery; exits when done.
#
# Exit codes mirror the wrapped tool plus a couple of script-local
# ones:
#   0   disc clean, or damage fully repaired
#   1   damage remained uncorrectable after fix (or heir declined fix)
#   2   image has no RS03 ECC header (nothing to check/repair)
#   3   usage / I-O error (missing image, no lcsas-ecc binary)
if [ -n "$CHECK_DISC_IMG" ]; then
    if [ ! -f "$CHECK_DISC_IMG" ]; then
        printf 'restore.sh: --check-disc: image not found: %s\n' \
               "$CHECK_DISC_IMG" >&2
        exit 3
    fi

    # Resolve the recovery root the same way the main path does, but
    # without requiring a TARGET_DIR positional: an explicit recovery
    # root as $1, else the auto-detected tree.
    cd_recovery=""
    if [ $# -ge 1 ] && { [ -d "$1/bin" ] || [ -d "$1/src" ]; } 2>/dev/null; then
        cd_recovery="$1"
    elif [ -n "$AUTO_RECOVERY" ]; then
        cd_recovery="$AUTO_RECOVERY"
    else
        printf 'restore.sh: --check-disc: cannot locate the recovery tree ' >&2
        printf '(run from inside it, or pass RECOVERY_ROOT as the first arg)\n' >&2
        exit 3
    fi

    # Per-target triple selection -- same matrix as the main dispatch,
    # honouring $LCSAS_TARGET.  Resolve bin/<triple>/lcsas-ecc, with a
    # bin/lcsas-ecc fallback for flat layouts / tests.
    cd_machine="$(uname -m 2>/dev/null || echo x86_64)"
    cd_os="$(uname -s 2>/dev/null || echo Linux)"
    if [ -n "${LCSAS_TARGET:-}" ]; then
        cd_triple="$LCSAS_TARGET"
    else
        case "$cd_os" in
            Linux)
                case "$cd_machine" in
                    x86_64|amd64)      cd_triple="x86_64-unknown-linux-musl" ;;
                    aarch64|arm64)     cd_triple="aarch64-unknown-linux-musl" ;;
                    armv7*|armv6*|arm) cd_triple="armv7-unknown-linux-gnueabihf" ;;
                    *)                 cd_triple="x86_64-unknown-linux-musl" ;;
                esac ;;
            Darwin)
                case "$cd_machine" in
                    arm64|aarch64)     cd_triple="aarch64-apple-darwin" ;;
                    *)                 cd_triple="x86_64-apple-darwin" ;;
                esac ;;
            *)                         cd_triple="x86_64-pc-windows-gnu" ;;
        esac
    fi

    cd_ecc_bin=""
    for cd_cand in \
        "$cd_recovery/bin/$cd_triple/lcsas-ecc" \
        "$cd_recovery/bin/$cd_triple/lcsas-ecc.exe" \
        "$cd_recovery/bin/lcsas-ecc"; do
        if [ -x "$cd_cand" ]; then cd_ecc_bin="$cd_cand"; break; fi
    done
    if [ -z "$cd_ecc_bin" ]; then
        printf 'restore.sh: --check-disc: no lcsas-ecc binary found under ' >&2
        printf '%s/bin/%s/ -- this meta disc cannot repair discs.\n' \
               "$cd_recovery" "$cd_triple" >&2
        printf 'Try the newest META disc you have (its tooling can repair ' >&2
        printf 'older data discs).\n' >&2
        exit 3
    fi

    printf '[check-disc] scanning %s for damage with %s\n' \
           "$CHECK_DISC_IMG" "$cd_ecc_bin" >&2
    cd_rc=0
    "$cd_ecc_bin" verify "$CHECK_DISC_IMG" || cd_rc=$?
    case "$cd_rc" in
        0)
            printf '[check-disc] no damage found; disc image is intact.\n' >&2
            exit 0 ;;
        2)
            printf '[check-disc] this image has no RS03 ECC parity layer; ' >&2
            printf 'nothing to repair.\n' >&2
            exit 2 ;;
        1)
            : ;;  # damage found -- fall through to the repair offer
        *)
            printf '[check-disc] could not read the image (exit %d).\n' \
                   "$cd_rc" >&2
            exit 3 ;;
    esac

    printf '[check-disc] DAMAGE DETECTED in %s.\n' "$CHECK_DISC_IMG" >&2
    # Confirm before writing, unless told to repair non-interactively.
    cd_do_fix=0
    if [ "${LCSAS_CHECK_DISC_AUTOFIX:-0}" = "1" ]; then
        cd_do_fix=1
    elif [ -t 0 ]; then
        printf 'Repair it now using the burned ECC parity? [y/N]: ' >&2
        IFS= read -r cd_ans
        case "$cd_ans" in y|Y|yes|YES|Yes) cd_do_fix=1 ;; esac
    else
        printf '[check-disc] no terminal to confirm on; not repairing.\n' >&2
        printf 'Re-run with LCSAS_CHECK_DISC_AUTOFIX=1 to repair ' >&2
        printf 'non-interactively.\n' >&2
        exit 1
    fi
    if [ "$cd_do_fix" != "1" ]; then
        printf '[check-disc] left unrepaired at your request.\n' >&2
        exit 1
    fi

    printf '[check-disc] repairing in place with %s ...\n' "$cd_ecc_bin" >&2
    cd_fix_rc=0
    "$cd_ecc_bin" fix "$CHECK_DISC_IMG" || cd_fix_rc=$?
    case "$cd_fix_rc" in
        0)
            printf '[check-disc] repair succeeded; %s is now intact.\n' \
                   "$CHECK_DISC_IMG" >&2
            exit 0 ;;
        1)
            printf '[check-disc] damage exceeded the available ECC parity; ' >&2
            printf 'some sectors are uncorrectable.\n' >&2
            printf 'Try another disc with the same label if one exists.\n' >&2
            exit 1 ;;
        *)
            printf '[check-disc] repair failed (exit %d).\n' "$cd_fix_rc" >&2
            exit 3 ;;
    esac
fi

# Pattern 1: first arg looks like a recovery root (has bin/ or src/).
if [ $# -ge 2 ] && [ -d "$1/bin" -o -d "$1/src" ] 2>/dev/null; then
    RECOVERY="$1"
    TARGET="$2"
    TARGET_FROM_POS=1
    SNAP="${3:-latest}"
elif [ $# -ge 1 ] && [ -n "$TARGET_FLAG" ] \
     && [ -d "$1/bin" -o -d "$1/src" ] 2>/dev/null; then
    # --target DIR already supplied the target; the sole positional is
    # the recovery root (optionally followed by a snapshot id).
    RECOVERY="$1"
    SNAP="${2:-latest}"
elif [ $# -ge 1 ] && [ -n "$AUTO_RECOVERY" ]; then
    RECOVERY="$AUTO_RECOVERY"
    TARGET="$1"
    TARGET_FROM_POS=1
    SNAP="${2:-latest}"
elif [ -n "$AUTO_RECOVERY" ]; then
    RECOVERY="$AUTO_RECOVERY"
    TARGET="${TARGET:-/tmp/restored}"
else
    cat >&2 <<EOF
usage: $0 [--repo NAME] [--target DIR] [--key FILE] [--version] [RECOVERY_ROOT] [TARGET_DIR] [SNAPSHOT_ID|latest]

  RECOVERY_ROOT (auto-detected when restore.sh is run from inside the
  recovery tree) must contain bin/<arch>/lcsas-restore and/or src/.
  TARGET_DIR is where to write restored files (default: /tmp/restored);
  --target DIR is the equivalent named form.

  Password is read from stdin, \$LCSAS_PASSWORD env, \$LCSAS_PWFILE,
  or --key FILE.
EOF
    exit 2
fi

# --target DIR (flag) and a positional TARGET_DIR are mutually
# exclusive -- with both present we cannot tell which the heir
# meant, so refuse rather than guess.
if [ -n "$TARGET_FLAG" ]; then
    if [ "$TARGET_FROM_POS" = "1" ]; then
        printf 'restore.sh: give the target once: either --target DIR or a positional TARGET_DIR\n' >&2
        exit 2
    fi
    TARGET="$TARGET_FLAG"
fi

# A recovery root given as a relative path is made absolute: resume.sh
# and the next set's run start from another folder, where it would name
# nothing (or be taken for the target).
case "$RECOVERY" in
    /*) ;;
    *) RECOVERY="$(cd "$RECOVERY" && pwd)" ;;
esac

# ── Target detection (arch + OS) ──────────────────────────────────
#
# Picks one of the six targets bundled by the meta disc builder (see
# docs/history/CROSS_PLATFORM_META_RFC.md §3, recovery/UPSTREAM.sha256):
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

# IMPORTANT: $TARGET above is the *caller-supplied restore directory*
# (positional arg 1 or 2).  From here on we need a separate name for
# the per-platform rust-triple that selects which binary to dispatch
# to under recovery/bin/.  Stuff the caller's TARGET into TARGET_DIR
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
# The folder that holds the typed share cards (#607), one file each.
SHARE_DIR=""
# The terminal settings while the password is read with echo off; the
# exit handler puts them back if Ctrl-C lands at that prompt.
_stty_saved=""
# Tier-3 stderr capture files (issue #240); set if tier 3 fires.
TIER3_ERR=""
TIER3_RC_FILE=""
# Persistent exit-diagnostic log (issue #259).  Written on non-zero exit so
# the failure is visible even after the tmux session dies.
_restore_exit_log="/tmp/lcsas-restore-exit.$(date +%Y%m%d-%H%M%S-$$)"
# shellcheck disable=SC2317  # runs from the EXIT/INT/TERM trap below
_restore_exit_handler() {
    rc=$?
    # Every step below is best-effort: under set -e one failed rm used to
    # abort the handler and turn a restore that succeeded into exit 1.
    # A signal can land while IFS is changed (the set list splits on
    # newlines, the mount-dir walks on colons): put it back.
    set +e
    IFS=' 	
'

    if [ -n "${_stty_saved:-}" ]; then
        stty "$_stty_saved" 2>/dev/null
        printf '\n' >&2
    fi
    if [ -n "${TIER3_ERR:-}" ]; then rm -f "$TIER3_ERR"; fi
    if [ -n "${TIER3_RC_FILE:-}" ]; then rm -f "$TIER3_RC_FILE"; fi
    if [ -n "${PWFILE_TMP:-}" ]; then rm -f "$PWFILE_TMP"; fi
    if [ -n "${SHARE_DIR:-}" ]; then rm -rf "$SHARE_DIR"; fi
    if [ -n "${RUN_TMP:-}" ]; then rm_tree "$RUN_TMP"; fi
    # #609: the tools folder goes after a restore that succeeded (#508:
    # otherwise every run left ~130 MB behind), and stays after any other
    # exit -- a wrong password, q at the disc prompt, Ctrl-C -- so the
    # restore can carry on from it.  Only a folder carrying the marker,
    # and only what this script put there (tools_dir_remove).  Once the
    # set is restored (RESTORE_DONE), a stop at the "another set?"
    # questions leaves nothing to carry on.
    if [ -n "${TOOLS:-}" ] && [ -f "$TOOLS/$TOOLS_MARK" ]; then
        if [ "$rc" -eq 0 ] || [ "${RESTORE_DONE:-0}" = 1 ]; then
            cd / 2>/dev/null
            tools_dir_remove "$TOOLS"
        elif [ -f "$TOOLS/resume.sh" ]; then
            cat >&2 <<EOF

The restore stopped before it finished (exit code $rc).  The files
restored so far stay in the target folder, and the tools copied from
the disc stay in
    $TOOLS
To carry on later (the META disc is not needed), run:
    sh $(sh_word "$TOOLS/resume.sh")
It asks for the password again.  Delete that folder when you are done.
EOF
        elif [ -d "$TOOLS/recovery" ] || [ -d "$TOOLS/pack-cache" ]; then
            printf '[restore.sh] the tools copied from the disc stay in %s for the next run\n' \
                   "$TOOLS" >&2
        else
            # Nothing in it worth keeping: the run stopped before it began.
            tools_dir_remove "$TOOLS"
        fi
    fi
    if [ "$rc" -ne 0 ]; then
        printf '[restore.sh] EXIT rc=%d at line approx=%d\n' "$rc" "${LINENO:-0}" \
            >> "$_restore_exit_log" 2>/dev/null
        printf '[restore.sh] PYBIN=%s PYREST=%s\n' "${PYBIN:-unset}" "${PYREST:-unset}" \
            >> "$_restore_exit_log" 2>/dev/null
        printf '[restore.sh] exit log: %s\n' "$_restore_exit_log" >&2
    fi
    # Return 0 so the trap does not clobber the script's exit status
    # (POSIX traps inherit the trap action's return code under some
    # shells -- notably dash on Debian / Ubuntu / busybox).  The set +e
    # above is what makes this line always reached.
    return 0
}
trap _restore_exit_handler EXIT
# Ctrl-C, TERM or a closed terminal end the run with the shell's usual
# status, so the EXIT handler sees a failure and KEEPS the tools folder.
# The handler used to run for INT/TERM itself, return 0 and let the
# script carry on, which then exited 0 (#616).
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# ── The tools folder and this run's own files (#609, #610) ───────
#
# TOOLS holds what a restore needs once the meta disc is out (see the
# header).  Relocated, it is the folder relocate_to_ram copied into; run
# in place, it holds only the pack cache, the catalog copy and
# resume.sh.  When no tools folder can be made (no $HOME, say), the run
# keeps those files in a temporary folder that goes when it exits, so a
# stopped restore cannot be carried on from it.
TOOLS=""
RUN_TMP=""
_tools_want="${LCSAS_TOOLS_DIR:-${HOME:+$HOME/lcsas-restore-tools}}"
case "$_tools_want" in ''|/*) ;; *) _tools_want="$(pwd)/$_tools_want" ;; esac
if [ -n "$_tools_want" ] && tools_dir_claim "$_tools_want"; then
    TOOLS="$(cd "$_tools_want" && pwd -P)"
    # Run in place (relocate_to_ram has done this for a relocated run):
    # what another build or another archive left here is not used (C6).
    # Its catalog copy would be the first catalog considered, and its
    # sets would join the list.
    if [ -z "${LCSAS_RELOCATED:-}" ]; then
        _tw_meta="$(dirname "$RECOVERY")/metadata"
        _tw_build="$(tools_dir_build "$TOOLS")"
        if tools_other_archive "$TOOLS" "$_tw_meta"; then
            tools_dir_replace "$TOOLS" "the copies of another archive's backup sets" 0
        elif [ -n "$_tw_build" ] && [ "$_tw_build" != "$(build_line "$SCRIPT")" ]; then
            tools_dir_replace "$TOOLS" "an older copy of the recovery tools" 0
        elif [ -f "$TOOLS/recovery/scripts/restore.sh" ] \
             && ! cmp -s "$SCRIPT" "$TOOLS/recovery/scripts/restore.sh"; then
            tools_dir_replace "$TOOLS" "an older copy of the recovery tools" 1
        fi
        build_line "$SCRIPT" > "$TOOLS/.lcsas-build"
        if [ -n "$(meta_sets "$_tw_meta")" ]; then
            meta_sets "$_tw_meta" > "$TOOLS/.lcsas-sets"
        fi
        unset _tw_meta _tw_build
    fi
else
    [ -z "$_tools_want" ] \
        || printf '[restore.sh] cannot use %s as the tools folder; using a temporary one\n' \
                  "$_tools_want" >&2
    RUN_TMP="$(mktemp -d "${TMPDIR:-/tmp}/lcsas-restore-run.XXXXXX" 2>/dev/null || true)"
fi
STATE_DIR="${TOOLS:-$RUN_TMP}"
unset _tools_want

# ── Mount directories, the meta disc and the newest data disc ─────
#
# The directories scanned for mounted discs, by repository discovery,
# the catalog pick and the pack search below.  Overridable via
# LCSAS_MOUNT_DIRS (colon-separated list), useful for tests and for
# unusual setups (e.g. systemd-mounted /run/media/$USER/).  Default
# mimics the /Volumes /media /mnt convention used elsewhere in the
# script.
LCSAS_MOUNT_DIRS_DEFAULT="/Volumes:/media/$(id -un 2>/dev/null):/media:/mnt:/run/media/$(id -un 2>/dev/null):/run/media"
LCSAS_MOUNT_DIRS_EFFECTIVE="${LCSAS_MOUNT_DIRS-$LCSAS_MOUNT_DIRS_DEFAULT}"
# Export so the tier-1 binary inherits the SAME list when it
# re-enumerates mount parents on every "press Enter to retry".  If
# the shell and the binary disagree here, a disc auto-mounted under
# (say) /run/media/$USER/ becomes invisible to the C-side locator
# even though the shell already found it.
export LCSAS_MOUNT_DIRS="$LCSAS_MOUNT_DIRS_EFFECTIVE"

# Opportunistic pack cache.  ON by default — without it the tier-1
# binary asks the heir to swap discs once per blob in the worst
# case (the v3 blind run took 16 swaps for 3 discs).  With it, the
# rest of each disc's data/ subtree is drained into a local cache
# on first contact; subsequent packs from the same disc resolve
# locally and the disc-swap count drops to O(N_discs).
#
# Three equivalent values:
#   LCSAS_PACK_CACHE_DIR=/abs/path   use that path (advanced)
#   LCSAS_PACK_CACHE_DIR=auto        auto-allocate under $TMPDIR
#   LCSAS_PACK_CACHE_DIR=            (empty) disable — hosts short
#                                    on disk space only
#
# Default when UNSET: auto, which is pack-cache/ in the tools folder
# (#616: it used to be ${TMPDIR:-/tmp}/lcsas-pack-cache.<pid>, which no
# run ever removed and no later run could reuse).  Explicitly empty: off.
_cache_auto=0
if [ "${LCSAS_PACK_CACHE_DIR-auto}" = "auto" ]; then
    LCSAS_PACK_CACHE_DIR="${STATE_DIR:+$STATE_DIR/pack-cache}"
    _cache_auto=1
fi
export LCSAS_PACK_CACHE_DIR

# The meta disc.  META_ROOT is the folder that holds recovery/: the disc
# root when the script runs from the disc, the tools folder after
# relocation (which copies the disc's metadata/ there).  META_DISC is
# where the disc was mounted.  Like tier 1 (disc_locator.c
# meta_disc_is_live), it counts as the meta disc only while its
# recovery/scripts/restore.sh is there: once the disc is out, its old
# mount point may hold a data disc.
META_ROOT="$(dirname "$RECOVERY")"
meta_disc_is_live() {
    [ -n "${META_DISC:-}" ] && [ -e "$META_DISC/recovery/scripts/restore.sh" ]
}
is_meta_mount() {
    [ "$1" = "$META_ROOT" ] && return 0
    meta_disc_is_live || return 1
    case "$1" in "$META_DISC"|"$META_DISC"/*) return 0 ;; esac
    return 1
}

EXIT_USER_ABORT=65
# A single newline and a tab, the field separators of the lists below.
# restore.sh may run against discs auto-mounted at paths containing
# spaces (e.g. /media/user/DISC LABEL 1), so the flag lists
# (--pack-search / --catalog / --meta-disc / --mount-point) are stored
# one token per line and expanded with `IFS=$NL; set -- $LIST` at each
# call site — never as space-joined strings that word-split a spaced
# path (issue #364).
NL='
'
TAB="$(printf '\t')"

# is_interactive: 0 when this run may ask its own extra questions (the
# newest data disc, a wrong password, another backup set).  It follows
# LCSAS_INTERACTIVE like tier 1 does: on asks even on a pipe (test rigs),
# off never asks, and auto (the default) asks only on a terminal, so a
# piped or scripted run never waits for an answer nobody gives.
is_interactive() {
    case "${LCSAS_INTERACTIVE:-auto}" in
        on)  return 0 ;;
        off) return 1 ;;
    esac
    [ -t 0 ]
}

# Text read off a disc (a set's name, a disc's label) is untrusted: it is
# only ever printed with printf '%s' or compared, never run, and control
# characters are dropped so it cannot drive the terminal.
clean_text() {
    tr -d '\000-\037\177'
}

# marker_field SET_DIR KEY: the value of KEY in SET_DIR/LCSAS_REPO.txt,
# the backup set marker (C1: key=value lines, a trailing CR stripped,
# # lines skipped), or nothing.  A marker whose id= is not the folder's
# name was copied into the wrong folder and is ignored.
marker_field() {
    [ -f "$1/LCSAS_REPO.txt" ] || return 0
    MF_ID="${1##*/}" MF_KEY="$2" awk '
        { sub(/\r$/, "") }
        /^#/ { next }
        { i = index($0, "=") }
        i > 0 { f[substr($0, 1, i - 1)] = substr($0, i + 1) }
        END {
            if (f["id"] == ENVIRON["MF_ID"] && (ENVIRON["MF_KEY"] in f))
                print f[ENVIRON["MF_KEY"]]
        }' "$1/LCSAS_REPO.txt" 2>/dev/null | clean_text
    return 0
}

# set_field SET_DIR KEY: marker_field for the set at SET_DIR, falling
# back to the tools folder's and the META disc's copies of the same set:
# a disc made before the marker existed has none, while a newer META
# disc's copy may.
set_field() {
    for _sf_dir in "$1" ${TOOLS:+"$TOOLS/metadata/${1##*/}"} \
                   "$META_ROOT/metadata/${1##*/}"; do
        _sf="$(marker_field "$_sf_dir" "$2")"
        if [ -n "$_sf" ]; then
            printf '%s' "$_sf"
            return 0
        fi
    done
    return 0
}

# set_label SET_DIR: the name a person knows the set by (#606): the
# marker's name=, else the folder's name, which is the set's id.
set_label() {
    _sl="$(set_field "$1" name)"
    case "$_sl" in
        *[!' ']*) printf '%s' "$_sl" ;;
        *)        printf '%s' "${1##*/}" ;;
    esac
}

# set_split SET_DIR: "K N" when the marker says the set's password is
# split into N share cards, any K of which rebuild it; else nothing.
set_split() {
    set_field "$1" split | awk -F'-of-' '
        NF == 2 && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ &&
        $1 + 0 >= 1 && $1 + 0 <= $2 + 0 && $2 + 0 <= 16 { print $1 + 0, $2 + 0 }'
}

# set_split_text SET_DIR: the menu's note for a split password.
set_split_text() {
    _st="$(set_split "$1")"
    [ -n "$_st" ] || return 0
    printf ' (password split: any %s of %s share cards)' "${_st% *}" "${_st#* }"
}

# file_mtime FILE: its mtime in seconds, 0 when it is missing.
file_mtime() {
    _fm="$(stat -c '%Y' "$1" 2>/dev/null || stat -f '%m' "$1" 2>/dev/null || true)"
    case "$_fm" in ''|*[!0-9]*) _fm=0 ;; esac
    printf '%s\n' "$_fm"
}

# is_meta_root DIR: 0 when DIR is the root of a META disc (or a copy).
is_meta_root() {
    [ -e "$1/recovery/scripts/restore.sh" ] && return 0
    [ -f "$1/volume_info.json" ] || return 1
    grep -q '"type"[[:space:]]*:[[:space:]]*"meta"' "$1/volume_info.json" 2>/dev/null
}

# disc_label DIR: the full label of the data disc mounted at DIR, from
# the "  Label:  <LABEL>" line of its VOLUME_INFO.txt (or the "label" of
# its volume_info.json); nothing for a META disc or a folder without
# one.  Never the name the computer shows for the disc: Joliet cuts that
# to 16 characters, the same for every disc of a year (C2).
disc_label() {
    _dl=""
    is_meta_root "$1" && return 0
    if [ -f "$1/VOLUME_INFO.txt" ]; then
        _dl="$(sed -n 's/^  Label:[[:space:]]*//p' "$1/VOLUME_INFO.txt" 2>/dev/null \
               | sed -n 1p | clean_text)"
    fi
    if [ -z "$_dl" ] && [ -f "$1/volume_info.json" ]; then
        _dl="$(sed -n 's/^.*"label"[[:space:]]*:[[:space:]]*"\([^"]*\)".*$/\1/p' \
                   "$1/volume_info.json" 2>/dev/null | sed -n 1p | clean_text)"
    fi
    printf '%s' "${_dl%"${_dl##*[! ]}"}"
}

# label_key LABEL: 18 digits that sort like the label's age (C2): the
# second-to-last "_" token is the year, the last the disc's number, so
# LCSAS_CD700_2026_0002 is newer than LCSAS_CD700_2025_0019.  A label of
# another shape counts as the oldest.
label_key() {
    printf '%s\n' "$1" | awk -F_ '{
        y = 0; s = 0
        if (NF >= 3 && $NF ~ /^[0-9]+$/ && $(NF - 1) ~ /^[0-9]+$/ &&
            length($NF) <= 9 && length($(NF - 1)) <= 9) {
            y = $(NF - 1) + 0; s = $NF + 0
        }
        printf "%09d%09d\n", y, s
    }'
}

# key_newer A B: 0 when label_key A is greater than label_key B.
key_newer() {
    awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 > b + 0) }'
}

# disc_copies: every folder under the mount directories (each one itself,
# and its children) that holds a metadata/ folder, one "KEY<TAB>DIR" line
# each, best first.  A data disc ranks by its label's year, then number,
# then by its catalog.db's mtime (C2); a META disc found there ranks
# below every data disc.  Ties keep the scan order.  The meta disc this
# script came from is left out while it is in the drive.
disc_copies() {
    _disc_copies_scan | sort -r
}
_disc_copies_scan() {
    _dc_i=0
    _dc_ifs="$IFS"; IFS=":"
    for _dc_parent in $LCSAS_MOUNT_DIRS_EFFECTIVE; do
        IFS="$_dc_ifs"
        if [ -z "$_dc_parent" ] || [ ! -d "$_dc_parent" ]; then IFS=":"; continue; fi
        for _dc_mnt in "$_dc_parent" "$_dc_parent"/*; do
            [ -d "$_dc_mnt/metadata" ] || continue
            is_meta_mount "$_dc_mnt" && continue
            _dc_i=$((_dc_i + 1))
            if is_meta_root "$_dc_mnt"; then
                _dc_key="1$(label_key '')000000000000"
            else
                _dc_key="2$(label_key "$(disc_label "$_dc_mnt")")"
                _dc_key="$_dc_key$(printf '%012d' "$(file_mtime "$_dc_mnt/catalog.db")")"
            fi
            printf '%s%06d%s%s\n' "$_dc_key" $((999999 - _dc_i)) "$TAB" "$_dc_mnt"
        done
        IFS=":"
    done
    IFS="$_dc_ifs"
    return 0
}

# data_discs: the data discs among disc_copies, newest first.
data_discs() {
    disc_copies | awk '/^2/'
}

# merge_set_metadata SET_DIR [LABEL]: make <tools>/metadata/<id>/ match
# the set's copy at SET_DIR (C2): its index/ and snapshots/ replace the
# older ones and its keys/ are added, so a resumed run, or the next set,
# with that disc out of the drive sees the same snapshots.  LABEL is the
# data disc SET_DIR is on; it is kept in <tools>/metadata/.from/<id> so
# discovery ranks the copy like that disc, and a copy from a newer disc
# is never replaced by one from an older disc (or from no disc).
# The new copy is built beside the old one (.<id>.new) and swapped in only
# when every file was copied: a scratched disc, a full disk or a disc
# taken out part-way leaves the old copy as it was, and untagged.
# 0 when the tools copy now holds what SET_DIR holds (copied, or it IS
# that copy); 1 when it was left alone or the copy failed.
merge_set_metadata() {
    _ms_id="${1##*/}"
    _ms_dst="$TOOLS/metadata/$_ms_id"
    _ms_tag="$TOOLS/metadata/.from/$_ms_id"
    _ms_new="$TOOLS/metadata/.$_ms_id.new"
    _ms_old="$TOOLS/metadata/.$_ms_id.old"
    [ "$(cd "$1" && pwd -P)" != "$(cd "$_ms_dst" 2>/dev/null && pwd -P)" ] || return 0
    if [ -f "$_ms_tag" ] && [ -d "$_ms_dst/index" ]; then
        [ -n "${2:-}" ] || return 1
        key_newer "$(label_key "$2")" "$(label_key "$(clean_text < "$_ms_tag")")" || return 1
    fi
    rm_tree "$_ms_new" "$_ms_old" || true
    _ms_ok=1
    mkdir -p "$_ms_new/keys" "$TOOLS/metadata/.from" || _ms_ok=0
    # Everything the copy has but its index/ and snapshots/, then the
    # disc's index/, snapshots/ and top-level files, and its keys/ added.
    for _ms_e in "$_ms_dst"/*; do
        [ -e "$_ms_e" ] || continue
        case "${_ms_e##*/}" in index|snapshots) continue ;; esac
        cp -R "$_ms_e" "$_ms_new/" 2>/dev/null || _ms_ok=0
    done
    for _ms_e in index snapshots; do
        if [ -d "$1/$_ms_e" ]; then cp -R "$1/$_ms_e" "$_ms_new/" 2>/dev/null || _ms_ok=0; fi
    done
    cp -R "$1/keys/." "$_ms_new/keys/" 2>/dev/null || _ms_ok=0
    for _ms_e in "$1"/*; do
        if [ -f "$_ms_e" ]; then cp -p "$_ms_e" "$_ms_new/" 2>/dev/null || _ms_ok=0; fi
    done
    if [ "$_ms_ok" = 1 ]; then
        make_writable "$_ms_new"
        if [ -e "$_ms_dst" ]; then mv "$_ms_dst" "$_ms_old"; fi
        mv "$_ms_new" "$_ms_dst"
        rm_tree "$_ms_old" || true
        if [ -n "${2:-}" ]; then
            printf '%s\n' "$2" > "$_ms_tag"
        else
            rm -f "$_ms_tag"
        fi
        return 0
    fi
    rm_tree "$_ms_new" || true
    printf '[lcsas-restore] could not copy all of the backup set %s from %s;\n' \
           "$(set_label "$1")" "${2:-$1}" >&2
    printf '                the copy in %s stays as it was.\n' "$_ms_dst" >&2
    return 1
}

# ── Step 0: the newest data disc (#605, C2, C7) ───────────────────
#
# Each disc holds every backup set's keys, index and snapshots as they
# were when it was made, and the restore reads them once, at the start.
# On a one-drive computer only the META disc is in at the start, and its
# copy can be older than the newest data disc (a set added later is not
# in it at all), so "latest" meant the META disc's latest.  So, run off
# the META disc with no data disc in yet, ask for the newest data disc
# first.  Only when someone can answer: on a terminal (see
# is_interactive), never for a pipe or a script; LCSAS_NEWEST_DISC_PROMPT=0
# skips it.  POSIX sh cannot wait with a timeout, so the reader presses
# ENTER once the disc is in.
newest_disc_wanted() {
    [ "${LCSAS_NEWEST_DISC_PROMPT:-1}" != 0 ] || return 1
    [ -n "${META_DISC:-}" ] || return 1
    is_interactive || return 1
    [ -z "$(data_discs)" ]
}
if newest_disc_wanted; then
    cat >&2 <<'EOF'

========================================================================
  Take the META disc out and put in your NEWEST data disc: the one
  with the latest year, and then the highest number, for example
  LCSAS_CD700_2026_0002.  The name the computer shows for a disc may
  be cut short; the full label is printed on the disc.

  To take the META disc out, eject it: use your file manager's Eject,
  or type  sudo eject /dev/sr0  in another terminal (on a Mac:
  drutil eject).  If the data disc does not open by itself, mount it:
  sudo mount /dev/sr0 /mnt

  Press ENTER when it is in; S then ENTER to skip (use the copies on
  the META disc); Q then ENTER to quit.
========================================================================
EOF
    while :; do
        printf 'ENTER, S or Q: ' >&2
        _nd_ans=""
        IFS= read -r _nd_ans || _nd_ans=q
        case "$_nd_ans" in
            s|S)
                printf 'Skipped: using the copies on the META disc.\n' >&2
                break ;;
            q|Q)
                printf 'Stopped at your request.\n' >&2
                exit "$EXIT_USER_ABORT" ;;
            '') ;;
            *)
                printf 'Press ENTER once the disc is in, or type S or Q.\n' >&2
                continue ;;
        esac
        _nd_top="$(data_discs | sed -n 1p)"
        _nd_dir="${_nd_top#*"$TAB"}"
        _nd_lab=""
        [ -z "$_nd_top" ] || _nd_lab="$(disc_label "$_nd_dir")"
        if [ -n "$_nd_lab" ]; then
            printf 'Found the data disc labelled %s\n' "$_nd_lab" >&2
            printf '  (at %s).\n' "$_nd_dir" >&2
            break
        elif [ -n "$_nd_top" ]; then
            printf 'The disc at %s has no label in a VOLUME_INFO.txt, so\n' "$_nd_dir" >&2
            printf 'it is not an LCSAS data disc.  Put in a data disc.\n' >&2
        elif meta_disc_is_live; then
            printf 'The META disc is still in the drive (%s).\n' "$META_DISC" >&2
            printf 'Take it out first, then put in the newest data disc.\n' >&2
        else
            printf 'No data disc found yet in %s.\n' "$LCSAS_MOUNT_DIRS_EFFECTIVE" >&2
            printf 'If it does not open by itself, mount it, for example:\n' >&2
            printf '    sudo mount /dev/sr0 /mnt\n' >&2
        fi
    done
fi

# The newest data disc mounted now refreshes the tools folder's copy of
# every set it holds (C2): its snapshots/ and index/ replace the META
# disc's, keys/ are added, and sets it does not hold keep the META
# disc's copy.  Only on the single-drive journey (a META disc is known),
# where that disc may come out of the drive before the next set or a
# resumed run needs the copy.  Never in a resumed run (LCSAS_RESUME,
# set by resume.sh): it carries on with the copy the stopped run used.
if [ -n "$TOOLS" ] && [ -n "${META_DISC:-}" ] && [ "${LCSAS_RESUME:-0}" != 1 ]; then
    _nd_top="$(data_discs | sed -n 1p)"
    _nd_dir="${_nd_top#*"$TAB"}"
    _nd_lab=""
    [ -z "$_nd_top" ] || _nd_lab="$(disc_label "$_nd_dir")"
    if [ -n "$_nd_lab" ]; then
        _nd_copy=0
        for _nd_set in "$_nd_dir/metadata"/*; do
            if [ ! -d "$_nd_set/keys" ] || [ ! -d "$_nd_set/index" ]; then continue; fi
            _nd_tag="$TOOLS/metadata/.from/${_nd_set##*/}"
            if [ ! -f "$_nd_tag" ] || key_newer "$(label_key "$_nd_lab")" \
                    "$(label_key "$(clean_text < "$_nd_tag")")"; then
                _nd_copy=1
            fi
        done
        if [ "$_nd_copy" = 1 ]; then
            printf '[lcsas-restore] copying the backup sets'"'"' metadata from the disc labelled\n' >&2
            printf '                %s to %s/metadata...\n' "$_nd_lab" "$TOOLS" >&2
            for _nd_set in "$_nd_dir/metadata"/*; do
                if [ ! -d "$_nd_set/keys" ] || [ ! -d "$_nd_set/index" ]; then continue; fi
                merge_set_metadata "$_nd_set" "$_nd_lab" || true
            done
        fi
    fi
fi

# ── Repo discovery ────────────────────────────────────────────────
#
# A restic-format repository is a directory containing keys/ and
# index/ subdirs.  The historical layout placed it directly at
# ${RECOVERY}/repo (the retired legacy bash driver's).  Modern LCSAS
# archives instead carry each repository's metadata under
# metadata/<repo_id>/ on every disc — the layout where the meta disc
# and every data disc both ship the repository metadata for every
# backed-up repository.  Probe both layouts, plus any mounted disc.
#
# Every copy of a set gets a rank, and the best copy of each set is the
# one used (C2, #605): the legacy layouts first, then the data discs,
# newest label first (an older disc's copy is older), then the tools
# folder's copy ranked like the data disc it was refreshed from, and the
# META disc's copies last, as the fallback for a set that no data disc
# holds.  First-wins by scan order put the META disc's copy first, which
# can be older than the data discs, and missed a set added after it.
#
# When multiple candidate repositories are found (typical: a
# multi-repository archive), LCSAS_REPO selects one by its id or its
# name.  If exactly one candidate exists we use it; otherwise we list
# them by name and ask.

REPO=""
REPO_CANDIDATES=""
_cand_i=0
add_repo_candidate() {
    # $1: path to inspect; $2: its rank.  If it looks like a restic repo
    # (keys/ + index/), add it to REPO_CANDIDATES as a "KEY<TAB>PATH"
    # line; the key's last 6 digits keep the scan order on ties.
    [ -d "$1/keys" ] && [ -d "$1/index" ] || return 0
    _cand_i=$((_cand_i + 1))
    REPO_CANDIDATES="$REPO_CANDIDATES$2$(printf '%06d' $((999999 - _cand_i)))$TAB$1$NL"
}
_key_none="$(label_key '')000000000000"

# Direct layouts (legacy).
add_repo_candidate "$RECOVERY/repo" "3$_key_none"
add_repo_candidate "$RECOVERY" "3$_key_none"
# Any currently-mounted archive disc (the same directories the
# pack-search scan below uses), so an heir can run `sh restore.sh`
# immediately after mounting a single data disc — no manual symlink
# dance required.  The mount point itself may directly contain
# metadata/* (when /mnt is the disc root), so disc_copies probes both
# /mnt itself and any children of /mnt.
_dc_list="$(disc_copies)"
while IFS= read -r _dc_line; do
    [ -n "$_dc_line" ] || continue
    _dc_key="${_dc_line%%"$TAB"*}"
    for cand in "${_dc_line#*"$TAB"}/metadata"/*; do
        [ -d "$cand" ] || continue
        add_repo_candidate "$cand" "${_dc_key%??????}"
    done
done <<EOF
$_dc_list
EOF
# The tools folder's copies (relocated, this is also <META_ROOT>/metadata):
# ranked like the data disc each was refreshed from, else as a META copy.
_tools_md=""
if [ -n "$TOOLS" ] && [ -d "$TOOLS/metadata" ]; then
    _tools_md="$TOOLS/metadata"
    for cand in "$_tools_md"/*; do
        [ -d "$cand" ] || continue
        if [ -f "$_tools_md/.from/${cand##*/}" ]; then
            add_repo_candidate "$cand" \
                "2$(label_key "$(clean_text < "$_tools_md/.from/${cand##*/}")")000000000000"
        else
            add_repo_candidate "$cand" "1$_key_none"
        fi
    done
fi
# The meta disc's own copies come LAST: <META_ROOT>/metadata/ (the disc
# root, or the tools folder's copy), the legacy $RECOVERY/metadata/, and
# the disc itself.  The meta disc's metadata/ used to be found ONLY by
# the mount-dir walk above, so a meta disc mounted anywhere else (say
# /cdrom) with no data disc mounted found no repository at all (#616).
for _meta_md in "$META_ROOT/metadata" "$RECOVERY/metadata" \
                ${META_DISC:+"$META_DISC/metadata"}; do
    [ -d "$_meta_md" ] || continue
    if [ -n "$_tools_md" ] \
       && [ "$(cd "$_meta_md" && pwd -P)" = "$(cd "$_tools_md" && pwd -P)" ]; then
        continue
    fi
    for cand in "$_meta_md"/*; do
        [ -d "$cand" ] || continue
        add_repo_candidate "$cand" "1$_key_none"
    done
done

# Best copy first, then one copy per set: de-duplicate by the folder
# name (the set's id), keeping the best-ranked copy.  Every data disc
# carries metadata/<id>/ for every set, so keying on the path listed one
# set once per mounted disc and turned a single-set archive into a bogus
# "Multiple repositories" prompt (#533).
REPO_CANDIDATES="$(printf '%s' "$REPO_CANDIDATES" | sort -r | awk -F"$TAB" '
    NF >= 2 {
        p = substr($0, index($0, FS) + 1)
        n = split(p, a, "/")
        if (!seen[a[n]]++) print p
    }')"
# The sets are listed by name (then id), so a number means the same set
# on every run: the next set's list, or a resumed run's.
_by_name=""
while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    _by_name="$_by_name$(set_label "$cand")$TAB${cand##*/}$TAB$cand$NL"
done <<EOF
$REPO_CANDIDATES
EOF
REPO_CANDIDATES="$(printf '%s' "$_by_name" | LC_ALL=C sort -f | cut -f3-)"

# set_match WANT: the candidate whose id is WANT; else the one candidate
# whose name, or name_cmd (the plain-letters form restore.bat shows, C1,
# C4), is WANT (#606).  Prints nothing when none, or when two sets share
# that name (then it says so: the number or the id tells them apart).
# Expects IFS to be a newline, as the menu block sets it.
set_match() {
    [ -n "$1" ] || return 0
    for _sm_c in $REPO_CANDIDATES; do
        case "${_sm_c##*/}" in "$1") printf '%s' "$_sm_c"; return 0 ;; esac
    done
    _sm_hit=""
    _sm_n=0
    for _sm_c in $REPO_CANDIDATES; do
        _sm_is=0
        case "$(set_label "$_sm_c")" in "$1") _sm_is=1 ;; esac
        if [ "$_sm_is" = 0 ]; then
            case "$(set_field "$_sm_c" name_cmd)" in "$1") _sm_is=1 ;; esac
        fi
        if [ "$_sm_is" = 1 ]; then
            _sm_hit="$_sm_c"
            _sm_n=$((_sm_n + 1))
        fi
    done
    if [ "$_sm_n" -gt 1 ]; then
        printf 'More than one backup set is called %s; choose it by its number or id.\n' \
               "$1" >&2
    elif [ "$_sm_n" -eq 1 ]; then
        printf '%s' "$_sm_hit"
    fi
    return 0
}

# another_pass_done: in the run that "Restore another backup set?" started
# (offer_another_set sets LCSAS_ANOTHER_PASS), Q or the end of the input
# at the set list means no other set.  The sets chosen before are
# restored, so the run ends with 0 and the tools folder goes (C5, C6), as
# restore.bat does and as Q at the folder question does.
another_pass_done() {
    [ "${LCSAS_ANOTHER_PASS:-0}" = 1 ] || return 0
    printf 'No other backup set restored.\n' >&2
    exit 0
}

# Count non-empty lines (printf "\n" | wc -l returns 1 — useless).
# `grep -c` exits 1 when nothing matches; under `set -e` that would
# kill the script, so wrap in `|| true` and let the count fall to 0.
REPO_COUNT="$(printf '%s\n' "$REPO_CANDIDATES" | grep -c '^.' || true)"
case "$REPO_COUNT" in
    0)
        cat >&2 <<EOF
no restic repository found.

The recovery script looked for a directory with keys/ and index/
subdirs under:

  - $RECOVERY/repo
  - $RECOVERY
  - $RECOVERY/metadata/*/    (the layout LCSAS uses)
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
        # Multi-repository archive.  Honour LCSAS_REPO if it matches a
        # candidate's id or name; otherwise list the sets by name and
        # ask the heir to pick one.
        #
        # REPO_CANDIDATES is newline-separated and a repository path can contain
        # spaces (e.g. a disc auto-mounted at /media/user/DISC LABEL 1), so
        # split ONLY on newlines here — default IFS word-splitting would
        # shatter such a path into bogus candidates (issue #364).  The
        # `IFS= read` below is a command-scoped assignment and does not
        # disturb this block-level IFS.
        _saved_ifs=$IFS
        IFS='
'
        REPO_NAMES=""
        for cand in $REPO_CANDIDATES; do
            _rn="$(set_label "$cand")"
            [ "$_rn" = "${cand##*/}" ] || _rn="$_rn (id ${cand##*/})"
            REPO_NAMES="$REPO_NAMES${REPO_NAMES:+, }$_rn"
        done
        if [ -n "${LCSAS_REPO:-}" ]; then
            REPO="$(set_match "$LCSAS_REPO")"
            if [ -z "$REPO" ]; then
                printf 'LCSAS_REPO=%s not among available repositories: %s\n' \
                       "$LCSAS_REPO" "$REPO_NAMES" >&2
                exit 1
            fi
        else
            while :; do
                printf 'Multiple repositories on this archive:\n' >&2
                repo_idx=0
                for cand in $REPO_CANDIDATES; do
                    repo_idx=$((repo_idx + 1))
                    # The name is untrusted text off a disc: printed as
                    # data with %s, never run or re-parsed (#606).
                    printf '  %d) %s%s\n' "$repo_idx" "$(set_label "$cand")" \
                           "$(set_split_text "$cand")" >&2
                done
                printf 'Choose a repository (number or name): ' >&2
                if ! IFS= read -r repo_choice; then
                    # The input ended: nobody chose, as good as Q (C5).
                    IFS=$_saved_ifs
                    printf '\nno repository chosen\n' >&2
                    another_pass_done
                    exit "$EXIT_USER_ABORT"
                fi
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
                # Name form: the set's id or its name.
                [ -n "$REPO" ] || REPO="$(set_match "$repo_choice")"
                [ -z "$REPO" ] || break
                case "$repo_choice" in
                    q|Q)
                        IFS=$_saved_ifs
                        another_pass_done
                        printf 'Stopped at your request.\n' >&2
                        exit "$EXIT_USER_ABORT" ;;
                esac
                printf 'no repository named %s; choose from: %s\n' \
                       "$repo_choice" "$REPO_NAMES" >&2
                if ! is_interactive; then
                    IFS=$_saved_ifs
                    exit 1
                fi
                printf 'Type its number, its name or its id (Q then ENTER to quit).\n' >&2
            done
        fi
        IFS=$_saved_ifs
        ;;
esac
# A resumed run (resume.sh sets LCSAS_RESUME=1) carries on the SAME
# restore: the set's copy in the tools folder is the one the stopped run
# used (it was brought up to date from that run's copy, below), so its
# "latest" is the same snapshot.  A newer data disc in the drive now
# must not switch it: tier 1 skips files already restored, and a second
# snapshot would leave a mix of the two.
if [ "${LCSAS_RESUME:-0}" = 1 ] && [ -n "$TOOLS" ] \
   && [ -d "$TOOLS/metadata/${REPO##*/}/keys" ] \
   && [ -d "$TOOLS/metadata/${REPO##*/}/index" ]; then
    REPO="$TOOLS/metadata/${REPO##*/}"
fi
printf '[restore.sh] using repository %s\n' "$REPO" >&2
SET_NAME="$(set_label "$REPO")"
[ "$SET_NAME" = "${REPO##*/}" ] || printf '[restore.sh] backup set: %s\n' "$SET_NAME" >&2

# ── No-recovery-binary guard (issue #225) ─────────────────────────
#
# Surface the dead-end error BEFORE we prompt for a
# password (otherwise the heir types a secret into the void) and
# BEFORE we walk the rest of the discovery code.  The message lists
# exactly which tier is missing what, includes a distinct exit code
# (64), and points at the manual-restore docs.

EXIT_NO_RECOVERY_BIN=64

# Compute paths the same way the tier-dispatch block does so the
# diagnostic shows the heir the EXACT path the script looked at.
# $TARGET is the rust-triple at this point; the caller-supplied target
# directory was renamed to $TARGET_DIR earlier.
_RESTORE_BIN_PROBE="$RECOVERY/bin/$TARGET/lcsas-restore"
_RUSTIC_BIN_PROBE="$RECOVERY/bin/$TARGET/rustic-static"

# tier3_python_runs CAND: true when CAND starts and is Python 3.8 or
# newer.  Being executable is not enough (#714): the bundled Linux
# CPython needs the musl loader (/lib/ld-musl-<arch>.so.1), which stock
# Debian, Ubuntu and Fedora lack, so there it fails with "not found".
# stdin is closed so a probed program can never eat the password line.
tier3_python_runs() {
    "$1" -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
        </dev/null >/dev/null 2>&1
}

# find_tier3_python: tier 3's interpreter.  The bundled CPython first
# (it works on hosts with no Python packaged), then python3 / python on
# PATH, taking the first that runs.  Sets TIER3_PYBIN (empty: none
# runs) and TIER3_PY_SKIPPED (a bundled interpreter that is there but
# did not run, else empty).  Used by both the probe below and the
# tier-3 dispatch, so the two always agree.
find_tier3_python() {
    TIER3_PYBIN=""
    TIER3_PY_SKIPPED=""
    for _t3_cand in \
        "$RECOVERY/bin/$TARGET/python/bin/python3" \
        "$RECOVERY/bin/$TARGET/python/python.exe" \
        "$RECOVERY/bin/$TARGET/python/bin/python"; do
        [ -x "$_t3_cand" ] || continue
        if tier3_python_runs "$_t3_cand"; then
            TIER3_PYBIN="$_t3_cand"; return 0
        fi
        [ -n "$TIER3_PY_SKIPPED" ] || TIER3_PY_SKIPPED="$_t3_cand"
    done
    for _t3_cand in python3 python; do
        command -v "$_t3_cand" >/dev/null 2>&1 || continue
        if tier3_python_runs "$_t3_cand"; then
            TIER3_PYBIN="$_t3_cand"; return 0
        fi
    done
    return 0
}

# Probe tier 3 (CPython + standalone_restorer.py) availability using
# the same search rules as the tier-3 dispatch block far below (the
# interpreter through find_tier3_python, after the tier-2b probe; the
# script list is repeated there, keep the two in lock-step).
_have_tier3_python=0
_have_tier3_script=0
_tier3_pybin_probe=""
_tier3_script_probe=""
TIER3_PY_SKIPPED=""
if [ "${LCSAS_ALLOW_PYTHON_TIER:-1}" = "1" ]; then
    for cand in "$RECOVERY/../standalone_restorer.py" \
                "$RECOVERY/standalone_restorer.py" \
                "$SCRIPT_DIR/../standalone_restorer.py"; do
        if [ -f "$cand" ]; then
            _tier3_script_probe="$cand"; _have_tier3_script=1; break
        fi
    done
fi

# Probe tier 2b (stock restic / rustic) availability -- the per-target
# bundled binary, then any restic/rustic on PATH.  Counted by presence
# (like tier 2) so the "no recovery binary" guard never fires when a
# stock reader is available to drive the restore.
_have_stdtool=0
_stdtool_probe=""
for cand in "$RECOVERY/bin/$TARGET/restic" "$RECOVERY/bin/$TARGET/restic.exe"; do
    if [ -x "$cand" ]; then _stdtool_probe="$cand"; _have_stdtool=1; break; fi
done
if [ "$_have_stdtool" = "0" ]; then
    for c in restic rustic; do
        if command -v "$c" >/dev/null 2>&1; then
            _stdtool_probe="$(command -v "$c")"; _have_stdtool=1; break
        fi
    done
fi

# Tier 3's interpreter is started only when no other tier is here, so
# the guard below depends on it: a restore that tier 1 can do never
# starts Python.
if [ "${LCSAS_ALLOW_PYTHON_TIER:-1}" = "1" ] \
   && [ ! -x "$_RESTORE_BIN_PROBE" ] && [ ! -x "$_RUSTIC_BIN_PROBE" ] \
   && [ "$_have_stdtool" = "0" ]; then
    find_tier3_python
    if [ -n "$TIER3_PYBIN" ]; then
        _tier3_pybin_probe="$TIER3_PYBIN"; _have_tier3_python=1
    fi
fi

if [ ! -x "$_RESTORE_BIN_PROBE" ] \
   && [ ! -x "$_RUSTIC_BIN_PROBE" ] \
   && [ "$_have_stdtool" = "0" ] \
   && { [ "$_have_tier3_python" = "0" ] || [ "$_have_tier3_script" = "0" ]; }
then
    if [ -s "$_RESTORE_BIN_PROBE" ] || [ -s "$_RUSTIC_BIN_PROBE" ]; then
        {
            # #496: the binaries are here but this computer won't run
            # them from this folder (FAT/exFAT copy without executable
            # bits, or a noexec mount, with no runnable copy possible).
            printf 'ERROR: the recovery programs are on this disc, but this computer\n'
            printf 'will not run them from %s\n' "$RECOVERY/bin/$TARGET"
            printf '(a USB-stick copy without "executable" permissions, or a folder\n'
            printf 'mounted "noexec").  The disc itself is fine.  Copy the disc to\n'
            printf 'your home folder and run the copy:\n\n'
            printf '    cp -R <this disc> ~/lcsas-meta\n'
            printf '    chmod -R u+x ~/lcsas-meta/recovery/bin\n'
            printf '    sh ~/lcsas-meta/restore.sh <target folder>\n\n'
            printf 'Exit code %d = no_recovery_binary_available.\n' \
                   "$EXIT_NO_RECOVERY_BIN"
        } >&2
        exit "$EXIT_NO_RECOVERY_BIN"
    fi
    {
        printf 'ERROR: meta disc lacks all recovery binaries -- check disc integrity.\n'
        printf '\n'
        printf 'Missing components (no tier is runnable on this host):\n'
        printf '  tier 1: %s -- not executable\n' "$_RESTORE_BIN_PROBE"
        printf '  tier 2: %s -- not executable\n' "$_RUSTIC_BIN_PROBE"
        printf '  tier 2b: no stock restic/rustic (bundled or on PATH)\n'
        if [ "${LCSAS_ALLOW_PYTHON_TIER:-1}" != "1" ]; then
            printf '  tier 3: disabled by LCSAS_ALLOW_PYTHON_TIER=0\n'
        else
            if [ "$_have_tier3_python" = "0" ] && [ -n "$TIER3_PY_SKIPPED" ]; then
                printf '  tier 3: the bundled Python %s cannot run on this host\n' \
                       "$TIER3_PY_SKIPPED"
                printf '          (missing musl loader?), and no working python3 on PATH\n'
            elif [ "$_have_tier3_python" = "0" ]; then
                printf '  tier 3: no working python3 on PATH and no bundled CPython at '
                printf '%s/bin/%s/python/\n' "$RECOVERY" "$TARGET"
            fi
            if [ "$_have_tier3_script" = "0" ]; then
                printf '  tier 3: standalone_restorer.py not found near '
                printf '%s\n' "$RECOVERY"
            fi
        fi
        printf '\n'
        printf 'This meta disc cannot be used to restore.  Try another\n'
        printf 'meta disc: the newest one first, then the one burned\n'
        printf 'closest in date to the data discs.  If none works, see\n'
        printf '%s/docs/RECOVER.txt for manual restore instructions.\n' "$RECOVERY"
        printf '\n'
        printf 'Exit code %d = no_recovery_binary_available.\n' \
               "$EXIT_NO_RECOVERY_BIN"
    } >&2
    exit "$EXIT_NO_RECOVERY_BIN"
fi

# (NL, the separator of the tier argument lists below, is set with the
# discovery helpers above.)

# Optional --catalog if a catalog.db is present somewhere reachable.
# Used for human-readable volume hints in prompts, and by the free-
# space preflight below [FMA-09] (which is why this block runs BEFORE
# the password prompt).
#
# The meta disc deliberately carries NO catalog.db (it would always be
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
# Local recovery-tree candidates (last resort).  The copy an earlier
# run left in the tools folder comes first: a resumed run with no disc
# in the drive still names the discs in its prompts.
for cand in ${STATE_DIR:+"$STATE_DIR/catalog.db"} "$RECOVERY/catalog.db" \
            "$REPO/catalog.db" "$RECOVERY/../catalog.db"; do
    catalog_consider "$cand"
done
# Mounted discs: the same LCSAS_MOUNT_DIRS walk as repo discovery and
# the pack search, the mount dir itself included (a disc mounted AT
# /mnt, as `sudo mount /dev/sr0 /mnt` does, #412).  This used its own
# fixed list (/Volumes/* /media/$USER/* /media/* /mnt/*), so it missed
# /run/media/$USER/* (udisks2 automounts on Fedora, Arch, ...) and a
# disc at /mnt itself: no catalog, so no disc-swap hints and no size
# for the free-space preflight.
OLD_IFS="$IFS"; IFS=":"
for parent in $LCSAS_MOUNT_DIRS_EFFECTIVE; do
    IFS="$OLD_IFS"
    if [ -z "$parent" ] || [ ! -d "$parent" ]; then IFS=":"; continue; fi
    for mnt in "$parent" "$parent"/*; do
        [ -d "$mnt" ] || continue
        catalog_consider "$mnt/catalog.db"
    done
    IFS=":"
done
IFS="$OLD_IFS"
# #610: tier 1 keeps its --catalog file open for the whole run, so a
# catalog.db on a disc held that disc in the drive.  Tier 1 gets a COPY
# in the tools folder instead, never a path on a disc.  cp -p keeps the
# mtime: tier 1 never swaps to a catalog older than this one, and the
# stale-locator check below compares it.
catalog_local=""
if [ -n "$catalog_pick" ] && [ -n "$STATE_DIR" ]; then
    catalog_local="$(cd "$STATE_DIR" && pwd -P)/catalog.db"
    _cat_src="$(cd "$(dirname "$catalog_pick")" && pwd -P)/$(basename "$catalog_pick")"
    if [ "$_cat_src" != "$catalog_local" ]; then
        _cat_kb="$(du -sk "$catalog_pick" 2>/dev/null | awk '{print $1}')"
        printf '[lcsas-restore] copying the catalog (about %s) from %s\n' \
               "$(human_kb "${_cat_kb:-0}")" "$catalog_pick" >&2
        rm -f "$catalog_local"
        cp -p "$catalog_pick" "$catalog_local" 2>/dev/null \
            || { rm -f "$catalog_local"; catalog_local=""; }
    fi
fi
if [ -n "$catalog_local" ]; then
    CATALOG_ARG="--catalog$NL$catalog_local"
    printf '[lcsas-restore] using catalog %s (a copy in %s)\n' "$catalog_pick" "$STATE_DIR" >&2
elif [ -n "$catalog_pick" ]; then
    printf '[lcsas-restore] could not copy the catalog %s, so the\n' "$catalog_pick" >&2
    printf '                disc prompts will not name the discs.\n' >&2
fi

# If the heir is using a persistent (non-auto) pack cache, the
# locator-catalog cached there may be stale when the source catalog
# advances (e.g. a new data disc was added to the archive).  Delete
# the stale copy so the binary re-derives it from the fresh catalog.
if [ -n "${LCSAS_PACK_CACHE_DIR:-}" ] && [ -n "$catalog_pick" ]; then
    _loc_cache="$LCSAS_PACK_CACHE_DIR/.locator-catalog.db"
    if [ -f "$_loc_cache" ]; then
        _loc_mt="$(stat -c '%Y' "$_loc_cache" 2>/dev/null \
            || stat -f '%m' "$_loc_cache" 2>/dev/null \
            || echo 0)"
        if [ "$catalog_pick_mtime" -gt "$_loc_mt" ] 2>/dev/null; then
            printf '[lcsas-restore] catalog advanced; discarding stale locator cache\n' >&2
            rm -f "$_loc_cache"
        fi
    fi
fi

# ── Free-space preflight [FMA-09] ─────────────────────────────────
#
# The burn side checks staging-area space before writing a byte; mirror
# that on the restore side BEFORE the password / disc prompts so a
# too-small target fails in one obvious line, not via ENOSPC after a
# long disc-swapping session.  Required bytes come from the catalog:
# the exact per-repository pack sum when a sqlite3 CLI is on PATH, else
# the tier-1 binary's --list-pending-packs total (a slight over-
# estimate when packs live on several discs).  When neither tool can
# run, the check is SKIPPED -- never block a restore on a missing
# convenience tool.  The pack cache needs about that size too: packs
# accumulate in it while the restore writes the target.  When the
# target and the cache share a filesystem (the default: both under
# the home folder) that filesystem needs the SUM of the two (#660);
# on two filesystems each needs its own share.  A restore that carries
# on counts what it already wrote: the packs in the cache, and the
# files in a target that holds the marker of an earlier restore.
# LCSAS_SKIP_SPACE_CHECK=1 bypasses the whole check.

space_probe_dir() {
    # Print the nearest EXISTING ancestor directory of "$1" (df needs
    # a path that exists; targets/caches often don't yet).
    _sp_probe="$1"
    while [ -n "$_sp_probe" ] && [ ! -d "$_sp_probe" ] \
          && [ "$_sp_probe" != "/" ] && [ "$_sp_probe" != "." ]; do
        _sp_probe="$(dirname "$_sp_probe")"
    done
    [ -d "$_sp_probe" ] && printf '%s\n' "$_sp_probe"
    return 0
}

free_kb_for() {
    # Available KB on the filesystem holding "$1"; empty if unknown.
    _fk_dir="$(space_probe_dir "$1")"
    [ -n "$_fk_dir" ] || return 0
    df -Pk "$_fk_dir" 2>/dev/null | awk 'NR==2 { print $4 }'
    return 0
}

fs_dev_for() {
    # df mount point of "$1": the target and the cache are on the same
    # filesystem when df names the same mount point for both.  Not the
    # device column, which is "tmpfs" for every tmpfs (#660).
    _fd_dir="$(space_probe_dir "$1")"
    [ -n "$_fd_dir" ] || return 0
    df -Pk "$_fd_dir" 2>/dev/null \
        | awk 'NR==2 { for (i = 1; i <= 5; i++) sub(/^[^ ]+ +/, ""); print }'
    return 0
}

used_kb_in() {
    # KB already used under "$1" (0 when it does not exist).
    _uk=0
    if [ -d "$1" ]; then
        _uk="$(du -sk "$1" 2>/dev/null | awk 'NR==1 { print $1 }')"
        case "$_uk" in ''|*[!0-9]*) _uk=0 ;; esac
    fi
    printf '%s\n' "$_uk"
}

still_needed_kb() {
    # required_kb less the "$1" KB already there, never below 0.
    if [ "$1" -ge "$required_kb" ]; then echo 0
    else echo $(( required_kb - $1 )); fi
}

space_short() {
    # space_short DIR NEED_KB: say so and set space_fail when the
    # filesystem holding DIR has less than NEED_KB free.  Returns 1
    # when it is short.  Free space that cannot be read is not short.
    _ss_free="$(free_kb_for "$1")"
    case "$_ss_free" in ''|*[!0-9]*) return 0 ;; esac
    [ "$_ss_free" -lt "$2" ] || return 0
    printf 'restore.sh: Need about %s free at %s; only %s available.\n' \
           "$(human_kb "$2")" "$1" "$(human_kb "$_ss_free")" >&2
    space_fail=1
    return 1
}

if [ "${LCSAS_SKIP_SPACE_CHECK:-0}" != "1" ]; then
    required_kb=0
    _tenant="$(basename "$REPO")"
    if [ -n "$catalog_pick" ] && command -v sqlite3 >/dev/null 2>&1; then
        _req_bytes="$(sqlite3 "${catalog_local:-$catalog_pick}" \
            "SELECT COALESCE(SUM(p.size_bytes), 0) FROM packs p \
             JOIN repositories r ON r.repo_id = p.repo_id \
             WHERE (r.name = '$_tenant' OR r.repo_id = '$_tenant') \
               AND p.is_pruned = 0;" 2>/dev/null || true)"
        case "$_req_bytes" in
            ''|*[!0-9]*) ;;
            *) required_kb=$(( ($_req_bytes + 1023) / 1024 )) ;;
        esac
    fi
    if [ "$required_kb" -eq 0 ] && [ -n "$catalog_pick" ] \
       && [ -x "$_RESTORE_BIN_PROBE" ]; then
        # Parse "Total: N packs, X.X MB across M discs." -> X.X
        # (output captured -- must not leak onto the heir's tty).
        # Tier 1's argument check wants --repo and --password-file even
        # for --list-pending-packs, which reads neither (it exits before
        # the key load); without them it printed usage, exited 2, and
        # this fallback never produced a size (#535).  The password has
        # not been asked for yet, so pass /dev/null.
        _req_mb="$("$_RESTORE_BIN_PROBE" --repo "$REPO" \
                       --password-file /dev/null --catalog "${catalog_local:-$catalog_pick}" \
                       --list-pending-packs 2>/dev/null \
                   | awk '/^Total:/ {
                         for (i = 1; i < NF; i++)
                             if ($(i+1) == "MB") { print $i; exit }
                     }' || true)"
        case "$_req_mb" in
            ''|*[!0-9.]*) ;;
            *) required_kb="$(awk -v mb="$_req_mb" \
                   'BEGIN { printf "%d", mb * 1024 }')" ;;
        esac
    fi
    if [ "$required_kb" -gt 0 ]; then
        mkdir -p "$TARGET_DIR" 2>/dev/null || true
        space_fail=0
        # What each still needs: the restored files (less those an
        # earlier restore into this folder already wrote) and the pack
        # cache (less the packs already in it).
        _sp_t_used=0
        if [ -f "$TARGET_DIR/.lcsas-restore-marker" ]; then
            _sp_t_used="$(used_kb_in "$TARGET_DIR")"
        fi
        _sp_t_need="$(still_needed_kb "$_sp_t_used")"
        _sp_c_dir="${LCSAS_PACK_CACHE_DIR:-}"
        _sp_c_need=0
        if [ -n "$_sp_c_dir" ]; then
            _sp_c_need="$(still_needed_kb "$(used_kb_in "$_sp_c_dir")")"
        fi
        _sp_t_dev="$(fs_dev_for "$TARGET_DIR")"
        _sp_c_dev=""
        [ -z "$_sp_c_dir" ] || _sp_c_dev="$(fs_dev_for "$_sp_c_dir")"
        if [ -n "$_sp_c_dir" ] && [ -n "$_sp_t_dev" ] \
           && [ "$_sp_t_dev" = "$_sp_c_dev" ]; then
            # One filesystem holds both: it needs the sum (#660).
            if ! space_short "$TARGET_DIR" "$(( _sp_t_need + _sp_c_need ))"; then
                printf '    (%s for the restored files and %s for the pack\n' \
                       "$(human_kb "$_sp_t_need")" "$(human_kb "$_sp_c_need")" >&2
                printf '    cache at %s, on the same disk.)\n' "$_sp_c_dir" >&2
            fi
        else
            space_short "$TARGET_DIR" "$_sp_t_need" || true
            if [ -n "$_sp_c_dir" ]; then
                space_short "$_sp_c_dir" "$_sp_c_need" || true
            fi
        fi
        if [ "$space_fail" = "1" ]; then
            printf 'Free space or choose a different --target, then re-run.\n' >&2
            printf '(set LCSAS_SKIP_SPACE_CHECK=1 to bypass this check.)\n' >&2
            exit 1
        fi
    fi
fi

# ── Non-empty-target guard [UX-07] ────────────────────────────────
#
# Writing into a folder that already holds the heir's OWN files (same
# names) silently overwrites live data with decades-old archive
# content -- the one place in the journey where a confused heir
# loses CURRENT data rather than just failing a restore.  Warn here,
# BEFORE the password prompt, so the heir is stopped before typing a
# secret.
#
# A hard refusal would be wrong: re-running into the SAME target is the
# supported way to resume an interrupted restore (RECOVER.txt RETRY
# SAFETY -- idempotent resume).  So we warn only when the target is
# non-empty AND lacks our marker file.  After confirmation/creation we
# drop a hidden marker, so every subsequent re-run is silent.  The
# marker names the backup sets restored into the folder, one id a line:
# another set restored on top of one would keep (same size) or overwrite
# (other size) the first set's files with no word, so a folder that
# holds ANOTHER set's restore is warned about too.  An empty marker (an
# older restore.sh, or restore.bat) names no set and stays silent.
# (EXIT_USER_ABORT, 65, is set with the discovery helpers above.)
SET_ID="${REPO##*/}"
MARKER="$TARGET_DIR/.lcsas-restore-marker"

# The tools folder (or this run's temporary one) is deleted when the
# restore is done, so the restored files must not go inside it (C6).
_tg_probe="$(space_probe_dir "$TARGET_DIR")"
if [ -n "$STATE_DIR" ] && [ -n "$_tg_probe" ] && path_inside "$_tg_probe" "$STATE_DIR"; then
    {
        printf 'restore.sh: the folder to restore into,\n'
        printf '    %s\n' "$TARGET_DIR"
        printf 'is inside the tools folder %s,\n' "$STATE_DIR"
        printf 'which is deleted when the restore is done.  Choose a folder\n'
        printf 'outside it, for example %s/restored.\n' "$(dirname "$STATE_DIR")"
    } >&2
    exit "$EXIT_USER_ABORT"
fi
unset _tg_probe

_tg_other=0
if [ -s "$MARKER" ] && ! grep -qxF -- "$SET_ID" "$MARKER" 2>/dev/null; then
    _tg_other=1
fi
# Only files other than the marker count: a folder emptied but for it
# (rm -rf restored/* leaves the dot-file) has nothing to overwrite.
# Globs, not `ls -A | grep` (SC2010); -L also counts a dangling link.
holds_more_than_marker() {
    for _hm in "$1"/* "$1"/.[!.]* "$1"/..?*; do
        [ -e "$_hm" ] || [ -L "$_hm" ] || continue
        [ "${_hm##*/}" = .lcsas-restore-marker ] && continue
        return 0
    done
    return 1
}
if [ -d "$TARGET_DIR" ] \
   && holds_more_than_marker "$TARGET_DIR" \
   && { [ ! -f "$MARKER" ] || [ "$_tg_other" = 1 ]; } \
   && [ "${LCSAS_FORCE_NONEMPTY_TARGET:-0}" != "1" ]; then
    {
        printf '==========================================================\n'
        printf '  WARNING: the folder\n'
        printf '      %s\n' "$TARGET_DIR"
        if [ "$_tg_other" = 1 ]; then
            printf '  already holds the restore of ANOTHER backup set.  Files\n'
            printf '  of %s with the same names as that set'"'"'s\n' "$SET_NAME"
            printf '  would be mixed in with them: some OVERWRITE them, and\n'
            printf '  some are skipped as already restored.\n'
            printf '\n'
            printf '  Safest choice: stop, and restore this set into a NEW,\n'
            printf '  empty folder instead.\n'
        else
            printf '  already contains files that do not look like a previous\n'
            printf '  LCSAS restore.  Restored files with the same names will\n'
            printf '  OVERWRITE what is there now.\n'
            printf '\n'
            printf '  Safest choice: stop, and restore into a NEW, empty\n'
            printf '  folder instead.\n'
        fi
        printf '==========================================================\n'
    } >&2
    if [ -t 0 ]; then
        printf 'Type YES to overwrite, anything else to stop: ' >&2
        IFS= read -r ans
        if [ "$ans" != "YES" ]; then
            printf 'Aborting; nothing was written.\n' >&2
            exit "$EXIT_USER_ABORT"
        fi
    else
        printf 'No terminal to confirm on; refusing to overwrite.\n' >&2
        printf 'Set LCSAS_FORCE_NONEMPTY_TARGET=1 to proceed non-interactively.\n' >&2
        exit "$EXIT_USER_ABORT"
    fi
fi
unset _tg_other
mkdir -p "$TARGET_DIR"
if [ ! -s "$MARKER" ] || ! grep -qxF -- "$SET_ID" "$MARKER" 2>/dev/null; then
    printf '%s\n' "$SET_ID" >> "$MARKER" 2>/dev/null || true
fi
# The target as a full path: resume.sh and the next set need it from
# any folder (tier 1 runs from / when the META disc is known).
TARGET_ABS="$(cd "$TARGET_DIR" && pwd)"

# ── resume.sh (#609) ──────────────────────────────────────────────
#
# The set and the target are known now, so write <tools>/resume.sh: it
# runs this restore again from the tools folder, with no META disc and
# no question but the password.  Tier 1 skips every file that is
# already complete (#92) and the pack cache keeps the discs already
# read.  resume.sh NEVER holds the password.
if [ -n "$TOOLS" ]; then
    # The chosen set's copy in the tools folder is brought to match the
    # one used (merge_set_metadata, above; it is that copy when
    # relocated).  When it does, resume.sh carries on with that copy
    # (LCSAS_RESUME=1), so "latest" stays the same snapshot whatever
    # disc is in the drive then.
    _rs_frozen=0
    case "$(basename "$(dirname "$REPO")")" in
        metadata)
            if merge_set_metadata "$REPO" "$(disc_label "$(dirname "$(dirname "$REPO")")")"; then
                _rs_frozen=1
            fi ;;
    esac
    _rs_env="" _rs_set="" _rs_val=""
    for _rs_var in LCSAS_RELOCATED LCSAS_META_DISC LCSAS_MOUNT_DIRS \
                   LCSAS_PACK_CACHE_DIR LCSAS_TARGET LCSAS_INTERACTIVE \
                   LCSAS_TIER_FALLBACK LCSAS_ANOTHER_SET; do
        eval "_rs_set=\${$_rs_var+x}"
        [ -n "$_rs_set" ] || continue
        eval "_rs_val=\$$_rs_var"
        _rs_env="$_rs_env$_rs_var=$(sh_quote "$_rs_val"); export $_rs_var$NL"
    done
    if [ "$_rs_frozen" = 1 ]; then
        _rs_env="${_rs_env}LCSAS_RESUME=1; export LCSAS_RESUME$NL"
    fi
    unset _rs_frozen
    # LCSAS_NEWEST_DISC_PROMPT=0: the tools folder already holds the
    # newest disc's copy of the sets, if this run saw one.
    cat > "$TOOLS/resume.sh" <<EOF
#!/bin/sh
# Carries on the LCSAS restore that stopped.  Written by restore.sh;
# it holds no password (the restore asks for it again).  Flags such as
# --key FILE are passed on.
${_rs_env}LCSAS_TOOLS_DIR=$(sh_quote "$TOOLS"); export LCSAS_TOOLS_DIR
LCSAS_REPO=$(sh_quote "$(basename "$REPO")"); export LCSAS_REPO
LCSAS_NEWEST_DISC_PROMPT=0; export LCSAS_NEWEST_DISC_PROMPT
exec sh $(sh_quote "$SCRIPT") "\$@" $(sh_quote "$RECOVERY") $(sh_quote "$TARGET_ABS") $(sh_quote "$SNAP")
EOF
    chmod 700 "$TOOLS/resume.sh"
fi

# ── The password (now that we know which backup set) ──────────────
#
# In this order: LCSAS_PWFILE (or --key FILE), LCSAS_PASSWORD, the share
# card files in LCSAS_SHARES_DIR, and otherwise ask (#607, #608): for the
# share cards when the set's marker says its password is split, else
# for the password.  An empty answer asks again, and Q (or the end of
# the input) stops with exit 77.  Asked on a terminal, a password that a
# repository key rejects is caught before tier 1 starts and asked for
# again (C7); a password given in the environment or a file is not
# asked again.

# bin_preflight_ok PATH -- see "Issue #223" at the tier dispatch below:
# 0 iff PATH is non-empty and a `--help` dry-run does not fail with an
# exec-format error (rc 126/127).
bin_preflight_ok() {
    [ -s "$1" ] || return 1
    "$1" --help >/dev/null 2>&1
    case $? in
        126|127) return 1 ;;
    esac
    return 0
}

# The share card combiner for this computer (lcsas-keyshare).  Checked
# like the tier binaries (bin_preflight_ok): a zero-byte (bit-rotted)
# file would run as an empty script, exit 0 and "accept" every card.
KEYSHARE_BIN=""
for _ks in "$RECOVERY/bin/$TARGET/lcsas-keyshare" \
           "$RECOVERY/bin/$TARGET/lcsas-keyshare.exe"; do
    if [ -x "$_ks" ] && bin_preflight_ok "$_ks"; then KEYSHARE_BIN="$_ks"; break; fi
done

# keyshare_broken RC: lcsas-keyshare gave an answer it never gives (exit
# RC, or no password at all), so it does not work on this computer: the
# cards are not to blame.
keyshare_broken() {
    printf 'The share combiner %s does not work on\n' "$KEYSHARE_BIN" >&2
    printf 'this computer (exit %s); the cards are not to blame.  Rebuild the\n' "$1" >&2
    printf 'password as RECOVER.txt "SHARE CARDS" says, then type it.\n' >&2
}

# quit_password: the reader typed Q, or the input ended, at the password
# or share card prompt.
quit_password() {
    printf 'Stopped at your request; no password was given.\n' >&2
    exit 77
}

# read_hidden VAR PROMPT: read one line into VAR with the terminal's echo
# off (#363), so the password is not shown on screen or left in the
# scrollback.  Not a terminal (stty -g fails): an ordinary read.
read_hidden() {
    _stty_saved="$(stty -g 2>/dev/null)" || _stty_saved=""
    [ -z "$_stty_saved" ] || stty -echo 2>/dev/null || true
    # The prompt after echo is off: a fast typist's first keys are hidden.
    printf '%s' "$2" >&2
    _rh_rc=0
    IFS= read -r "$1" || _rh_rc=$?
    if [ -n "$_stty_saved" ]; then
        stty "$_stty_saved" 2>/dev/null || true
        _stty_saved=""
        printf '\n' >&2
    fi
    return "$_rh_rc"
}

# password_ok: 1 when a repository key of the set rejects the password
# in $PWFILE.  Tier 1 lists the snapshots, which opens the keys first
# and exits 77 on a wrong password (main.c).  Only asked on a terminal,
# where the password can be asked for again, and only when tier 1 runs
# here; any other answer is left for the restore itself to report.
password_ok() {
    is_interactive || return 0
    [ -x "$_RESTORE_BIN_PROBE" ] && bin_preflight_ok "$_RESTORE_BIN_PROBE" || return 0
    printf '[restore.sh] checking the password...\n' >&2
    _po_rc=0
    LCSAS_PACK_CACHE_DIR='' "$_RESTORE_BIN_PROBE" --repo "$REPO" \
        --password-file "$PWFILE" --list-snapshots >/dev/null 2>&1 || _po_rc=$?
    [ "$_po_rc" -ne 77 ]
}

# ask_password: the password prompt, until a password is accepted.
ask_password() {
    printf 'Type the password for %s, then ENTER (Q then ENTER to quit).\n' \
           "$SET_NAME" >&2
    while :; do
        _ap_pw=""
        read_hidden _ap_pw 'Password: ' || quit_password
        case "$_ap_pw" in
            '')
                printf 'No password typed.  Type the password, or Q then ENTER to quit.\n' >&2
                continue ;;
            q|Q) quit_password ;;
        esac
        printf '%s\n' "$_ap_pw" > "$PWFILE"
        password_ok && return 0
        printf 'That password was not accepted for %s.\n' "$SET_NAME" >&2
        printf 'Type it again (check the letters and the caps lock), or Q then ENTER to quit.\n' >&2
    done
}

# drop_shares: remove the typed share cards (they are also removed on
# any exit).
drop_shares() {
    [ -z "$SHARE_DIR" ] || rm -rf "$SHARE_DIR"
    SHARE_DIR=""
}

# ask_shares K N: the share card prompts (C7).  Each card is typed on one
# line, checked alone with lcsas-keyshare (a typo is named and that card
# asked again), and kept in its own file in a private temporary folder,
# never the tools folder.  Exactly K cards are combined into $PWFILE.
# Returns 1 when the reader types P: they will type the password.
ask_shares() {
    _as_k="${1% *}"
    _as_n="${1#* }"
    _as_sid="$(set_field "$REPO" split_id | tr -cd '0-9')"
    printf '\nThe password for %s is split into %s share cards;\n' \
           "$SET_NAME" "$_as_n" >&2
    printf 'you need any %s of them%s.\n' "$_as_k" "${_as_sid:+ (Split ID $_as_sid)}" >&2
    printf 'Type the words of each card on one line; they show as you type.\n' >&2
    printf 'P then ENTER instead: type the password itself.\n' >&2
    while :; do
        drop_shares
        SHARE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/lcsas-shares.XXXXXX" 2>/dev/null || true)"
        if [ -z "$SHARE_DIR" ] || ! chmod 700 "$SHARE_DIR"; then
            SHARE_DIR=""
            printf 'Cannot make a private temporary folder in %s for the cards;\n' \
                   "${TMPDIR:-/tmp}" >&2
            printf 'rebuild the password first (see RECOVER.txt) and type it.\n' >&2
            return 1
        fi
        _as_i=1
        set --   # the card files typed so far
        while [ "$_as_i" -le "$_as_k" ]; do
            printf 'Share card %d of %d: type all the words on one line, then ENTER (or Q to quit).\n> ' \
                   "$_as_i" "$_as_k" >&2
            _as_line=""
            IFS= read -r _as_line || quit_password
            case "$_as_line" in
                q|Q) quit_password ;;
                p|P) drop_shares; return 1 ;;
                *[!' 	']*) ;;
                *)
                    printf 'No words typed.\n' >&2
                    continue ;;
            esac
            _as_f="$(mktemp "$SHARE_DIR/share.XXXXXX")"
            chmod 600 "$_as_f"
            printf '%s\n' "$_as_line" > "$_as_f"
            # The same card twice counts once, so the cards could not
            # be combined; say so now.
            _as_norm="$(printf '%s\n' "$_as_line" | tr '[:upper:]' '[:lower:]' | tr -s ' 	' '  ')"
            _as_dup=0
            for _as_g in "$@"; do
                if [ "$(tr '[:upper:]' '[:lower:]' < "$_as_g" | tr -s ' 	' '  ')" = "$_as_norm" ]; then
                    _as_dup=1
                fi
            done
            if [ "$_as_dup" = 1 ]; then
                rm -f "$_as_f"
                printf 'That is a card you already typed.  Type a different card.\n' >&2
                continue
            fi
            _as_rc=0
            "$KEYSHARE_BIN" "$_as_f" >/dev/null 2>"$SHARE_DIR/check.err" || _as_rc=$?
            case "$_as_rc" in
                0|1)
                    # Valid on its own (1: it cannot rebuild the password
                    # alone, as expected below K).
                    set -- "$@" "$_as_f"
                    _as_i=$((_as_i + 1)) ;;
                3)
                    # lcsas-keyshare names the bad word; the file it
                    # names is this card.
                    SC_FILE="$_as_f" SC_CARD="card $_as_i" awk '{
                        f = ENVIRON["SC_FILE"]; i = index($0, f)
                        if (i) $0 = substr($0, 1, i - 1) ENVIRON["SC_CARD"] substr($0, i + length(f))
                        print "  " $0 }' "$SHARE_DIR/check.err" >&2
                    rm -f "$_as_f"
                    printf 'That card has a mistake (see above).  Type card %d again.\n' \
                           "$_as_i" >&2 ;;
                *)
                    # 2 (usage), 126/127 (cannot run), a crash: never a
                    # card's fault.
                    drop_shares
                    keyshare_broken "$_as_rc"
                    return 1 ;;
            esac
        done
        _as_rc=0
        "$KEYSHARE_BIN" "$@" > "$PWFILE" 2>"$SHARE_DIR/check.err" || _as_rc=$?
        if [ "$_as_rc" -eq 0 ] && [ ! -s "$PWFILE" ]; then
            _as_rc="0, no password"
        fi
        case "$_as_rc" in
            0)
                drop_shares
                password_ok && return 0
                printf 'The password rebuilt from these cards was not accepted for %s.\n' \
                       "$SET_NAME" >&2
                printf 'The cards may belong to another backup set: compare the Split ID.\n' >&2 ;;
            1|3)
                sed 's/^/  /' "$SHARE_DIR/check.err" >&2
                printf 'These cards could not be combined: they may come from different\n' >&2
                printf 'splits (compare the Split ID on each card).\n' >&2 ;;
            *)
                drop_shares
                keyshare_broken "$_as_rc"
                return 1 ;;
        esac
        printf 'Start again with card 1, or Q then ENTER to quit.\n' >&2
    done
}

# shares_from_dir: rebuild the password from every card file in
# LCSAS_SHARES_DIR, with no questions.  1 when it cannot.
shares_from_dir() {
    if [ ! -d "$LCSAS_SHARES_DIR" ]; then
        printf 'LCSAS_SHARES_DIR=%s is not a folder.\n' "$LCSAS_SHARES_DIR" >&2
        return 1
    fi
    if [ -z "$KEYSHARE_BIN" ]; then
        printf 'LCSAS_SHARES_DIR is set, but there is no share combiner here\n' >&2
        printf '(%s/bin/%s/lcsas-keyshare).\n' "$RECOVERY" "$TARGET" >&2
        return 1
    fi
    set --
    for _sd_f in "$LCSAS_SHARES_DIR"/*; do
        if [ -f "$_sd_f" ]; then set -- "$@" "$_sd_f"; fi
    done
    if [ $# -eq 0 ]; then
        printf 'LCSAS_SHARES_DIR=%s holds no card files.\n' "$LCSAS_SHARES_DIR" >&2
        return 1
    fi
    printf '[restore.sh] rebuilding the password for %s from the %d card files in %s\n' \
           "$SET_NAME" $# "$LCSAS_SHARES_DIR" >&2
    _sd_rc=0
    "$KEYSHARE_BIN" "$@" > "$PWFILE" || _sd_rc=$?
    if [ "$_sd_rc" -eq 3 ]; then
        # A card file has a mistake.  Typed, that card would be asked for
        # again; here it is left out, and the others are combined if
        # enough of them are left.
        _sd_n=$#
        for _sd_f in "$@"; do
            shift
            _sd_one=0
            "$KEYSHARE_BIN" "$_sd_f" >/dev/null 2>&1 || _sd_one=$?
            case "$_sd_one" in
                0|1) set -- "$@" "$_sd_f" ;;
                3)   printf '[restore.sh] leaving out %s: it has a mistake (see above)\n' \
                            "$_sd_f" >&2 ;;
                *)   keyshare_broken "$_sd_one"
                     return 1 ;;
            esac
        done
        if [ $# -gt 0 ] && [ $# -lt "$_sd_n" ]; then
            printf '[restore.sh] combining the other %d card files\n' $# >&2
            _sd_rc=0
            "$KEYSHARE_BIN" "$@" > "$PWFILE" || _sd_rc=$?
        fi
    fi
    if [ "$_sd_rc" -eq 0 ]; then
        if [ -s "$PWFILE" ]; then return 0; fi
        keyshare_broken "0, no password"
        return 1
    fi
    case "$_sd_rc" in
        1|3)
            printf 'Could not rebuild the password from the cards in %s\n' "$LCSAS_SHARES_DIR" >&2
            printf '(lcsas-keyshare exit %d: see above).\n' "$_sd_rc" >&2 ;;
        *)  keyshare_broken "$_sd_rc" ;;
    esac
    return 1
}

PWFILE="${LCSAS_PWFILE:-}"
if [ -z "$PWFILE" ]; then
    PWFILE_TMP="$(mktemp /tmp/lcsas-pw.XXXXXX)"
    chmod 600 "$PWFILE_TMP"
    PWFILE="$PWFILE_TMP"
    _pw_split="$(set_split "$REPO")"
    if [ -n "${LCSAS_PASSWORD:-}" ]; then
        printf '%s\n' "$LCSAS_PASSWORD" > "$PWFILE"
    elif [ -n "${LCSAS_SHARES_DIR:-}" ]; then
        shares_from_dir || exit 77
    elif [ -n "$_pw_split" ] && [ -n "$KEYSHARE_BIN" ]; then
        ask_shares "$_pw_split" || ask_password
    else
        if [ -n "$_pw_split" ]; then
            printf 'The password for %s is split into share cards, but the share\n' "$SET_NAME" >&2
            printf 'combiner (lcsas-keyshare) is missing here.  Rebuild the password\n' >&2
            printf 'as RECOVER.txt "SHARE CARDS" says, then type it.\n' >&2
        fi
        ask_password
    fi
fi

mkdir -p "$TARGET_DIR"

# ── RAM-backed-target guard [BOOT-05] ─────────────────────────────
#
# On many mainstream distros (Fedora, Arch, openSUSE) and on nearly
# every live-USB session, /tmp -- and a live system's home directory
# -- is a RAM-backed tmpfs.  The documented default target is
# /tmp/restored, so an heir who accepts it gets a "successful"
# restore that evaporates at poweroff.  Warn loudly; when a human is
# reachable (TTY), default to aborting.  Non-interactive automation
# keeps working (warn-and-continue) unless LCSAS_FORBID_TMPFS_TARGET=1.

# (target_fstype is defined near the top, before relocation uses it.)

_tgt_fstype="$(target_fstype "$TARGET_DIR")"
case "$_tgt_fstype" in
tmpfs|ramfs)
    {
        printf '==========================================================\n'
        printf '  WARNING: the restore target\n'
        printf '      %s\n' "$TARGET_DIR"
        printf '  is on a RAM-backed filesystem (%s).  Restored files\n' "$_tgt_fstype"
        printf '  WILL BE LOST when this computer powers off or reboots.\n'
        printf '  Choose a real disk instead, e.g.:\n'
        printf '      sh restore.sh /media/<your-usb-drive>/restored latest\n'
        printf '==========================================================\n'
    } >&2
    if [ "${LCSAS_FORBID_TMPFS_TARGET:-0}" = "1" ]; then
        printf 'aborting: LCSAS_FORBID_TMPFS_TARGET=1 forbids RAM-backed targets.\n' >&2
        exit 1
    fi
    _tmpfs_ans=""
    if [ "${LCSAS_ALLOW_TMPFS_TARGET:-0}" = "1" ]; then
        printf '[restore.sh] LCSAS_ALLOW_TMPFS_TARGET=1 -- continuing into RAM.\n' >&2
    elif [ -t 0 ]; then
        printf 'Continue restoring into RAM anyway? [y/N] ' >&2
        IFS= read -r _tmpfs_ans || _tmpfs_ans=""
    elif ( exec </dev/tty ) 2>/dev/null; then
        # stdin is redirected but a controlling terminal exists --
        # a human is still reachable; prompt there.
        printf 'Continue restoring into RAM anyway? [y/N] ' >&2
        IFS= read -r _tmpfs_ans </dev/tty || _tmpfs_ans=""
    else
        printf '[restore.sh] no TTY -- continuing into the RAM-backed target.\n' >&2
        printf '[restore.sh] set LCSAS_FORBID_TMPFS_TARGET=1 to make this fatal.\n' >&2
        _tmpfs_ans="y"
    fi
    if [ "${LCSAS_ALLOW_TMPFS_TARGET:-0}" != "1" ]; then
        case "$_tmpfs_ans" in
            y|Y) ;;
            *)
                # The reader said no: a quit at a question (C5).
                printf 'aborted: pick a directory on a real disk and re-run.\n' >&2
                exit "$EXIT_USER_ABORT" ;;
        esac
    fi
    ;;
esac

# ── Auto-discover other mounted discs for multi-disc restore ──────
#
# When packs are split across multiple LCSAS volumes, the heir may
# have several discs mounted simultaneously.  We scan the usual mount
# points on macOS, Linux, and BSD and pass each as --pack-search to
# the recovery binary.

PACK_SEARCH_ARGS=""
add_pack_search() {
    # $1: a path that might contain restic data.
    # Skip anything under the meta disc while it is still in the drive
    # -- a single-drive heir cannot rely on the meta disc as a pack
    # source AND eject it.  Once it is out, the same mount point may
    # hold a data disc, and that one is searched (#616).
    if meta_disc_is_live; then
        case "$1" in
            "$META_DISC"|"$META_DISC"/*) return ;;
        esac
    fi
    # Add it if data/ exists, or if the path itself contains pack files.
    if [ -d "$1/data" ]; then
        PACK_SEARCH_ARGS="$PACK_SEARCH_ARGS${PACK_SEARCH_ARGS:+$NL}--pack-search$NL$1"
        return
    fi
    if [ -d "$1/repo/data" ]; then
        PACK_SEARCH_ARGS="$PACK_SEARCH_ARGS${PACK_SEARCH_ARGS:+$NL}--pack-search$NL$1/repo"
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
        # Don't list the recovery folder itself again.
        [ "$mnt" = "$RECOVERY" ] && continue
        add_pack_search "$mnt"
    done
    # The disc may be mounted AT the mount dir itself (the documented
    # single-drive journey mounts the data disc at /media), in which
    # case data/ lives directly under $parent and no child matches.
    # Without this, a catalog-less restore has NO pack source at all
    # and even packs on the inserted disc are invisible (#412).  The
    # function's META_DISC guard + data/-existence check keep this safe.
    [ "$parent" = "$RECOVERY" ] || add_pack_search "$parent"
    IFS=":"
done
IFS="$OLD_IFS"

# Hard-error when no data discs were discovered AND the resolved
# $REPO doesn't itself carry a data/ subdir (i.e. it's not a legacy
# self-contained repo).  Without this, the recovery binary will
# eventually fail with a less actionable "no packs found" message
# after the heir has already typed a password.
#
# EXCEPTION (single-drive flow): when META_DISC is set, the heir
# started by mounting the meta disc.  They are about to be asked to
# swap to a data disc -- that hand-off is the recovery binary's job
# via its framed "Insert the right disc and press ENTER to retry."
# prompt.  Hard-exiting here would short-circuit that loop with the
# unactionable error of "you need a data disc mounted to even reach
# the point where I would have told you to swap discs."  Fall through
# and let the binary drive the swap loop.
if [ -z "$PACK_SEARCH_ARGS" ] && [ ! -d "$REPO/data" ] \
   && [ -z "${META_DISC:-}" ] \
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

# Pass the meta disc path through so the C-side locator excludes it
# from its own search list and drops cwd outside of it before prompts.
META_DISC_ARG=""
if [ -n "${META_DISC:-}" ]; then
    META_DISC_ARG="--meta-disc$NL$META_DISC"
fi

# LCSAS_INTERACTIVE=on|off|auto is passed to tier 1 as --interactive, as
# restore.bat does (#617).  auto, tier 1's own default, asks at the disc
# prompt only when its input is a terminal; on lets a test rig with a
# piped stdin play the reader there.  Any other value is ignored.
INTERACTIVE_ARG=""
case "${LCSAS_INTERACTIVE:-}" in
    on|off|auto) INTERACTIVE_ARG="--interactive$NL$LCSAS_INTERACTIVE" ;;
    '') ;;
    *)  printf '[restore.sh] ignoring LCSAS_INTERACTIVE=%s (use on, off or auto)\n' \
               "$LCSAS_INTERACTIVE" >&2 ;;
esac

# ── Session log helper ───────────────────────────────────────────
#
# Append one ISO-8601 UTC line to $HOME/.lcsas-restore-log so a
# returning heir can see what worked last time.  Silently skipped
# when $HOME is unset/empty or not writable -- the log is a
# convenience, never a precondition for restore success.
#
# Disc count is best-effort: we count --pack-search dirs that
# existed at start time.  The tier-1 binary may handle additional
# swaps internally; that delta is not visible here.

session_disc_count() {
    # Count `--pack-search` flag tokens in $PACK_SEARCH_ARGS.  The list is
    # newline-delimited (one token per line), so each flag is its own line;
    # awk on whole-line equality avoids `set -- $PACK_SEARCH_ARGS`, which
    # would clobber the caller's positional parameters.
    printf '%s\n' "$PACK_SEARCH_ARGS" \
        | awk '$0 == "--pack-search" { n++ } END { print n + 0 }'
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

# ── Another backup set (C6) ───────────────────────────────────────
#
# After a restore that succeeds, a reader who chose the set from the
# list may want another set from the same discs (documents, then
# photos).  Asking here, before the exit handler deletes the tools
# folder, keeps its copies and its pack cache for that set: the script
# starts again from the set list with no META disc.  Only when there is
# another set, and someone can answer (is_interactive);
# LCSAS_ANOTHER_SET=0 skips it.  No (the default), Q or the end of the
# input ends the run, and the tools folder goes.  The next set goes into
# a NEW folder by default (<target>-2, -3, ...): restored on top of this
# one, the two sets' files would be mixed (the target guard says so too).
offer_another_set() {
    [ "${LCSAS_ANOTHER_SET:-1}" != 0 ] || return 0
    [ "$REPO_COUNT" -gt 1 ] || return 0
    is_interactive || return 0
    printf '\nRestore another backup set from these discs? [y/N] ' >&2
    _os_ans=""
    IFS= read -r _os_ans || return 0
    case "$_os_ans" in
        y|Y|yes|Yes|YES) ;;
        *) return 0 ;;
    esac
    _os_base="$TARGET_ABS"
    case "${_os_base##*-}" in
        ''|*[!0-9]*) ;;
        *) _os_base="${_os_base%-*}" ;;
    esac
    _os_n=2
    while [ -n "$(ls -A "$_os_base-$_os_n" 2>/dev/null)" ]; do
        _os_n=$((_os_n + 1))
    done
    while :; do
        printf 'Folder to restore it into (ENTER: %s; Q then ENTER: no other set): ' \
               "$_os_base-$_os_n" >&2
        _os_dir=""
        IFS= read -r _os_dir || _os_dir=q
        # shellcheck disable=SC2088  # a typed "~/": the text, not a path
        case "$_os_dir" in
            q|Q)
                printf 'No other backup set restored; %s is done.\n' "$SET_NAME" >&2
                return 0 ;;
            '')   _os_dir="$_os_base-$_os_n" ;;
            '~')  _os_dir="${HOME:-}" ;;
            '~/'*) _os_dir="${HOME:-}/${_os_dir#'~/'}" ;;
        esac
        case "$_os_dir" in
            /?*) ;;
            *)
                printf 'Type the full path of the folder, starting with / or ~/.\n' >&2
                continue ;;
        esac
        _os_probe="$(space_probe_dir "$_os_dir")"
        if [ -n "$STATE_DIR" ] && [ -n "$_os_probe" ] \
           && path_inside "$_os_probe" "$STATE_DIR"; then
            printf 'That folder is inside the tools folder %s, which is\n' "$STATE_DIR" >&2
            printf 'deleted at the end.  Choose a folder outside it.\n' >&2
            continue
        fi
        break
    done
    # This run's own files go now (exec skips the exit handler); the
    # tools folder stays for the next set.
    [ -z "${PWFILE_TMP:-}" ] || rm -f "$PWFILE_TMP"
    drop_shares
    [ -z "${RUN_TMP:-}" ] || rm_tree "$RUN_TMP" || true
    # A pack cache this run put in its own temporary folder (which is
    # gone now) is not handed on: the next run picks its own.
    if [ "$_cache_auto" = 1 ]; then unset LCSAS_PACK_CACHE_DIR; fi
    unset LCSAS_REPO LCSAS_PASSWORD LCSAS_PWFILE LCSAS_SHARES_DIR LCSAS_RESUME
    LCSAS_NEWEST_DISC_PROMPT=0
    # Q at the next run's set list then ends with 0 (another_pass_done).
    LCSAS_ANOTHER_PASS=1
    export LCSAS_NEWEST_DISC_PROMPT LCSAS_ANOTHER_PASS
    if [ -n "$TOOLS" ]; then
        LCSAS_TOOLS_DIR="$TOOLS"
        export LCSAS_TOOLS_DIR
    fi
    printf '[restore.sh] starting again for the next backup set\n' >&2
    cd / 2>/dev/null || true
    exec sh "$SCRIPT" "$RECOVERY" "$_os_dir" latest
}

# restore_done: the set is restored.  resume.sh would carry on a
# restore that finished, so it goes; and from here any exit, Ctrl-C at
# the "another set?" questions too, removes the tools folder.
restore_done() {
    RESTORE_DONE=1
    if [ -n "$TOOLS" ]; then rm -f "$TOOLS/resume.sh"; fi
}

# restore_succeeded TIER: the restore is done; log it, offer another
# set, and exit 0.
restore_succeeded() {
    write_session_log "$1"
    restore_done
    offer_another_set
    exit 0
}

# say_wrong_password: tier 1 exited 77.
say_wrong_password() {
    printf '[tier 1] wrong password: a repository key rejected it, and the\n'  >&2
    printf '        other tiers use the same keys, so not retrying.\n'         >&2
    printf '        Check the password (and any share cards).\n'               >&2
}

# ── Tier dispatch ─────────────────────────────────────────────────
#
# Each tier is tried in priority order.  The default behavior is to
# `exec` the first available binary — once tier 1 starts, the shell
# is gone and there is no fall-through if tier 1 crashes mid-restore.
# That matches the bare-minimum restore story: tier 1 IS the
# restore, and it has to work.
#
# Opt-in fallback (LCSAS_TIER_FALLBACK=1): run the tier as a
# subprocess, capture exit code, and fall through to the next tier
# on non-zero.  Useful when the heir suspects a bug in a higher
# tier and wants the script to walk the cascade for them.  Tier 3 is
# always `exec`'d (it's the last resort — nothing to fall back to).

RESTORE_BIN="$RECOVERY/bin/$TARGET/lcsas-restore"
RUSTIC_BIN="$RECOVERY/bin/$TARGET/rustic-static"
FALLBACK="${LCSAS_TIER_FALLBACK:-0}"

# Issue #223 — present-but-corrupted tier binaries.
#
# `[ -x BIN ]` only checks for the executable bit; it doesn't notice
# bit-rot off the meta disc.  Two failure modes survive that check:
#
#   1. Zero-byte file.  POSIX shells "exec" an empty file as a no-op
#      script that exits 0 -- so without a size check, a corrupted
#      tier 1 silently claims success and the heir gets nothing.
#   2. Wrong-arch binary (Linux ELF on macOS, Windows PE on Linux).
#      `exec` returns 126 ("not executable") and the shell exits;
#      under default semantics that aborts the cascade entirely.
#
# bin_preflight_ok PATH -- returns 0 iff PATH is non-empty AND a
# `--help` dry-run completes without an exec-format error (rc != 126
# and != 127).  Any other exit code (incl. program errors like rc=17
# from a synthetic-failure stub) is treated as "binary loads fine";
# the cascade then runs it for real and lets normal exit-code
# semantics (or LCSAS_TIER_FALLBACK) handle the result.  (It is defined
# with the password helpers above, whose check uses it too.)

# ── Tier 1: prebuilt lcsas-restore (C89, static, no Python) ───────

if [ -x "$RESTORE_BIN" ] && ! bin_preflight_ok "$RESTORE_BIN"; then
    printf '[tier 1] %s is present but failed pre-flight ' "$RESTORE_BIN" >&2
    printf '(zero-byte / wrong arch / corrupted binary); skipping\n' >&2
elif [ -s "$RESTORE_BIN" ] && [ ! -x "$RESTORE_BIN" ]; then
    printf '[tier 1] %s is present but this computer will not run it ' "$RESTORE_BIN" >&2
    printf '(no executable permission, or a noexec mount); skipping.  See ' >&2
    printf 'START_HERE.txt "IF THE COMPUTER REFUSES TO RUN IT".\n' >&2
elif [ -x "$RESTORE_BIN" ]; then
    printf '[tier 1] using prebuilt lcsas-restore (%s)\n' "$TARGET" >&2
    # Drop cwd outside the meta disc before exec, so the kernel does
    # not hold it through the exec barrier.
    [ -n "${META_DISC:-}" ] && cd / 2>/dev/null || true
    # Build the pack-search / catalog / meta-disc flag list as positional
    # params.  The lists are newline-delimited, so splitting on $NL (not
    # the default IFS) keeps a spaced path — e.g. a disc mounted at
    # /media/user/DISC LABEL 1 — as a single argument (issue #364).
    _oifs=$IFS; IFS=$NL
    # shellcheck disable=SC2086
    set -- $PACK_SEARCH_ARGS $CATALOG_ARG $META_DISC_ARG $INTERACTIVE_ARG
    IFS=$_oifs
    if [ "$FALLBACK" = "1" ]; then
        # `set -e` would kill the script on any tier-1 non-zero; the
        # `|| true` lets us capture $? and decide whether to advance.
        tier1_rc=0
        "$RESTORE_BIN" --repo "$REPO" --password-file "$PWFILE" \
                       --target "$TARGET_DIR" --snapshot "$SNAP" \
                       "$@" \
                       || tier1_rc=$?
        if [ $tier1_rc -eq 0 ]; then restore_succeeded 1; fi
        # Exit 77 means a key file POSITIVELY rejected the password
        # (Poly1305 MAC mismatch) -- NOT a missing/unreadable keys dir,
        # which exits 1 and is allowed to fall through.  A rejected
        # password is TERMINAL: tiers 2/2b/3 read the SAME keys with the
        # SAME password, so they would only fail identically -- and a
        # later tier can leave a partial tree behind before it rejects
        # the password (issue #384).  Stop with a clear message.
        if [ $tier1_rc -eq 77 ]; then
            say_wrong_password
            exit 77
        fi
        # Exit 75: the reader typed q at the disc prompt (#615).  That is
        # a pause, not a tier-1 failure, so it is TERMINAL like 77: the
        # tools folder stays and the exit handler prints how to carry on.
        if [ $tier1_rc -eq 75 ]; then
            printf '[tier 1] stopped at the disc prompt at your request; not trying another tier.\n' >&2
            exit 75
        fi
        printf '[tier 1] exited %d, falling through to tier 2\n' \
               $tier1_rc >&2
    else
        # Run (not exec) so the EXIT trap fires and shreds the temporary
        # password file; exec would replace this shell and leave
        # /tmp/lcsas-pw.* on disk after the restore (issue #363).  This is
        # the last tier we try on the non-fallback path, so exit with the
        # binary's own status either way.
        _tier1_rc=0
        "$RESTORE_BIN" --repo "$REPO" --password-file "$PWFILE" \
                       --target "$TARGET_DIR" --snapshot "$SNAP" \
                       "$@" \
                       || _tier1_rc=$?
        if [ "$_tier1_rc" -eq 0 ]; then restore_succeeded 1; fi
        if [ "$_tier1_rc" -eq 77 ]; then say_wrong_password; fi
        exit "$_tier1_rc"
    fi
fi

# ── Tier 2: vendored rustic-static (no Python) ────────────────────
#
# rustic-static expects ALL packs to live in $REPO/data/ on the local
# filesystem.  It has no concept of LCSAS's multi-disc
# layout where packs are spread across N data discs that need to be
# swapped through one drive (issue #227).  Skip tier 2 entirely when
# the resolved repo doesn't carry its own data/ subtree -- the
# heir gets the tier-3 prompt (which DOES understand the disc-
# swap protocol via standalone_restorer.py) instead of a cryptic
# rustic "pack not found" error after the meta disc is ejected.

if [ -x "$RUSTIC_BIN" ] && ! bin_preflight_ok "$RUSTIC_BIN"; then
    printf '[tier 2] %s is present but failed pre-flight ' "$RUSTIC_BIN" >&2
    printf '(zero-byte / wrong arch / corrupted binary); skipping\n' >&2
elif [ -x "$RUSTIC_BIN" ] && [ ! -d "$REPO/data" ]; then
    printf '[tier 2] skipped: rustic-static cannot drive multi-disc ' >&2
    printf 'restores (no %s/data/); falling through to tier 3\n' "$REPO" >&2
elif [ -x "$RUSTIC_BIN" ]; then
    printf '[tier 2] using vendored rustic-static (%s)\n' "$TARGET" >&2
    if [ "$FALLBACK" = "1" ]; then
        tier2_rc=0
        "$RUSTIC_BIN" --repository "$REPO" --password-file "$PWFILE" \
                     restore "$SNAP" "$TARGET_DIR" \
                     || tier2_rc=$?
        if [ $tier2_rc -eq 0 ]; then restore_succeeded 2; fi
        printf '[tier 2] exited %d, falling through to tier 3\n' \
               $tier2_rc >&2
    else
        # Run (not exec) so the EXIT trap shreds the temp password file
        # (issue #363).  Last tier on the non-fallback path → exit with
        # rustic's own status.
        _tier2_rc=0
        "$RUSTIC_BIN" --repository "$REPO" --password-file "$PWFILE" \
                     restore "$SNAP" "$TARGET_DIR" || _tier2_rc=$?
        if [ "$_tier2_rc" -eq 0 ]; then restore_succeeded 2; fi
        exit "$_tier2_rc"
    fi
fi

# ── Tier 2b: stock restic / rustic (independent, audited reader) ──
#
# A fully independent, widely-audited reader of the SAME repository
# format (restic is the cryptographically-audited Go original; it
# restores a rustic-written repo byte-identically -- proven by
# tests/recovery_hardening/test_stock_restic_compat.py).  Tried after the
# vendored rustic-static so a heir who cannot run -- or does not trust --
# the binaries we shipped still gets an AUTOMATIC, no-LCSAS-code restore
# before the Python last resort.  Prefers the per-target stock restic
# bundled on the meta disc (recovery/bin/<arch>/restic), then any
# restic or rustic on the heir's PATH.  Same on-disc-repository requirement
# as tier 2 (it cannot drive the multi-disc swap), so skip when $REPO has
# no local data/.  The fully manual equivalent is
# recovery/docs/RESTORE_STANDARD_TOOLS.txt.
STDTOOL_BIN=""
for _cand in "$RECOVERY/bin/$TARGET/restic" "$RECOVERY/bin/$TARGET/restic.exe"; do
    if [ -x "$_cand" ]; then STDTOOL_BIN="$_cand"; break; fi
done
if [ -z "$STDTOOL_BIN" ]; then
    for _c in restic rustic; do
        if command -v "$_c" >/dev/null 2>&1; then
            STDTOOL_BIN="$(command -v "$_c")"; break
        fi
    done
fi
if [ -n "$STDTOOL_BIN" ] && [ -x "$STDTOOL_BIN" ] \
       && [ -d "$REPO/data" ] && bin_preflight_ok "$STDTOOL_BIN"; then
    # Classify by the BINARY NAME, not the whole path: a stock restic
    # installed under e.g. /opt/rustic-tools/bin/restic must NOT be driven
    # with rustic's CLI just because "rustic" appears in a parent dir
    # (issue #368).
    case "$(basename "$STDTOOL_BIN")" in
        rustic*) _stdtool_kind=rustic ;;
        *)       _stdtool_kind=restic ;;
    esac
    printf '[tier 2b] using stock %s (%s)\n' "$_stdtool_kind" "$STDTOOL_BIN" >&2
    if [ "$FALLBACK" = "1" ]; then
        tier2b_rc=0
        if [ "$_stdtool_kind" = rustic ]; then
            "$STDTOOL_BIN" --repository "$REPO" --password-file "$PWFILE" \
                 restore "$SNAP" "$TARGET_DIR" || tier2b_rc=$?
        else
            RESTIC_PASSWORD_FILE="$PWFILE" "$STDTOOL_BIN" -r "$REPO" \
                 restore "$SNAP" --target "$TARGET_DIR" --no-lock || tier2b_rc=$?
        fi
        if [ $tier2b_rc -eq 0 ]; then restore_succeeded 2; fi
        printf '[tier 2b] exited %d, falling through to tier 3\n' $tier2b_rc >&2
    else
        # Run (not exec) so the EXIT trap shreds the temp password file
        # (issue #363).  Last tier on the non-fallback path → exit with
        # the tool's own status.
        _tier2b_rc=0
        if [ "$_stdtool_kind" = rustic ]; then
            "$STDTOOL_BIN" --repository "$REPO" --password-file "$PWFILE" \
                 restore "$SNAP" "$TARGET_DIR" || _tier2b_rc=$?
        else
            RESTIC_PASSWORD_FILE="$PWFILE" "$STDTOOL_BIN" -r "$REPO" \
                 restore "$SNAP" --target "$TARGET_DIR" --no-lock || _tier2b_rc=$?
        fi
        if [ "$_tier2b_rc" -eq 0 ]; then restore_succeeded 2; fi
        exit "$_tier2b_rc"
    fi
fi

# ─────────────────────────────────────────────────────────────────
# BARE-MINIMUM PATH ENDS HERE.  Everything above is statically-linked
# C/Rust + POSIX sh, with NO Python dependency at any step.
# ─────────────────────────────────────────────────────────────────

# ── Tier 3: Python fallback (LAST RESORT, off the bare path) ─────

if [ "${LCSAS_ALLOW_PYTHON_TIER:-1}" = "1" ]; then
    # Prefer the per-target bundled CPython (from python-build-standalone)
    # over whatever happens to be on $PATH.  This keeps tier 3 working on
    # hosts that don't have python3 packaged.  Only an interpreter that
    # actually runs counts (#714): see find_tier3_python.
    find_tier3_python
    PYBIN="$TIER3_PYBIN"
    PYREST=""
    for cand in "$RECOVERY/../standalone_restorer.py" \
                "$RECOVERY/standalone_restorer.py" \
                "$SCRIPT_DIR/../standalone_restorer.py"; do
        if [ -f "$cand" ]; then PYREST="$cand"; break; fi
    done
    if [ -n "$PYBIN" ] && [ -n "$PYREST" ]; then
        if [ -n "$TIER3_PY_SKIPPED" ]; then
            printf '[tier 3] the bundled Python cannot run on this host (missing musl loader?); using the host %s\n' \
                   "$PYBIN" >&2
        fi
        printf '[tier 3] dispatch: pybin=%s pyrest=%s repo=%s\n' \
            "$PYBIN" "$PYREST" "$REPO" >> "$_restore_exit_log" 2>/dev/null || true
        printf '[tier 3] falling back to Python (%s + %s)\n' \
               "$PYBIN" "$PYREST" >&2
        # standalone_restorer.py CLI is flag-based:
        #   --repo DIR --password-file FILE --target DIR [--snapshot ID]
        #   [--mount-point DIR ...] [--interactive on|off|auto]
        # See src/lcsas/restore/standalone_builder.py:_cli_main.
        # The non-"latest" sentinel is passed straight through.
        TIER3_SNAP_ARGS=""
        if [ -n "$SNAP" ] && [ "$SNAP" != "latest" ]; then
            TIER3_SNAP_ARGS="--snapshot$NL$SNAP"
        fi
        # Issue #234 -- pass every currently-known data-disc root AND
        # every mount-parent dir as --mount-point so tier 3 can drive
        # the LCSAS disc-swap protocol exactly as tier 1 does.
        # PACK_SEARCH_ARGS is already built by the upstream
        # LCSAS_MOUNT_DIRS walk (see line ~700) and contains one
        # "--pack-search <root>" per inserted data disc.  Rewrite to
        # tier-3's flag name.
        #
        # In addition, since the heir typically inserts data discs
        # one at a time after the prompt fires (the first disc may not
        # be mounted when restore.sh started), we ALSO pass each
        # LCSAS_MOUNT_DIRS entry directly as --mount-point so the swap
        # rescan finds packs at the canonical mount point after each
        # insert.  Tier 3 probes both data/<XX>/<hex> and <XX>/<hex>
        # layouts under every mount point.
        # TIER3_MOUNT_ARGS is newline-delimited like PACK_SEARCH_ARGS; the
        # flag token lives on its own line, so rewrite the whole
        # `--pack-search` line to `--mount-point` (a value line with a
        # spaced path is left untouched -- issue #364).
        TIER3_MOUNT_ARGS=""
        if [ -n "$PACK_SEARCH_ARGS" ]; then
            TIER3_MOUNT_ARGS="$(printf '%s\n' "$PACK_SEARCH_ARGS" \
                | sed 's/^--pack-search$/--mount-point/')"
        fi
        OLD_IFS3="$IFS"; IFS=":"
        for parent in $LCSAS_MOUNT_DIRS_EFFECTIVE; do
            IFS="$OLD_IFS3"
            [ -n "$parent" ] || { IFS=":"; continue; }
            # Skip if we already covered this via PACK_SEARCH_ARGS.  Match a
            # whole newline-delimited value line so a spaced parent still
            # compares exactly.
            case "$NL$TIER3_MOUNT_ARGS$NL" in
                *"$NL$parent$NL"*) IFS=":"; continue ;;
            esac
            TIER3_MOUNT_ARGS="$TIER3_MOUNT_ARGS${TIER3_MOUNT_ARGS:+$NL}--mount-point$NL$parent"
            IFS=":"
        done
        IFS="$OLD_IFS3"
        # Issue #239 -- wire PYTHONPATH to the bundled zstandard.
        #
        # `standalone_restorer.py` does `import zstandard` at module
        # top (guarded by try/ImportError -> _HAS_ZSTD=False, so a
        # truly-missing zstandard does NOT crash the script -- it
        # only fails when a zstd-compressed blob is decompressed).
        # But every real LCSAS repository IS zstd-compressed (rustic v2
        # default), so the guard's fallback path would die on the
        # FIRST pack; tier 3 must find zstandard to succeed.
        #
        # `MetaVolumeBuilder._bundle_tools` calls
        # `ToolBundler.bundle_python_package("zstandard")` which
        # stamps the package at <META>/tools/lib/python3.X/zstandard/.
        # The bundled per-target CPython at $RECOVERY/bin/$TARGET/
        # python/ does NOT have zstandard baked in, so we must export
        # PYTHONPATH for tier 3 to find it.  Glob the python3.X
        # subdir -- the host-built bundle stamps in the BUILD host's
        # minor version, which matches the bundled per-target
        # CPython minor (currently 3.12 ↔ 3.12 per
        # recovery/UPSTREAM.sha256 and host install).  If a future
        # version skew breaks ABI, the `import zstandard` guard
        # still lets tier 3 START and fail on the first decompress
        # with an actionable error.
        for _zstd_libdir in "$META_ROOT"/tools/lib/python*; do
            if [ -d "$_zstd_libdir/zstandard" ]; then
                if [ -n "${PYTHONPATH:-}" ]; then
                    PYTHONPATH="$_zstd_libdir:$PYTHONPATH"
                else
                    PYTHONPATH="$_zstd_libdir"
                fi
                export PYTHONPATH
                printf '[tier 3] PYTHONPATH includes bundled zstandard at %s\n' \
                       "$_zstd_libdir" >&2
                break
            fi
        done
        unset _zstd_libdir

        # --interactive on: blind-harness redirects stdin, so isatty()
        # would return false and 'auto' would suppress the prompt.  We
        # want the prompt FIRED so the agent (or heir) can swap
        # discs via the canonical LCSAS protocol.
        #
        # Issue #248 -- pass the catalog through so tier-3's framed
        # prompt can resolve pack IDs to volume labels in the
        # SAME shape tier-1 prints.  $CATALOG_ARG was resolved
        # earlier (~line 796) and reused here verbatim; empty when
        # no catalog is reachable -- the Python reader then
        # falls back to its legacy "(no catalog available)" line.
        write_session_log 3
        # Issue #240 -- capture tier-3 stderr and echo on failure.
        # Issue #248 -- thread $CATALOG_ARG so tier-3's swap prompt
        #               resolves pack IDs to volume labels.
        #
        # Previously this was `exec "$PYBIN" "$PYREST" ...` which
        # replaces the shell process and inherits the Python exit code
        # silently.  When tier-3 crashes at startup (e.g. ImportError,
        # missing PYTHONPATH), the heir sees only a numeric exit
        # and the traceback vanishes.
        #
        # We now run tier-3 as a subprocess, tee stderr to both the
        # heir's terminal AND a capture file, then on non-zero exit
        # replay the captured stderr with a labelled separator so the
        # failure mode is unambiguous in agent transcripts.
        #
        # Portability:
        #  * `restore.sh` is `#!/bin/sh` so we cannot use bash's process
        #    substitution `tee >(...)`.  Instead we swap fds: stderr is
        #    redirected into the pipe to `tee`, stdout is restored via
        #    fd 3 (which we opened to the outer stdout via `3>&1` on
        #    the surrounding brace group).  `tee` then writes one copy
        #    to TIER3_ERR and one copy back to the real stderr (`>&2`).
        #  * POSIX sh has no `PIPESTATUS` / `pipefail`, so the Python
        #    process's exit code cannot be read from `$?` after
        #    the pipeline (that reads `tee`'s rc).  We relay the rc
        #    through a second temp file written by the inner brace
        #    group.
        #  * stdin is NOT touched -- the heir's disc-swap prompt
        #    (`restic_fallback.py::_print_swap_prompt`) still blocks
        #    on stdin in real time; `tee` only sits in front of stderr.
        TIER3_ERR=$(mktemp /tmp/lcsas-tier3-err.XXXXXX)
        TIER3_RC_FILE=$(mktemp /tmp/lcsas-tier3-rc.XXXXXX)
        # Build the catalog / mount-point / snapshot flag list as positional
        # params, splitting the newline-delimited lists so spaced paths
        # survive intact (issue #364).  Done before the redirect block so
        # "$@" can be used inside it.
        _oifs=$IFS; IFS=$NL
        # shellcheck disable=SC2086
        set -- $CATALOG_ARG $TIER3_MOUNT_ARGS $TIER3_SNAP_ARGS
        IFS=$_oifs
        # `set -e` would abort the LHS subshell of the pipe the moment
        # Python exits non-zero, losing the rc capture.  The
        # `&& echo 0 || echo $?` form converts both outcomes into a
        # zero-rc statement (the `||` branch succeeds), keeping the
        # subshell alive long enough to record the rc.  `$?` inside the
        # `||` arm is the failing rc of the Python process -- exactly
        # what we need.
        {
            { "$PYBIN" "$PYREST" \
                   --repo "$REPO" \
                   --password-file "$PWFILE" \
                   --target "$TARGET_DIR" \
                   --interactive on \
                   "$@" \
                && echo 0 > "$TIER3_RC_FILE" \
                || echo $? > "$TIER3_RC_FILE"; \
            } 2>&1 1>&3 3>&- | tee "$TIER3_ERR" >&2
        } 3>&1
        tier3_rc=$(cat "$TIER3_RC_FILE" 2>/dev/null || echo 1)
        # Defensive: cat read an empty file?  Default to 1.
        [ -n "$tier3_rc" ] || tier3_rc=1
        printf '[tier 3] rc=%d\n' "$tier3_rc" >> "$_restore_exit_log" 2>/dev/null || true
        if [ "$tier3_rc" -eq 75 ]; then
            # Exit 75: the reader typed q at tier 3's disc prompt, or its
            # input ended -- a pause, not a failure, as for tier 1's 75.
            # The exit handler keeps the tools folder and says how to
            # carry on.
            printf '[tier 3] stopped at the disc prompt at your request.\n' >&2
        elif [ "$tier3_rc" -ne 0 ] && [ -s "$TIER3_ERR" ]; then
            printf '\n[tier 3] FAILED (rc=%d)\n' "$tier3_rc" >&2
            printf '[tier 3] -------------- captured stderr --------------\n' >&2
            sed 's/^/[tier 3] /' "$TIER3_ERR" >&2
            printf '[tier 3] --------------------------------------------\n' >&2
        fi
        printf '[tier 3] diagnostic log: %s\n' "$_restore_exit_log" >&2 2>/dev/null || true
        rm -f "$TIER3_ERR" "$TIER3_RC_FILE"
        if [ "$tier3_rc" -eq 0 ]; then
            restore_done
            offer_another_set
        fi
        exit "$tier3_rc"
    fi
    if [ -n "$PYREST" ] && [ -z "$PYBIN" ]; then
        if [ -n "$TIER3_PY_SKIPPED" ]; then
            printf '[tier 3] cannot run: the bundled Python %s cannot run on this host\n' \
                   "$TIER3_PY_SKIPPED" >&2
            printf '[tier 3] (missing musl loader?), and there is no working python3 on PATH.\n' >&2
        else
            printf '[tier 3] cannot run: no working Python here (bundled or python3 on PATH).\n' >&2
        fi
    fi
fi

cat >&2 <<EOF
ERROR: no recovery method available.

The bare-minimum restore path (tiers 1-2) needs ONE of:
  * a prebuilt $RESTORE_BIN
  * a prebuilt $RUSTIC_BIN

The optional Python tier (tier 3) needs a Python 3 that runs on this
computer (the bundled one or python3 on PATH) and standalone_restorer.py.

See $RECOVERY/docs/RECOVER.txt for manual restore instructions.
EOF
exit 1
