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
            if [ ! -f "$src" ]; then
                printf 'WARN: missing source %s; placeholder zero-byte file\n' \
                    "$src" >&2
                mkdir -p "$STAGING$(dirname "$target")"
                : > "$STAGING$target"
            else
                mkdir -p "$STAGING$(dirname "$target")"
                cp "$src" "$STAGING$target"
            fi
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

printf 'wrote %s (%s bytes)\n' "$OUT" "$(wc -c < "$OUT")" >&2
