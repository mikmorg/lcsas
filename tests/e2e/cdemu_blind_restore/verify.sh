#!/usr/bin/env bash
# verify.sh — post-run acceptance checks for the blind-restore test.
#
# Usage: verify.sh <run_dir>
#
# Exit 0 iff all fourteen success criteria from PLAN.md hold.

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

# Fail-closed fixture-presence guard.  Without these files, the
# manifest comparisons below trivially match (comm against an empty
# set is empty) and the run reports a false PASS.  Refuse to score.
for f in alpha_manifest.sha256 bravo_manifest.sha256 expected_alpha_volumes.txt; do
    if ! sudo test -s "$FIXTURE/$f"; then
        echo "FAIL  fixture inputs missing: $FIXTURE/$f not present or empty" >&2
        echo "      verify.sh refuses to score without the ground-truth fixture." >&2
        exit 2
    fi
done

PASS=0
FAIL=0
check() {
    local name="$1" cmd="$2"
    if bash -c "$cmd" >/dev/null 2>&1; then
        echo "PASS  $name"
        PASS=$((PASS + 1))
    else
        echo "FAIL  $name"
        FAIL=$((FAIL + 1))
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
check "alpha manifest matches" \
    "comm -23 '$RUN_DIR/expected_by_name.txt' '$RUN_DIR/restored_by_name.txt' | (! grep -q .)"

# -----------------------------------------------------------------------
# 2. No bravo pack plaintext leaked into restored/.
#    Hash-set intersection — compare restored SHA-256s to the known
#    bravo file hashes. Any overlap means bravo bled through.
sudo find "$AGENT_HOME/restored" -type f -print0 \
    | sudo xargs -0 sha256sum 2>/dev/null \
    | awk '{print $1}' | sort -u > "$RUN_DIR/restored.sha"
sudo cat "$FIXTURE/bravo_manifest.sha256" | awk '{print $1}' | sort -u > "$RUN_DIR/bravo.sha"
check "no bravo leak" \
    "[[ -z \"\$(comm -12 '$RUN_DIR/restored.sha' '$RUN_DIR/bravo.sha')\" ]]"

# -----------------------------------------------------------------------
# 3. No forbidden illusion-leak tokens in the agent's own shell commands.
#    We only look at the agent's Bash input strings (tool_use.input.command),
#    not tool output — mount's stderr mentions dmesg, and the production
#    README-RESTORE.md legitimately mentions .iso. Those reach the agent
#    through legitimate channels. What we care about is whether the agent
#    *typed* a forbidden token, which would mean it pierced the illusion.
cat > "$RUN_DIR/illusion_check.py" <<'PY'
import json, re, sys
pat = re.compile(r'cdemu|vhba|lsmod|modinfo|udevadm|/var/lib/disc-vault|/opt/disc-robot|loopback', re.IGNORECASE)
hits = []
with open(sys.argv[1]) as f:
    for line in f:
        try: d = json.loads(line)
        except Exception: continue
        content = d.get('message', {}).get('content', [])
        if not isinstance(content, list): continue
        for c in content:
            if c.get('type') == 'tool_use':
                cmd = c.get('input', {}).get('command', '')
                m = pat.search(cmd)
                if m: hits.append(m.group(0))
if hits:
    print('FORBIDDEN TOKENS IN AGENT COMMANDS:', hits[:5], file=sys.stderr)
    sys.exit(1)
PY
check "illusion intact" "python3 '$RUN_DIR/illusion_check.py' '$TRANSCRIPT'"

# -----------------------------------------------------------------------
# 5. First disc the *agent* asked for was LCSAS_META.
#    We check the transcript (agent's commands), not the disc-loader log,
#    because stale processes from prior runs can race into the log before
#    the current agent starts.
FIRST_INSERT="$(python3 -c "
import json, re, sys
with open('$TRANSCRIPT') as f:
    for line in f:
        try: d = json.loads(line)
        except: continue
        content = d.get('message', {}).get('content', [])
        if not isinstance(content, list): continue
        for c in content:
            if c.get('type') == 'tool_use':
                cmd = c.get('input', {}).get('command', '')
                m = re.search(r'disc-loader\s+insert\s+(LCSAS_[A-Z0-9_]+)', cmd)
                if m:
                    print(m.group(1))
                    sys.exit(0)
" 2>/dev/null)"
check "first disc was LCSAS_META" \
    "[[ '$FIRST_INSERT' == 'LCSAS_META' ]]"

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
check "all required discs inserted" \
    "[[ -z \"\$(comm -23 $RUN_DIR/expected.txt $RUN_DIR/inserted.txt)\" ]]"

NEEDED=$(wc -l < $RUN_DIR/expected.txt)
ACTUAL=$($DISC_LOG_CAT 2>/dev/null \
    | grep -oE 'insert LCSAS_[A-Z0-9_]+' \
    | grep -v ' LCSAS_META$' \
    | wc -l)
check "no excessive thrashing (actual=$ACTUAL, needed=$NEEDED)" \
    "[[ $ACTUAL -le $((NEEDED * 5)) ]]"

# -----------------------------------------------------------------------
# 7. Agent's transcript includes the production "RESTORE COMPLETE" string.
check "RESTORE COMPLETE printed" \
    "grep -q 'RESTORE COMPLETE' '$TRANSCRIPT'"

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
check "meta disc has no catalog.db" \
    "[[ '$HAS_CATALOG' == 'no' ]]"
check "meta disc has metadata/" \
    "[[ '$HAS_METADATA' == 'yes' ]]"

# -----------------------------------------------------------------------
# 9. Agent invoked the interactive restore.sh — never the auto / legacy /
#    standalone variants directly.  (restore.sh itself may exec whichever
#    tier is appropriate; we only forbid the agent typing those names at
#    the top of a shell command.)
cat > "$RUN_DIR/script_invoke_check.py" <<'PY'
import json, re, sys

# Leading invocation: optional "sudo "/"sh "/"bash "/"exec ", path-ish
# basename ending in .sh/.py/.bat, then whitespace or EOL.
LEADING = re.compile(
    r'(?:^|[;&|]|\s)(?:sudo\s+)?(?:sh|bash|exec)?\s*(?:\./|/)?'
    r'(?P<name>[A-Za-z0-9_./-]+\.(?:sh|py|bat))(?:\s|$)'
)
FORBIDDEN = {
    'restore-auto.sh', 'restore_legacy.sh', 'restore_c89.sh',
    'restore.bat', 'standalone_restorer.py',
}
saw_restore_sh = False
forbidden_hits = []
with open(sys.argv[1]) as fh:
    for line in fh:
        try:
            d = json.loads(line)
        except Exception:
            continue
        content = d.get('message', {}).get('content', [])
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get('type') != 'tool_use':
                continue
            cmd = c.get('input', {}).get('command', '')
            for m in LEADING.finditer(cmd):
                base = m.group('name').rsplit('/', 1)[-1]
                if base in FORBIDDEN:
                    forbidden_hits.append(base)
                if base == 'restore.sh':
                    saw_restore_sh = True
if forbidden_hits:
    print('AGENT INVOKED FORBIDDEN SCRIPT(S):',
          sorted(set(forbidden_hits)), file=sys.stderr)
    sys.exit(1)
if not saw_restore_sh:
    print('AGENT NEVER INVOKED restore.sh', file=sys.stderr)
    sys.exit(1)
PY
check "agent ran restore.sh (no auto/legacy/standalone)" \
    "python3 '$RUN_DIR/script_invoke_check.py' '$TRANSCRIPT'"

# -----------------------------------------------------------------------
# 10. Agent did not read any script file (cat/head/less/sed/awk/etc. on
#     *.sh, *.py, *.bat, *.c, *.h).  Reading READMEs and JSON metadata is
#     allowed — we only forbid inspecting code.
cat > "$RUN_DIR/script_read_check.py" <<'PY'
import json, re, shlex, sys

READERS = {
    'cat', 'head', 'tail', 'less', 'more', 'sed', 'awk', 'grep',
    'strings', 'od', 'xxd', 'view', 'nano', 'vim', 'vi',
}
SCRIPT_EXT = re.compile(r'\.(?:sh|py|bat|c|h)(?:$|[\s,;:])')

# Split a command into top-level clauses on shell operators we care
# about; lets us detect "cat README.md && cat restore.sh".
CLAUSE_SPLIT = re.compile(r'\s*(?:&&|\|\||;|\||&)\s*')

hits = []
with open(sys.argv[1]) as fh:
    for line in fh:
        try:
            d = json.loads(line)
        except Exception:
            continue
        content = d.get('message', {}).get('content', [])
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get('type') != 'tool_use':
                continue
            cmd = c.get('input', {}).get('command', '')
            for clause in CLAUSE_SPLIT.split(cmd):
                try:
                    tokens = shlex.split(clause, posix=True)
                except ValueError:
                    tokens = clause.split()
                if not tokens:
                    continue
                # Strip a leading 'sudo' (and its flags) before checking
                # argv[0].  Stop at the first non-flag.
                i = 0
                if tokens[i] == 'sudo':
                    i += 1
                    while i < len(tokens) and tokens[i].startswith('-'):
                        i += 1
                if i >= len(tokens):
                    continue
                argv0 = tokens[i].rsplit('/', 1)[-1]
                if argv0 not in READERS:
                    continue
                for tok in tokens[i + 1:]:
                    if SCRIPT_EXT.search(tok):
                        hits.append(f'{argv0} {tok}')
                        break
if hits:
    print('AGENT READ SCRIPT FILE(S):', hits[:5], file=sys.stderr)
    sys.exit(1)
PY
check "agent did not cat any script" \
    "python3 '$RUN_DIR/script_read_check.py' '$TRANSCRIPT'"

# -----------------------------------------------------------------------
# 11. Agent drove the restore through tmux (proves human-in-chair
#     simulation).  Requires both a `tmux ...` command and at least one
#     `send-keys`.
check "agent used tmux send-keys" \
    "grep -q 'tmux ' '$TRANSCRIPT' && grep -q 'send-keys' '$TRANSCRIPT'"

# -----------------------------------------------------------------------
# 12. Agent did not author wrapper scripts.  A real human-in-the-chair
#     would not write `/tmp/pyshim/python3` or a fake `lcsas-restore`
#     bash wrapper to translate CLI flags when the production binary
#     misbehaves.  If `restore.sh` fails, capture the error and quit —
#     do not improvise.  Tripwires: heredocs writing to /tmp/, or
#     chmod +x against any path under /tmp/, /home/lcsas-blind/, or
#     /var/tmp/.
cat > "$RUN_DIR/no_authoring_check.py" <<'PY'
import json, re, sys

HEREDOC = re.compile(
    r'(?:cat|tee)\s+(?:-a\s+)?>\s*(?P<path>[/\w._-]+)\s*<<', re.IGNORECASE
)
CHMODX = re.compile(
    r'chmod\s+(?:-R\s+)?[+aug]*x[+aug]*\s+(?P<path>[/\w._-]+)'
)
WRITABLE_PREFIX = re.compile(
    r'^(?:/tmp/|/var/tmp/|/home/lcsas-blind/|~)'
)
EXEC_SUFFIX = re.compile(r'\.(?:sh|py|bash)$')

hits = []
with open(sys.argv[1]) as fh:
    for line in fh:
        try:
            d = json.loads(line)
        except Exception:
            continue
        content = d.get('message', {}).get('content', [])
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get('type') != 'tool_use':
                continue
            cmd = c.get('input', {}).get('command', '')
            for m in HEREDOC.finditer(cmd):
                path = m.group('path')
                if WRITABLE_PREFIX.match(path) and (
                    EXEC_SUFFIX.search(path) or '/pyshim/' in path
                    or '/wrapper' in path or path.endswith('/python3')
                    or path.endswith('/lcsas-restore')
                ):
                    hits.append(f'wrote {path}')
            for m in CHMODX.finditer(cmd):
                path = m.group('path')
                if WRITABLE_PREFIX.match(path):
                    hits.append(f'chmod +x {path}')
if hits:
    print('AGENT AUTHORED WRAPPER SCRIPT(S):', hits[:5], file=sys.stderr)
    sys.exit(1)
PY
check "agent did not author wrapper scripts" \
    "python3 '$RUN_DIR/no_authoring_check.py' '$TRANSCRIPT'"

# -----------------------------------------------------------------------
# 13. Agent did not invoke recovery binaries directly.  `restore.sh` is
#     the only allowed top-level recovery driver; it will `exec` rustic
#     / rustic-static / lcsas-restore / standalone_restorer.py
#     internally.  The agent invoking them as argv[0] means it bypassed
#     restore.sh — a clear "I gave up on the production path" signal.
#     `--version` / `--help` invocations are exempt (those are just
#     probing the binary, not running a restore through it).
cat > "$RUN_DIR/no_bypass_check.py" <<'PY'
import json, re, shlex, sys

BINARIES = {
    'rustic', 'rustic-static', 'lcsas-restore', 'standalone_restorer.py',
    'restic',
}
CLAUSE_SPLIT = re.compile(r'\s*(?:&&|\|\||;|\||&)\s*')
hits = []
with open(sys.argv[1]) as fh:
    for line in fh:
        try:
            d = json.loads(line)
        except Exception:
            continue
        content = d.get('message', {}).get('content', [])
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get('type') != 'tool_use':
                continue
            cmd = c.get('input', {}).get('command', '')
            for clause in CLAUSE_SPLIT.split(cmd):
                try:
                    tokens = shlex.split(clause, posix=True)
                except ValueError:
                    tokens = clause.split()
                if not tokens:
                    continue
                # Strip leading sudo / sh / bash / exec wrappers.
                i = 0
                while i < len(tokens) and tokens[i] in (
                    'sudo', 'sh', 'bash', 'exec'
                ):
                    i += 1
                    while i < len(tokens) and tokens[i].startswith('-'):
                        i += 1
                if i >= len(tokens):
                    continue
                argv0 = tokens[i].rsplit('/', 1)[-1]
                if argv0 not in BINARIES:
                    continue
                # Allow probing: --version / --help / -V are not a
                # bypass; we only flag invocations that look like an
                # actual restore.
                rest = tokens[i + 1:]
                if rest and rest[0] in (
                    '--version', '-V', '--help', '-h', 'version', 'help'
                ):
                    continue
                hits.append(f'{argv0} {" ".join(rest[:3])}'.strip())
if hits:
    print('AGENT BYPASSED restore.sh to call binary directly:',
          hits[:5], file=sys.stderr)
    sys.exit(1)
PY
check "agent did not bypass restore.sh (no direct rustic/lcsas-restore/standalone)" \
    "python3 '$RUN_DIR/no_bypass_check.py' '$TRANSCRIPT'"

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
