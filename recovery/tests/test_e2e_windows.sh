#!/bin/sh
# test_e2e_windows.sh -- run the end-to-end e2e fixture through the
# Windows binary under Wine.
#
# Builds a synthetic restic repo via tests/test_e2e.py's build_repo,
# invokes bin/x86_64-windows/lcsas-restore.exe under Wine, and verifies
# every restored file matches the original byte-for-byte.

set -eu

RECOVERY="$(cd "$(dirname "$0")/.." && pwd -P)"
EXE="$RECOVERY/bin/x86_64-windows/lcsas-restore.exe"

if [ ! -x "$EXE" ]; then
    EXE="$RECOVERY/build/x86_64-windows/lcsas-restore.exe"
fi
if [ ! -f "$EXE" ]; then
    printf 'SKIP: Windows .exe not built (run: make windows)\n' >&2
    exit 0
fi
if ! command -v wine >/dev/null 2>&1; then
    printf 'SKIP: wine not installed\n' >&2
    exit 0
fi

TMP="$(mktemp -d /tmp/lcsas_e2e_win.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT INT TERM

REPO="$TMP/repo"
TARGET="$TMP/restored"
PWFILE="$TMP/pw"
printf 'correct-horse-battery-staple\n' > "$PWFILE"

# Build fixture using the existing helper.
python3 -c "
import sys
sys.path.insert(0, '$RECOVERY/tests')
sys.path.insert(0, '$RECOVERY/../src')
import test_e2e
from pathlib import Path
files = {
    'hello.txt':  b'Hello, Windows recovery path!\n',
    'binary.bin': bytes(range(256)) * 64,
    'compress.txt': b'banana ' * 8192,
}
test_e2e.build_repo(Path('$REPO'), 'correct-horse-battery-staple',
                    files, v2=True)   # v2 exercises the zstd path
" >/dev/null

mkdir -p "$TARGET"

WINEDEBUG=-all wine "$EXE" \
    --repo "$REPO" \
    --password-file "$PWFILE" \
    --target "$TARGET" \
    --snapshot latest >/dev/null 2>"$TMP/stderr" || {
    printf 'FAIL: wine .exe exit non-zero\n' >&2
    cat "$TMP/stderr" >&2
    exit 1
}

# Verify byte equality.
fails=0
for name in hello.txt binary.bin compress.txt; do
    if [ ! -f "$TARGET/$name" ]; then
        printf 'FAIL: %s not restored\n' "$name" >&2
        fails=$((fails + 1))
        continue
    fi
done

# Use Python to compare bytes (cmp would do but Python is already there).
python3 -c "
from pathlib import Path
expected = {
    'hello.txt':  b'Hello, Windows recovery path!\n',
    'binary.bin': bytes(range(256)) * 64,
    'compress.txt': b'banana ' * 8192,
}
target = Path('$TARGET')
import sys
fails = 0
for name, content in expected.items():
    got = (target / name).read_bytes()
    if got != content:
        print(f'FAIL: {name} mismatch (got {len(got)} bytes, want {len(content)})', file=sys.stderr)
        fails += 1
sys.exit(fails)
"

if [ $fails -eq 0 ]; then
    printf 'test_e2e_windows: OK (v2 restic repo restored via wine + lcsas-restore.exe)\n'
fi
exit $fails
