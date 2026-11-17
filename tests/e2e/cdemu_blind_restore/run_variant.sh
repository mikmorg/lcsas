#!/usr/bin/env bash
# run_variant.sh — driver for adversarial blind-restore variants
# (issue #214).  Each variant mutates the fixture before running
# the standard blind-restore harness.
#
# Usage:  sudo bash run_variant.sh <variant>
#
# Supported variants:
#   default              — baseline (no mutation).  Sanity check.
#   tier1-missing        — meta disc lacks lcsas-restore.  Agent must
#                          ride the LCSAS_TIER_FALLBACK=1 path to tier 2.
#   tier1-tier2-missing  — meta disc lacks BOTH tier-1 and tier-2.  Forces
#                          tier 3 (CPython + standalone_restorer.py).
#   single-tenant        — only the alpha repo exists; exercises the
#                          no-prompt fast path (issue #216).
#   5-tenant             — alpha + bravo + charlie + delta + echo;
#                          stress-tests the multi-tenant prompt (#217).
#   no-catalog           — every data disc lacks catalog.db; forces the
#                          hash-only swap-prompt path (issue #218).
#   single-key           — explicit baseline alias of `default`: agent
#                          gets the plaintext ~/tenant-alpha.pw, standard
#                          flow, no mutation (key-escrow Phase 3, K3.1).
#   split-key-2of5       — agent gets ONLY 5 SLIP-0039 share cards (no
#                          plaintext password) and must reconstruct the
#                          password before restoring (key-escrow Phase 3,
#                          K3.2).  Keeps alpha+bravo tenants.
#   split-key-docs       — KEY-04 docs-driven acceptance gate: agent gets a
#                          scenario-only prompt (no command spoon-feed) plus 2
#                          production -card.txt artifacts, and must derive
#                          every command from the on-disc instructions.  The
#                          archive splits every password 2-of-5 in its
#                          config; the discs and a config-built meta disc
#                          carry PASSWORD_INFO.txt (#828).
#   mixed-split          — #587: one archive, alpha's password split 2-of-3
#                          in its own config table, bravo on an ordinary
#                          password.  The discs and a config-built meta disc
#                          carry the per-repository split text; the agent
#                          gets split-key-docs' prompt and 2 of the 3 cards.
#   windows              — UX-08 layer 3: the Windows heir journey driven
#                          through recovery/scripts/restore.bat under wine,
#                          with the discs presented as a plain directory
#                          tree (no cdemu — Windows has no vhba).  NEVER
#                          BUILT: its blockers (UX-01, INFRA-01) landed, and
#                          the owner decided (2026-10-30, #479) to retire it
#                          once the manual Windows drill (#476) passes.  It
#                          fails loud, spending nothing, until then.
#
# Exits 0 with `SCORE: 16/16 (variant=<name>)  OUTCOME PASS  CONDUCT PASS`
# on full pass: verify.sh's checks come in two groups, OUTCOME (every file
# restored intact, from the discs, with the documented tools) and CONDUCT
# (the rules kept), and a run passes when both pass (#827).
# Exits non-zero on any failure; the score line still prints.
#
# The run is scored on its OWN run folder ($RUN_DIR), never "the newest
# /tmp/lcsas-blind-* folder" (#830).  A pass needs all 16 of verify.sh's
# checks (9 OUTCOME, 7 CONDUCT) to run and pass; a run that cannot be scored (no folder, no
# transcript, a 0/0 score) fails, XFAIL-listed or not.  Besides the
# SCORE line it prints `RUN_DIR: <path>` before the agent starts and one
# `RESULT: {json}` line (run folder, score, cost, turns) at the end, for
# sweep scripts to read from the log (#830).
#
# Run folders (#840): $LCSAS_BLIND_TMPDIR, else $TMPDIR, else /tmp (sudo -E
# drops TMPDIR, so set LCSAS_BLIND_TMPDIR to move them).  A passing run
# removes its folder (LCSAS_BLIND_KEEP_RUN=1 keeps it); a failing one keeps
# it, and run.sh prunes kept folders older than LCSAS_BLIND_KEEP_DAYS
# (default 14).

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
VARIANT="${1:?usage: run_variant.sh <variant>}"

case "$VARIANT" in
    default|tier1-missing|tier1-tier2-missing) : ;;
    single-tenant|5-tenant|no-catalog) : ;;
    single-key|split-key-2of5|split-key-docs) : ;;
    mixed-split) : ;;
    windows) : ;;
    *)
        echo "ERROR: unknown variant: $VARIANT" >&2
        echo "       supported: default | tier1-missing | tier1-tier2-missing |" >&2
        echo "                  single-tenant | 5-tenant | no-catalog |" >&2
        echo "                  single-key | split-key-2of5 | split-key-docs |" >&2
        echo "                  mixed-split | windows" >&2
        exit 2
        ;;
esac

# UX-08 layer 3: the Windows journey would NOT use the cdemu/vhba disc
# loader (no kernel module on a Windows-emulating host); it would present
# the discs as a directory tree and drive restore.bat under `wine cmd`.  It
# was never built.  Its old blockers, UX-01 (restore.bat repo discovery)
# and INFRA-01 (the windows fixture builder), both landed; on 2026-10-30
# the owner decided to retire this variant once the manual Windows drill
# (#476) passes, because windows-e2e.yml (restore.bat on a real Windows
# runner) plus that drill cover the journey (#479).  Fail loud, spending
# nothing, instead of faking a green/red number.
if [ "$VARIANT" = "windows" ]; then
    echo "ERROR: variant windows was never built." >&2
    echo "       It retires once the manual Windows drill (#476) passes" >&2
    echo "       (owner decision, 2026-10-30; see #479).  What covers" >&2
    echo "       Windows today: .github/workflows/windows-e2e.yml and" >&2
    echo "         sh recovery/tests/test_restore_bat_e2e.sh" >&2
    echo "SCORE: 0 (variant=windows)  [NOT BUILT: retires once #476 passes]" >&2
    exit 3
fi

echo "=== blind-restore variant: $VARIANT ==="

# Each variant rebuilds the fixture from scratch via setup.py with
# LCSAS_VARIANT set; setup.py's _apply_variant_mutations() handles
# the meta-disc surgery (binary removal) per variant before the ISO
# is burned, and _apply_data_disc_variant_mutations() handles per-
# data-disc surgery (e.g. catalog.db removal for no-catalog).
export LCSAS_VARIANT="$VARIANT"

# Tenant-count variants set LCSAS_TENANT_COUNT so setup.py's _tenants()
# generates the right repo list.
case "$VARIANT" in
    single-tenant)  export LCSAS_TENANT_COUNT=1 ;;
    5-tenant)       export LCSAS_TENANT_COUNT=5 ;;
esac

# Tier-fallback-requiring variants set LCSAS_TIER_FALLBACK=1 so the
# agent's restore.sh invocation falls through to tier 2 / tier 3.
case "$VARIANT" in
    tier1-missing|tier1-tier2-missing)
        export LCSAS_TIER_FALLBACK=1
        ;;
esac

"$HERE/setup.py"

RUN_BASE="$(python3 "$HERE/blind_runs.py" base)"
mkdir -p "$RUN_BASE"
RUN_DIR="$RUN_BASE/lcsas-blind-variant-${VARIANT}-$$"
# A leftover folder of the same name (pid reuse) would be scored as ours.
rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR"
echo "RUN_DIR: $RUN_DIR"

# 45-minute wall-clock cap, matching make blind-restore.
timeout --foreground 2700 \
    env RUN_DIR="$RUN_DIR" \
        LCSAS_VARIANT="$VARIANT" \
        ${LCSAS_TENANT_COUNT:+LCSAS_TENANT_COUNT=$LCSAS_TENANT_COUNT} \
        ${LCSAS_TIER_FALLBACK:+LCSAS_TIER_FALLBACK=$LCSAS_TIER_FALLBACK} \
    "$HERE/run.sh"

# Score THIS run's folder.  blind_runs.py prints verify.sh's output, the
# SCORE line and the RESULT line; exit 0 = 16/16, 1 = scored and failed,
# 2 = could not be scored.
score_rc=0
python3 "$HERE/blind_runs.py" score "$RUN_DIR" --variant "$VARIANT" \
    || score_rc=$?

# A passing run removes its folder (#840); a failing one keeps it for the
# post-mortem.  LCSAS_BLIND_KEEP_RUN=1 keeps a passing one too.
finish() {
    local rc="$1"
    if [ "$rc" -eq 0 ] && [ "${LCSAS_BLIND_KEEP_RUN:-0}" != "1" ]; then
        rm -rf "$RUN_DIR"
        echo "run folder removed (passed): $RUN_DIR"
    else
        echo "run folder kept: $RUN_DIR"
    fi
    exit "$rc"
}

if [ "$score_rc" -ne 0 ] && [ "$score_rc" -ne 1 ]; then
    # Not scored at all: never an XFAIL, never a pass.
    echo "ERROR: variant=$VARIANT could not be scored (see above)" >&2
    finish 2
fi

# Variants that are expected to fail until a tracked bug lands are
# recorded in the XFAIL ledger (GATE-06): tests/e2e/cdemu_blind_restore/
# XFAIL.list, one "<variant> issue=#N expires=YYYY-MM-DD" entry per
# line.  A red score on a listed variant is reported as XFAIL and exits
# 0 (the baseline we expect until the underlying production-code bug is
# fixed).  The LCSAS_VARIANT_XFAIL env var (comma-separated) still
# overrides the ledger for ad-hoc runs.
#
# Promotion log.  The 2026-05 entries were scored when verify.sh ran 15
# checks; it runs 16 today (9 OUTCOME, 7 CONDUCT), and a pass needs all
# of them.
# Promoted out of xfail (cycle 7 sweep, 2026-05-27):
#   5-tenant     — 15/15 confirmed.
#   no-catalog   — 15/15 confirmed.
# Promoted out of xfail (cycle 8, 2026-05-28):
#   single-tenant — 15/15 confirmed after fix in PR #277 (expect pattern
#     case + deterministic disc cycling, closes #256).
# Promoted out of xfail (cycle 9, 2026-05-28):
#   tier1-tier2-missing — 15/15 confirmed after PR #285 (zstandard into
#     RAM dir) + PR #286 (immutable=1 + conn.close() for catalog on
#     iso9660, fixes EBUSY preventing disc swaps).
# Promoted out of xfail (2026-06-15):
#   tier1-missing — 16/16 on two consecutive runs (issue #227): with the
#     tier-1 binary gone, restore.sh rides the fallback path and
#     recovers all data.
#     Deterministic coverage of the chain is pinned by
#     tests/recovery_hardening/test_restore_sh_tier1_missing_multidisc.py.
# The ledger (XFAIL.list) is empty today.
XFAIL_LIST="$HERE/XFAIL.list"
if [ -n "${LCSAS_VARIANT_XFAIL+x}" ]; then
    # Env override (comma-separated) for ad-hoc runs.
    XFAIL="$LCSAS_VARIANT_XFAIL"
else
    # Ledger: collect the first field of each non-comment, non-blank
    # line into a comma-separated set.
    XFAIL=""
    if [ -f "$XFAIL_LIST" ]; then
        while IFS= read -r _ledger_line || [ -n "$_ledger_line" ]; do
            case "$_ledger_line" in
                ''|'#'*) continue ;;
            esac
            _ledger_variant="${_ledger_line%%[ 	]*}"
            [ -n "$_ledger_variant" ] || continue
            XFAIL="${XFAIL:+$XFAIL,}$_ledger_variant"
        done < "$XFAIL_LIST"
    fi
fi
case ",$XFAIL," in
    *",${VARIANT},"*) is_xfail=1 ;;
    *)                is_xfail=0 ;;
esac

if [ "$score_rc" -eq 0 ]; then
    if [ "$is_xfail" -eq 1 ]; then
        # Strict XPASS: a listed variant that scores 16/16 must force
        # the ledger cleanup NOW, not whenever someone reads the log.
        echo "XPASS: variant=$VARIANT passed every check (was expected to fail)"
        echo "       Promote it: remove the '$VARIANT' line from $XFAIL_LIST" >&2
        echo "       and record the 16/16 in run_variant.sh's promotion log." >&2
        finish 1
    fi
    finish 0
fi

if [ "$is_xfail" -eq 1 ]; then
    echo "XFAIL: variant=$VARIANT failed a check (expected)"
    # Expected, but still a failed run: keep its folder.
    LCSAS_BLIND_KEEP_RUN=1 finish 0
fi
finish 1
