#!/bin/sh
# build_initramfs.sh -- assemble a deterministic initramfs cpio.gz.
#
# Usage:
#   sh build_initramfs.sh ARCH OUT_FILE
#
# Reads boot/initramfs/manifest.txt and produces a gzipped cpio (newc
# format) suitable for use as initrd= in isolinux.cfg / grub.cfg.
#
# Deterministic: SOURCE_DATE_EPOCH-driven mtime, sorted entries, gzip -n.
set -eu

if [ $# -lt 2 ]; then
    printf 'usage: %s ARCH OUT_FILE\n' "$0" >&2
    exit 2
fi

ARCH="$1"
OUT="$2"
ROOT="${RECOVERY_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
MANIFEST="$ROOT/boot/initramfs/manifest.txt"
SDE="${SOURCE_DATE_EPOCH:-1735689600}"
STAGING="$(mktemp -d /tmp/lcsas-initramfs.XXXXXX)"
trap 'rm -rf "$STAGING"' EXIT INT TERM

# Substitute {{ARCH}} on the fly while iterating.
while IFS= read -r raw; do
    line="$(printf '%s' "$raw" | sed "s/{{ARCH}}/$ARCH/g")"
    case "$line" in
        ''|'#'*) continue ;;
    esac
    set -- $line
    kind="$1"; shift
    case "$kind" in
        d)
            target="$1"; mode="$2"
            mkdir -p "$STAGING$target"
            chmod "$mode" "$STAGING$target"
            ;;
        f)
            src="$ROOT/$1"; target="$2"; mode="$3"
            # Hard-fail on missing OR empty sources: an initramfs with a
            # zero-byte /bin/busybox (or /init) is a black screen for the
            # heir.  A build that cannot produce a working artifact must
            # fail loud, never ship placeholders.
            if [ ! -f "$src" ] || [ ! -s "$src" ]; then
                printf 'ERROR: manifest source missing or empty: %s (for %s)\n' \
                    "$src" "$target" >&2
                printf 'ERROR: refusing to build an initramfs with placeholder files.\n' >&2
                exit 1
            fi
            mkdir -p "$STAGING$(dirname "$target")"
            cp "$src" "$STAGING$target"
            chmod "$mode" "$STAGING$target"
            ;;
        s)
            link="$1"; tgt="$2"
            mkdir -p "$STAGING$(dirname "$link")"
            ln -sfn "$tgt" "$STAGING$link"
            ;;
    esac
done < "$MANIFEST"

# Force deterministic mtime on everything.
find "$STAGING" -depth -exec touch -h -d "@$SDE" {} +

# Build newc cpio with sorted entries.
( cd "$STAGING" && find . -print | LC_ALL=C sort \
    | cpio -o -H newc --reproducible 2>/dev/null \
    | gzip -n -9 ) > "$OUT"

# Defense in depth: the archive must not contain any zero-byte regular
# file (the f-branch above should make this unreachable).  The pytest
# gate (tests/recovery_hardening/test_initramfs_manifest_sources.py)
# is the authoritative check.
empties="$(gzip -dc "$OUT" | cpio -tv 2>/dev/null | awk '/^-/ && $5 == 0')"
if [ -n "$empties" ]; then
    printf 'ERROR: built initramfs contains zero-byte regular files:\n%s\n' \
        "$empties" >&2
    rm -f "$OUT"
    exit 1
fi

printf 'wrote %s (%s bytes)\n' "$OUT" "$(wc -c < "$OUT")" >&2
