#!/bin/sh
# fetch_upstream.sh -- POSIX-sh downloader for cross-platform recovery binaries.
#
# Reads recovery/UPSTREAM.sha256 (the pinned upstream manifest) and downloads
# every listed artifact into ~/.cache/lcsas/recovery-binaries/, verifying each
# file's SHA-256 against the manifest hash.  Idempotent: a warm cache produces
# no network traffic.  Air-gapped operators can rsync the cache directory
# between machines.
#
# Layout of the cache:
#
#   ~/.cache/lcsas/recovery-binaries/
#       rustic/<target>/<rustic-archive>.tar.gz        ← downloaded archive
#       rustic/<target>/rustic                          ← extracted binary
#       python/<target>/<python-archive>.tar.gz         ← downloaded archive
#       python/<target>/python/                         ← extracted prefix
#
# Override the cache root with $LCSAS_RECOVERY_CACHE.
# Override the manifest path with $LCSAS_UPSTREAM_MANIFEST (default:
# the file next to this script, ../UPSTREAM.sha256).

set -eu

# ── Argument handling ─────────────────────────────────────────────

usage() {
    cat >&2 <<EOF
usage: $0 [--manifest PATH] [--cache PATH] [--no-extract] [--verify-only]

  --manifest PATH   Path to UPSTREAM.sha256 (default: <script-dir>/../UPSTREAM.sha256)
  --cache PATH      Cache root (default: \$LCSAS_RECOVERY_CACHE or
                    ~/.cache/lcsas/recovery-binaries)
  --no-extract      Download + verify only; skip tarball extraction.
  --verify-only     Audit the cache against the manifest.  Never downloads
                    and never extracts.  Exits non-zero on any mismatch or
                    missing artifact.  Used by \`make verify-recovery\`.
  -h, --help        Show this message.

Idempotent: re-runs are no-ops on a warm cache.
EOF
}

MANIFEST=""
CACHE=""
EXTRACT=1
VERIFY_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --manifest) MANIFEST="$2"; shift 2 ;;
        --cache)    CACHE="$2";    shift 2 ;;
        --no-extract) EXTRACT=0;   shift   ;;
        --verify-only) VERIFY_ONLY=1; EXTRACT=0; shift ;;
        -h|--help)  usage; exit 0 ;;
        *)          printf 'unknown argument: %s\n' "$1" >&2; usage; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd -P)"
: "${MANIFEST:=${SCRIPT_DIR}/../UPSTREAM.sha256}"
: "${CACHE:=${LCSAS_RECOVERY_CACHE:-${HOME}/.cache/lcsas/recovery-binaries}}"

if [ ! -r "$MANIFEST" ]; then
    printf 'manifest not readable: %s\n' "$MANIFEST" >&2
    exit 1
fi

# ── Tool detection (curl preferred, wget fallback) ────────────────

if command -v curl >/dev/null 2>&1; then
    DL_CMD="curl -fsSL -o"
elif command -v wget >/dev/null 2>&1; then
    DL_CMD="wget -q -O"
else
    printf 'neither curl nor wget available; cannot fetch\n' >&2
    exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
    SHA_CMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    SHA_CMD="shasum -a 256"
else
    printf 'neither sha256sum nor shasum available; cannot verify\n' >&2
    exit 1
fi

# ── Upstream source URLs ──────────────────────────────────────────
#
# These are derived from the artifact filename and the category prefix
# in the UPSTREAM.sha256 manifest.  Updating the manifest version
# headers also requires updating these URL bases.

RUSTIC_BASE="https://github.com/rustic-rs/rustic/releases/download/v0.11.2"
PYTHON_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/20260510"

resolve_url() {
    # $1: category (rustic | python)
    # $2: filename (basename of the artifact)
    case "$1" in
        rustic) printf '%s/%s\n' "$RUSTIC_BASE" "$2" ;;
        python) printf '%s/%s\n' "$PYTHON_BASE" "$2" ;;
        *) printf 'unknown category: %s\n' "$1" >&2; return 1 ;;
    esac
}

# ── Per-line processor ────────────────────────────────────────────

mkdir -p "$CACHE"

failures=0
total=0

process_line() {
    # $1: expected SHA-256
    # $2: relative path like "rustic/<target>/<filename>"
    expected_sha="$1"
    relpath="$2"

    # Split relpath into category, target, filename.
    category="${relpath%%/*}"
    rest="${relpath#*/}"
    target="${rest%%/*}"
    filename="${rest#*/}"

    dest_dir="$CACHE/$category/$target"
    dest_file="$dest_dir/$filename"
    mkdir -p "$dest_dir"

    total=$((total + 1))

    # If the file already exists and matches the expected hash, skip.
    if [ -f "$dest_file" ]; then
        actual_sha="$($SHA_CMD "$dest_file" | awk '{print $1}')"
        if [ "$actual_sha" = "$expected_sha" ]; then
            printf '[cached] %s\n' "$relpath" >&2
        elif [ "$VERIFY_ONLY" -eq 1 ]; then
            printf '[error]  SHA mismatch for %s\n         expected %s\n         got      %s\n' \
                "$relpath" "$expected_sha" "$actual_sha" >&2
            failures=$((failures + 1))
            return
        else
            printf '[stale]  %s — removing and re-downloading\n' "$relpath" >&2
            rm -f "$dest_file"
        fi
    fi

    if [ ! -f "$dest_file" ]; then
        if [ "$VERIFY_ONLY" -eq 1 ]; then
            printf '[error]  missing from cache: %s\n' "$relpath" >&2
            failures=$((failures + 1))
            return
        fi
        url="$(resolve_url "$category" "$filename")"
        printf '[fetch]  %s\n' "$url" >&2
        if ! $DL_CMD "$dest_file" "$url"; then
            printf '[error]  download failed: %s\n' "$url" >&2
            rm -f "$dest_file"
            failures=$((failures + 1))
            return
        fi
        actual_sha="$($SHA_CMD "$dest_file" | awk '{print $1}')"
        if [ "$actual_sha" != "$expected_sha" ]; then
            printf '[error]  SHA mismatch for %s\n         expected %s\n         got      %s\n' \
                "$relpath" "$expected_sha" "$actual_sha" >&2
            rm -f "$dest_file"
            failures=$((failures + 1))
            return
        fi
        printf '[ok]     %s\n' "$relpath" >&2
    fi

    # ── Extraction step ───────────────────────────────────────────
    if [ "$EXTRACT" -eq 0 ]; then
        return
    fi

    marker="$dest_dir/.extracted"
    if [ -f "$marker" ] && [ "$(cat "$marker")" = "$expected_sha" ]; then
        return
    fi

    case "$category" in
        rustic)
            # rustic tarballs unpack to a single `rustic` binary (sometimes
            # inside a subdirectory; tar --strip-components=0 with --wildcards
            # is portable enough).
            tar -xzf "$dest_file" -C "$dest_dir"
            # Locate the rustic binary (named "rustic" or "rustic.exe").
            for cand in "$dest_dir/rustic" "$dest_dir/rustic.exe"; do
                if [ -f "$cand" ]; then
                    chmod +x "$cand" 2>/dev/null || true
                    break
                fi
            done
            # If the archive nests under a subdir, flatten it.
            for sub in "$dest_dir"/*/rustic "$dest_dir"/*/rustic.exe; do
                if [ -f "$sub" ]; then
                    mv "$sub" "$dest_dir/"
                    chmod +x "$dest_dir/rustic"* 2>/dev/null || true
                fi
            done
            ;;
        python)
            # PBS tarballs unpack to a "python/" prefix tree.
            tar -xzf "$dest_file" -C "$dest_dir"
            ;;
    esac
    printf '%s' "$expected_sha" > "$marker"
}

# ── Walk the manifest ─────────────────────────────────────────────

while IFS= read -r line; do
    # Skip blanks and comments.
    case "$line" in
        ""|"#"*) continue ;;
    esac
    # Parse "<hash><space><space><path>" — matches sha256sum format.
    sha="${line%% *}"
    path="${line#*  }"
    # Defensive: if there were no two spaces, the line is malformed.
    if [ "$sha" = "$line" ] || [ "$path" = "$line" ]; then
        printf '[skip]   malformed manifest line: %s\n' "$line" >&2
        continue
    fi
    process_line "$sha" "$path"
done < "$MANIFEST"

printf '\n' >&2
if [ "$failures" -gt 0 ]; then
    printf '%d of %d artifacts failed.  Cache root: %s\n' "$failures" "$total" "$CACHE" >&2
    exit 1
fi
printf 'all %d artifacts verified and cached at %s\n' "$total" "$CACHE" >&2
exit 0
