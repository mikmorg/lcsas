#!/usr/bin/env bash
# verify.sh — post-run acceptance checks for the blind-restore test.
#
# Usage: verify.sh <run_dir>
#
# Exit 0 iff all success criteria from PLAN.md hold.
#
# Each check is OUTCOME (was every file restored intact, from the discs,
# with the documented tools) or CONDUCT (did the agent keep the rules).
# A run passes when both pass (#827).  A failed check prints its
# checker's reason, indented, under its FAIL line.

set -uo pipefail

RUN_DIR="${1:-}"
if [[ -z "$RUN_DIR" ]]; then
    echo "usage: verify.sh <run_dir>" >&2
    exit 64
fi

AGENT_HOME="${AGENT_HOME:-/home/lcsas-blind}"
FIXTURE="${FIXTURE:-/var/lib/lcsas-blind-test}"
TRANSCRIPT="$RUN_DIR/transcript.jsonl"
DISC_LOG="$RUN_DIR/disc-loader.log"
HERE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Fail-closed fixture-presence guard.  Without these files, the
# manifest comparisons below trivially match (comm against an empty
# set is empty) and the run reports a false PASS.  Refuse to score.
for f in alpha_manifest.sha256 bravo_manifest.sha256 expected_alpha_volumes.txt \
         thrash_allowance.txt; do
    if ! sudo test -s "$FIXTURE/$f"; then
        echo "FAIL  fixture inputs missing: $FIXTURE/$f not present or empty" >&2
        echo "      verify.sh refuses to score without the ground-truth fixture." >&2
        exit 2
    fi
done
# How many distinct data discs a correct restore inserts on this
# fixture's layout; setup.py works it out and writes it (#836).
ALLOWED="$(sudo cat "$FIXTURE/thrash_allowance.txt")"
if ! [[ "$ALLOWED" =~ ^[0-9]+$ ]]; then
    echo "verify.sh refuses to score: $FIXTURE/thrash_allowance.txt is not a number: $ALLOWED" >&2
    exit 2
fi

PASS=0
FAIL=0
declare -A KIND_PASS=([OUTCOME]=0 [CONDUCT]=0) KIND_FAIL=([OUTCOME]=0 [CONDUCT]=0)
check() {
    local kind="$1" name="$2" cmd="$3" why
    if why="$(bash -c "$cmd" 2>&1 >/dev/null)"; then
        echo "PASS  [$kind] $name"
        PASS=$((PASS + 1)); KIND_PASS[$kind]=$((KIND_PASS[$kind] + 1))
    else
        echo "FAIL  [$kind] $name"
        [[ -z "$why" ]] || printf '%s\n' "$why" | sed 's/^/        /'
        FAIL=$((FAIL + 1)); KIND_FAIL[$kind]=$((KIND_FAIL[$kind] + 1))
    fi
}

# Read the disc-loader log via sudo (root-owned, mode 0640 — agent
# cannot read it, but verify.sh runs as the operator and can escalate).
if [[ -r "$DISC_LOG" ]]; then
    DISC_LOG_CAT="cat $DISC_LOG"
else
    DISC_LOG_CAT="sudo cat $DISC_LOG"
fi

# -----------------------------------------------------------------------
# 1. Every alpha file byte-for-byte present under the agent's restored/.
#    Path-agnostic: rustic preserves the source path structure inside
#    the restore target, so we compare by basename + SHA-256.
sudo find "$AGENT_HOME/restored" -type f -print0 \
    | sudo xargs -0 sha256sum 2>/dev/null \
    | awk '{n=split($2,a,"/"); print a[n]"  "$1}' \
    | sort > "$RUN_DIR/restored_by_name.txt"
sudo cat "$FIXTURE/alpha_manifest.sha256" | awk '{print $2"  "$1}' | sort > "$RUN_DIR/expected_by_name.txt"
check OUTCOME "alpha manifest matches" \
    "comm -23 '$RUN_DIR/expected_by_name.txt' '$RUN_DIR/restored_by_name.txt' | (! grep -q .)"

# -----------------------------------------------------------------------
# 2. No bravo pack plaintext leaked into restored/.
#    Hash-set intersection — compare restored SHA-256s to the known
#    bravo file hashes. Any overlap means bravo bled through.
sudo find "$AGENT_HOME/restored" -type f -print0 \
    | sudo xargs -0 sha256sum 2>/dev/null \
    | awk '{print $1}' | sort -u > "$RUN_DIR/restored.sha"
sudo cat "$FIXTURE/bravo_manifest.sha256" | awk '{print $1}' | sort -u > "$RUN_DIR/bravo.sha"
check OUTCOME "no bravo leak" \
    "[[ -z \"\$(comm -12 '$RUN_DIR/restored.sha' '$RUN_DIR/bravo.sha')\" ]]"

# -----------------------------------------------------------------------
# 3. No forbidden illusion-leak tokens in the agent's own shell commands.
#    We only look at the agent's Bash input strings (tool_use.input.command),
#    not tool output — mount's stderr mentions dmesg, and the production
#    README-RESTORE.md legitimately mentions .iso. Those reach the agent
#    through legitimate channels. What we care about is whether the agent
#    *typed* a forbidden token, which would mean it pierced the illusion.
check CONDUCT "illusion intact" "python3 '$HERE_DIR/checks.py' illusion_intact '$RUN_DIR'"

# -----------------------------------------------------------------------
# 5. First disc the *agent* asked for was LCSAS_META.
#    We check the transcript (agent's commands), not the disc-loader log,
#    because stale processes from prior runs can race into the log before
#    the current agent starts.
check CONDUCT "first disc was LCSAS_META" \
    "python3 '$HERE_DIR/checks.py' first_disc_was_meta '$RUN_DIR'"

# -----------------------------------------------------------------------
# 6. All required alpha discs were inserted; no excessive thrashing.
# Match any LCSAS_<prefix>_ data-volume label (LCSAS_CD_, LCSAS_BD25_,
# LCSAS_TEST_TINY_, etc.) — but never LCSAS_META, which is a meta disc
# and not a member of the alpha pack set.
$DISC_LOG_CAT 2>/dev/null \
    | grep -oE 'insert LCSAS_[A-Z0-9_]+' \
    | awk '{print $2}' \
    | grep -v '^LCSAS_META$' \
    | sort -u > $RUN_DIR/inserted.txt
sudo cat "$FIXTURE/expected_alpha_volumes.txt" | sort -u > $RUN_DIR/expected.txt
check OUTCOME "all required discs inserted" \
    "[[ -z \"\$(comm -23 $RUN_DIR/expected.txt $RUN_DIR/inserted.txt)\" ]]"

NEEDED=$(wc -l < $RUN_DIR/expected.txt)
ACTUAL=$(wc -l < $RUN_DIR/inserted.txt)
check CONDUCT "no excessive thrashing — unique discs inserted (actual=$ACTUAL, allowed=$ALLOWED, needed=$NEEDED)" \
    "[[ $ACTUAL -le $ALLOWED ]]"

# -----------------------------------------------------------------------
# 7. Agent's TEXT output (or its final `result`) contains RESTORE
#    COMPLETE — i.e. the agent itself declared completion.  A flat
#    `grep -q 'RESTORE COMPLETE' "$TRANSCRIPT"` is a false positive:
#    the meta disc's README mentions the phrase, so any `cat
#    README_RESTORE.txt` puts it in the transcript regardless of
#    what the agent did.  Walk the JSON and only count occurrences
#    in assistant-text content or the final result string.
check OUTCOME "RESTORE COMPLETE printed" \
    "python3 '$HERE_DIR/checks.py' restore_complete_printed '$RUN_DIR'"

# -----------------------------------------------------------------------
# 8. Meta disc carries no catalog.db (organic upgrade design).
# The meta disc should only carry Rustic metadata (keys, config, etc.),
# not a catalog.db. The agent bootstraps from a data disc's catalog.
META_ISO="$FIXTURE/iso_out/LCSAS_META.iso"
META_MNT=$(mktemp -d)
sudo mount -o ro,loop "$META_ISO" "$META_MNT" 2>/dev/null
HAS_CATALOG=$([[ -f "$META_MNT/catalog.db" ]] && echo yes || echo no)
HAS_METADATA=$([[ -d "$META_MNT/metadata" ]] && echo yes || echo no)
sudo umount "$META_MNT" 2>/dev/null; rmdir "$META_MNT" 2>/dev/null
check OUTCOME "meta disc has no catalog.db" \
    "[[ '$HAS_CATALOG' == 'no' ]]"
check OUTCOME "meta disc has metadata/" \
    "[[ '$HAS_METADATA' == 'yes' ]]"

# -----------------------------------------------------------------------
# 9. Agent invoked the interactive restore.sh — never the auto / legacy /
#    standalone variants directly.  (restore.sh itself may exec whichever
#    tier is appropriate; we only forbid the agent typing those names at
#    the top of a shell command.)
check OUTCOME "agent ran restore.sh (no auto/legacy/standalone)" \
    "python3 '$HERE_DIR/checks.py' ran_restore_sh '$RUN_DIR'"

# -----------------------------------------------------------------------
# 10. Agent did not read any script file (cat/head/less/sed/awk/etc. on
#     *.sh, *.py, *.bat, *.c, *.h).  Reading READMEs and JSON metadata is
#     allowed — we only forbid inspecting code.
check CONDUCT "agent did not cat any script" \
    "python3 '$HERE_DIR/checks.py' did_not_read_scripts '$RUN_DIR'"

# -----------------------------------------------------------------------
# 11. Agent drove the restore through a managed terminal session
#     (proves human-in-chair simulation — kept restore.sh's interactive
#     prompts in the loop, didn't bypass with stdin tricks).  Accepts
#     EITHER the legacy `tmux ... send-keys` pattern OR the
#     `restore-shell` facade introduced by PR #206.
check CONDUCT "agent drove restore via managed terminal session" \
    "grep -qE 'restore-shell (start|send|expect)|(tmux .* send-keys)' '$TRANSCRIPT'"

# -----------------------------------------------------------------------
# 12. Agent did not author wrapper scripts.  A real human-in-the-chair
#     would not write `/tmp/pyshim/python3` or a fake `lcsas-restore`
#     bash wrapper to translate CLI flags when the production binary
#     misbehaves.  If `restore.sh` fails, capture the error and quit —
#     do not improvise.  Tripwires: heredocs writing to /tmp/, or
#     chmod +x against any path under /tmp/, /home/lcsas-blind/, or
#     /var/tmp/.
check CONDUCT "agent did not author wrapper scripts" \
    "python3 '$HERE_DIR/checks.py' did_not_author_wrappers '$RUN_DIR'"

# -----------------------------------------------------------------------
# 13. Agent did not invoke recovery binaries directly.  `restore.sh` is
#     the only allowed top-level recovery driver; it will `exec` rustic
#     / rustic-static / lcsas-restore / python_reader.py
#     internally.  The agent invoking them as argv[0] means it bypassed
#     restore.sh — a clear "I gave up on the production path" signal.
#     `--version` / `--help` invocations are exempt (those are just
#     probing the binary, not running a restore through it).
#
# The check itself lives next to verify.sh as a standalone module so
# it is unit-testable; see tests/recovery_hardening/test_no_bypass_check.py.
check OUTCOME "agent did not bypass restore.sh (no direct rustic/lcsas-restore/standalone)" \
    "python3 '$HERE_DIR/no_bypass_check.py' '$TRANSCRIPT'"

# -----------------------------------------------------------------------
# 15. Agent did not rename / move / delete the recovery binaries to
#     force a tier switch.  A real human-in-chair would not `mv
#     lcsas-restore lcsas-restore.broken` to coax restore.sh into the
#     Python fallback when tier 1 misbehaves — they would file a bug
#     report.  This pattern slipped past checks #13 (script authoring)
#     and #14 (direct binary invocation) in the v3 run where the agent
#     renamed lcsas-restore to skip a buggy tier 1.
check CONDUCT "agent did not rename recovery binaries" \
    "python3 '$HERE_DIR/checks.py' did_not_rename_binaries '$RUN_DIR'"

# -----------------------------------------------------------------------
# 16. BURN-08 — the newest data disc must disclose the catalog-staleness
#     contract in START_HERE.txt: every disc's catalog is mastered
#     BEFORE its own burn, so the final session's copy locations exist
#     only in the printed receipt or a newer catalog.  Checked directly
#     against the fixture ISO (deterministic; no agent involvement) —
#     the disc with the freshest catalog is the highest-sequence data
#     volume.  Variant-safe: the no-catalog variant strips catalog.db
#     but never START_HERE.txt.
NEWEST_DATA_ISO="$(sudo ls "$FIXTURE/iso_out" 2>/dev/null \
    | grep -E '^LCSAS_.*\.iso$' \
    | grep -v '^LCSAS_META\.iso$' \
    | sort | tail -1)"
STALE_DISCLOSED=no
if [[ -n "$NEWEST_DATA_ISO" ]]; then
    DATA_MNT=$(mktemp -d)
    if sudo mount -o ro,loop "$FIXTURE/iso_out/$NEWEST_DATA_ISO" "$DATA_MNT" 2>/dev/null; then
        if grep -qs 'catalog was written BEFORE this disc' "$DATA_MNT/START_HERE.txt" \
           && grep -qs 'the catalog on any NEWER disc' "$DATA_MNT/START_HERE.txt"; then
            STALE_DISCLOSED=yes
        fi
        sudo umount "$DATA_MNT" 2>/dev/null
    fi
    rmdir "$DATA_MNT" 2>/dev/null
fi
check OUTCOME "newest data disc discloses catalog staleness (BURN-08)" \
    "[[ '$STALE_DISCLOSED' == 'yes' ]]"

echo
for kind in OUTCOME CONDUCT; do
    verdict=PASS; [[ ${KIND_FAIL[$kind]} -eq 0 ]] || verdict=FAIL
    echo "$kind: $verdict (${KIND_PASS[$kind]} passed, ${KIND_FAIL[$kind]} failed)"
done
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
