#!/bin/sh
# restore_auto.sh -- non-interactive variant of restore.sh.
#
# Reads password from $LCSAS_PASSWORD_FILE (must be set).  Restores the latest
# snapshot of every backup set (repository) it finds, each into its own
# folder TARGET_ROOT/<id>, by running restore.sh once per set.  This is
# the meta disc's one automation script (#689); on the disc run it as
#   LCSAS_PASSWORD_FILE=/full/path/pw sh /mnt/recovery/scripts/restore_auto.sh \
#       /mnt/recovery ~/restored
#
# Where it looks for the sets (each a folder holding keys/):
#   $RECOVERY/repos/*/           when $RECOVERY/repos/ exists; otherwise
#   $RECOVERY/*/, $RECOVERY/metadata/*/ and $RECOVERY/../metadata/*/
#   (the last is the meta disc's metadata/ folder, next to recovery/),
#   and metadata/*/ on every mounted disc restore.sh looks at (each
#   folder in $LCSAS_MOUNT_DIRS and each folder in it; there a set also
#   needs index/).  A data disc burned after the meta disc can hold a
#   set the meta disc does not have (#801).
#
# A set that is only on a data disc (not on the meta disc) is found
# only while a disc holding it is mounted, and restore.sh reads its keys
# and snapshots from that disc when the set's turn comes: keep a disc
# that holds it mounted until then.
#
# Each set gets its own tools folder, <tools folder>-<id> (the tools
# folder is $LCSAS_TOOLS_DIR, default $HOME/lcsas-tools).  So a
# set that stopped keeps its resume.sh and pack cache when a later set
# succeeds: only its own success removes them (#801).  Run again, the
# loop carries that set on with its resume.sh, on the same snapshot.
# Every other set is restored again, also one that finished in the
# earlier run: files already restored are skipped, but if a newer data
# disc is mounted by then, its newer snapshot is the one restored, and
# the folder can end up with files of both snapshots.  Restore into a
# new TARGET_ROOT to keep the two apart.
#
# Exit codes:
#   0   -- all repositories restored
#   1   -- at least one repository failed, or no repository was found
#   2   -- usage error
#   4   -- every repository finished, but some items could not be
#          restored: each set's list is <TARGET_ROOT>/<id>/
#          RESTORE_FAILURES.txt (1 wins when a set also failed)
#   74  -- the disk filled up; no later set is tried.  Free some space,
#          then run this same command again.  (An older restore.sh exits
#          1 there, and this exits 1 too, after "the disk is full".)
#   75  -- the person typed q at a disc prompt; no later set is tried
#   129, 130, 143 -- a closed terminal, Ctrl-C or TERM stopped it; no
#          later set is tried
set -eu

case "${1:-}" in
    -h|--help)
        # This header, without the comment marks.
        sed -n '2,/^set -eu$/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
        exit 0 ;;
esac

# A retired environment variable, set, is refused first (#934).
for _old_new in LCSAS_REPO:LCSAS_BACKUP_SET LCSAS_PWFILE:LCSAS_PASSWORD_FILE \
                LCSAS_TARGET:LCSAS_PLATFORM
do
    _old="${_old_new%%:*}"
    eval "_old_set=\${$_old+x}"
    if [ -n "$_old_set" ]; then
        printf 'restore_auto.sh: %s was renamed: use %s\n' "$_old" "${_old_new#*:}" >&2
        exit 2
    fi
done

if [ $# -lt 2 ]; then
    printf 'usage: %s RECOVERY_ROOT TARGET_ROOT\n' "$0" >&2
    printf 'requires LCSAS_PASSWORD_FILE environment variable to be set\n' >&2
    exit 2
fi
if [ -z "${LCSAS_PASSWORD_FILE:-}" ] || [ ! -f "$LCSAS_PASSWORD_FILE" ]; then
    printf 'LCSAS_PASSWORD_FILE must point to an existing password file\n' >&2
    exit 2
fi

RECOVERY="$1"
TARGET="$2"
fail=0
partial=0
NL='
'

# restore.sh's own questions are for a person at a terminal: the newest
# data disc first, and "restore another backup set?" after each one.
# This loop restores every set itself and may run on a terminal, so it
# turns both off (#605, #609).
LCSAS_NEWEST_DISC_PROMPT=0
LCSAS_ANOTHER_SET=0
export LCSAS_NEWEST_DISC_PROMPT LCSAS_ANOTHER_SET

# Ctrl-C, TERM or a closed terminal stop the whole loop (#801).  The
# trap only notes it: the set being restored ends first (restore.sh
# keeps its tools folder and says how to carry on), then the loop
# stops.  A trap that runs a command, unlike one that ignores the
# signal, is reset in restore.sh, which sets its own.
stop_rc=""
trap 'stop_rc=130' INT
trap 'stop_rc=143' TERM
trap 'stop_rc=129' HUP

# The tools folder each set gets: <base>-<id>, next to the one a plain
# restore.sh run uses, never inside it (restore.sh only takes a folder
# that is empty or one it made itself).
tools_base="${LCSAS_TOOLS_DIR:-${HOME:+$HOME/lcsas-tools}}"
tools_base="${tools_base%/}"
case "$tools_base" in ''|/*) ;; *) tools_base="$(pwd)/$tools_base" ;; esac

# ── The backup sets ───────────────────────────────────────────────
#
# Every folder with a keys/ folder, once per id: the meta disc, a data
# disc and a hand-made tree can each hold a copy of one set.  restore.sh
# then picks the newest copy of the set it is asked for (#605).
sets=""
seen="/"
add_sets() {
    # add_sets DIR [index]: the sets in DIR; with "index", only those
    # with an index/ folder too (restore.sh's test for a disc's copy).
    for _as in "$1"/*/; do
        [ -d "$_as" ] || continue
        [ -d "$_as/keys" ] || continue
        if [ "${2:-}" = index ] && [ ! -d "$_as/index" ]; then continue; fi
        _as="${_as%/}"
        _as="${_as##*/}"
        case "$seen" in *"/$_as/"*) continue ;; esac
        seen="$seen$_as/"
        sets="$sets$_as$NL"
    done
    return 0
}

if [ -d "$RECOVERY/repos" ]; then
    add_sets "$RECOVERY/repos"
else
    add_sets "$RECOVERY"
    add_sets "$RECOVERY/metadata"
    add_sets "$RECOVERY/../metadata"
    # The mounted discs, found as restore.sh finds them: each folder in
    # LCSAS_MOUNT_DIRS ('' = none) and each folder in it.
    _user="$(id -un 2>/dev/null || true)"
    _mount_dirs="${LCSAS_MOUNT_DIRS-/Volumes:/media/$_user:/media:/mnt:/run/media/$_user:/run/media}"
    _ifs="$IFS"; IFS=":"
    for _parent in $_mount_dirs; do
        IFS="$_ifs"
        if [ -n "$_parent" ] && [ -d "$_parent" ]; then
            for _mnt in "$_parent" "$_parent"/*; do
                if [ -d "$_mnt/metadata" ]; then add_sets "$_mnt/metadata" index; fi
            done
        fi
        IFS=":"
    done
    IFS="$_ifs"
fi

if [ -z "$sets" ]; then
    printf '!!! no backup set found: no folder with keys/ under\n' >&2
    printf '    %s/repos, %s, %s/metadata or %s/../metadata,\n' \
           "$RECOVERY" "$RECOVERY" "$RECOVERY" "$RECOVERY" >&2
    printf '    and no mounted disc with one in its metadata/ folder.\n' >&2
    printf '    Nothing was restored.  Run restore.sh itself with a data disc\n' >&2
    printf '    mounted, and LCSAS_BACKUP_SET=<id> to choose the set.\n' >&2
    exit 1
fi

# ── Restoring one set ─────────────────────────────────────────────
#
# When the disk fills up, restore.sh exits 74 (tiers 1 and 3 say so with
# that exit code).  An older tier 1 or tier 3, and tiers 2 and 2b, exit
# 1, as for any other failure, and say "out of space" (tier 1), "No
# space left on device" (tier 3, tiers 2 and 2b) or "Disk quota
# exceeded".  So, for those, restore.sh's messages go to the
# screen through tee, and a copy goes to grep, which keeps only those
# lines: nothing is written to the disk, which may be the full one.
# The copy needs /dev/fd; without it restore.sh runs plainly, and a full
# disk that only the messages tell counts as one more failed set.  tee
# and grep ignore Ctrl-C, so what restore.sh prints as it stops (how to
# carry on) still shows.
#
# A set that stopped in an earlier run left resume.sh in its tools
# folder.  When that resume.sh carries on this same set into this same
# folder, it runs instead of restore.sh: it keeps the snapshot the
# stopped run chose, where a fresh run could pick a newer one off a disc
# mounted since and leave a mix of the two in the folder.
#
# restore_cmd ID OUT TOOLS: run restore.sh, or the set's resume.sh.
restore_cmd() {
    _rc_resume="$3/resume.sh"
    if [ -n "$3" ] && [ -f "$_rc_resume" ] \
       && grep -qF "LCSAS_BACKUP_SET='$1'" "$_rc_resume" 2>/dev/null \
       && grep -qF "'$(cd "$2" && pwd)'" "$_rc_resume" 2>/dev/null; then
        printf '==> carrying on the restore of %s that stopped, with %s\n' \
               "$1" "$_rc_resume" >&2
        sh "$_rc_resume"
    else
        LCSAS_BACKUP_SET="$1" LCSAS_TOOLS_DIR="$3" \
            sh "$RECOVERY/scripts/restore.sh" --restore-folder "$2" --snapshot latest "$RECOVERY"
    fi
}

# run_set ID OUT TOOLS: sets set_rc to restore.sh's exit code and
# set_full to 1 when its messages said the disk is full.
exec 3>&1
run_set() {
    set_rc=0
    set_full=0
    if [ ! -d /dev/fd ]; then
        restore_cmd "$@" || set_rc=$?
        return 0
    fi
    _rs_out="$(
        {
            {
                trap ':' INT TERM HUP
                _rc=0
                restore_cmd "$@" 2>&1 >&3 3>&- 4>&- || _rc=$?
                printf 'lcsas-auto-rc=%s\n' "$_rc" >&4
            } | ( trap '' INT TERM HUP; exec tee /dev/fd/5 5>&1 >&2 3>&- 4>&- ) \
              | ( trap '' INT TERM HUP
                  exec grep -i -e 'out of space' -e 'no space left on device' \
                               -e 'disk quota exceeded' 3>&- 4>&- )
        } 4>&1
    )" || true
    set_rc=""
    while IFS= read -r _rs_line; do
        case "$_rs_line" in
            lcsas-auto-rc=*) set_rc="${_rs_line#lcsas-auto-rc=}" ;;
            ?*) set_full=1 ;;
        esac
    done <<EOF
$_rs_out
EOF
    case "$set_rc" in
        ''|*[!0-9]*) set_rc="${stop_rc:-1}" ;;
    esac
    return 0
}

# not_yet ID: the sets after ID in the list, on one line.
not_yet() {
    printf '%s' "$sets" | awk -v id="$1" '
        after { s = s (s == "" ? "" : ", ") $0 }
        $0 == id { after = 1 }
        END { print s }'
}

# stop_loop ID RC WHY: end the loop at the set ID with exit code RC.
stop_loop() {
    printf '!!! The restore of %s stopped: %s (exit %s).\n' "$1" "$3" "$2" >&2
    _ny="$(not_yet "$1")"
    if [ -n "$_ny" ]; then
        printf '    These backup sets were not restored: %s\n' "$_ny" >&2
    fi
    printf '    To carry on, run this same command again: %s carries on\n' "$1" >&2
    printf '    where it stopped, and files already restored are skipped.\n' >&2
    exit "$2"
}

# A set's id is a folder name off a disc: no pattern matching on it.
set -f
_ifs="$IFS"; IFS="$NL"
for name in $sets; do
    IFS="$_ifs"
    [ -z "$stop_rc" ] || stop_loop "$name" "$stop_rc" "the run was interrupted before it began"
    out="$TARGET/$name"
    mkdir -p "$out"
    printf '==> restoring %s -> %s\n' "$name" "$out" >&2
    # Select THIS repository via LCSAS_BACKUP_SET so restore.sh restores the
    # right one for each iteration instead of re-resolving on its own --
    # which, on a multi-repository archive, would hit the interactive
    # selection prompt (EOF → fail) or restore the same one every loop
    # (issue #373).
    run_set "$name" "$out" "${tools_base:+$tools_base-$name}"
    case "$set_rc" in
        0)
            ;;
        4)
            # #799: the set finished, but some items could not be
            # restored.  The next set goes on.  (When a disc was not
            # there, restore.sh keeps that set's resume.sh, #887: run
            # this again with the disc in and restore_cmd carries it on.)
            printf '!!! %s finished, but some items could not be restored:\n' "$name" >&2
            printf '    they are listed in %s/RESTORE_FAILURES.txt\n' "$out" >&2
            partial=1 ;;
        74)
            stop_loop "$name" 74 \
                "the disk is full; free some space or use a bigger disk" ;;
        77)
            # Wrong password for THIS repository (issue #384).  Each
            # repository has its own password, so one shared LCSAS_PASSWORD_FILE
            # can be right for some and wrong for others -- keep looping,
            # but say exactly which repository rejected the password.
            printf '!!! restore of %s FAILED: wrong password (exit 77).\n' \
                   "$name" >&2
            printf '    LCSAS_PASSWORD_FILE does not decrypt this backup set'"'"'s keys;\n' >&2
            printf '    the remaining backup sets are still attempted.\n' >&2
            fail=1 ;;
        75)
            stop_loop "$name" 75 "you chose to stop at a disc prompt" ;;
        129|130|143)
            stop_loop "$name" "$set_rc" "it was interrupted" ;;
        *)
            if [ "$set_full" = 1 ]; then
                stop_loop "$name" "$set_rc" \
                    "the disk is full; free some space or use a bigger disk"
            fi
            printf '!!! restore of %s FAILED (exit %s)\n' "$name" "$set_rc" >&2
            fail=1 ;;
    esac
    IFS="$NL"
done
IFS="$_ifs"

[ -z "$stop_rc" ] || exit "$stop_rc"
[ "$fail" = 0 ] || exit 1
[ "$partial" = 0 ] || exit 4
exit 0
