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
#
# Exits 0 with `SCORE: 15/15 (variant=<name>)` on full pass.
# Exits non-zero on any failure; the score line still prints.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
VARIANT="${1:?usage: run_variant.sh <variant>}"

case "$VARIANT" in
    default|tier1-missing|tier1-tier2-missing) : ;;
    *)
        echo "ERROR: unknown variant: $VARIANT" >&2
        echo "       supported: default | tier1-missing | tier1-tier2-missing" >&2
        exit 2
        ;;
esac

echo "=== blind-restore variant: $VARIANT ==="

# Each variant rebuilds the fixture from scratch via setup.py with
# LCSAS_VARIANT set; setup.py's _apply_variant_mutations() handles
# the meta-disc surgery (binary removal) per variant before the ISO
# is burned.
export LCSAS_VARIANT="$VARIANT"

# Tier-fallback-requiring variants set LCSAS_TIER_FALLBACK=1 so the
# agent's restore.sh invocation falls through to tier 2 / tier 3.
case "$VARIANT" in
    tier1-missing|tier1-tier2-missing)
        export LCSAS_TIER_FALLBACK=1
        ;;
esac

LCSAS_VARIANT="$VARIANT" \
    "$HERE/setup.py"

RUN_DIR="/tmp/lcsas-blind-variant-${VARIANT}-$$"
mkdir -p "$RUN_DIR"

# 45-minute wall-clock cap, matching make blind-restore.
timeout --foreground 2700 \
    env RUN_DIR="$RUN_DIR" \
        LCSAS_VARIANT="$VARIANT" \
        ${LCSAS_TIER_FALLBACK:+LCSAS_TIER_FALLBACK=$LCSAS_TIER_FALLBACK} \
    "$HERE/run.sh"

last="$(ls -1dt /tmp/lcsas-blind-* 2>/dev/null | head -1)"
verify_out="$("$HERE/verify.sh" "$last" || true)"
echo "$verify_out"
pass_count="$(printf '%s\n' "$verify_out" | grep -c '^PASS' || true)"
fail_count="$(printf '%s\n' "$verify_out" | grep -c '^FAIL' || true)"
total=$((pass_count + fail_count))
echo
echo "SCORE: ${pass_count}/${total} (variant=${VARIANT})"

# Variants that are expected to fail until a tracked bug lands are
# listed in LCSAS_VARIANT_XFAIL (comma-separated).  A red score on an
# xfail variant is reported as XFAIL and exits 0 (it's the baseline we
# expect until the underlying production-code bug is fixed).
# Default xfail set tracks issue #227 (tier-cascade fallback through
# rustic-static can't handle disc-spread packs).
XFAIL="${LCSAS_VARIANT_XFAIL:-tier1-missing,tier1-tier2-missing}"
case ",$XFAIL," in
    *",${VARIANT},"*) is_xfail=1 ;;
    *)                is_xfail=0 ;;
esac

if [ "$pass_count" -eq "$total" ]; then
    if [ "$is_xfail" -eq 1 ]; then
        echo "XPASS: variant=$VARIANT (was expected to fail per #227 — drop from LCSAS_VARIANT_XFAIL)"
    fi
    exit 0
fi

if [ "$is_xfail" -eq 1 ]; then
    echo "XFAIL: variant=$VARIANT scored ${pass_count}/${total} (expected — tracks #227)"
    exit 0
fi
exit 1
