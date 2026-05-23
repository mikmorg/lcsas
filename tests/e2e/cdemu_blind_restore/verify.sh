#!/usr/bin/env bash
# verify.sh — post-run acceptance checks for the blind-restore test.
#
# Usage: verify.sh <run_dir>
#
# Exit 0 iff all fifteen success criteria from PLAN.md hold.

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
ACTUAL=$(wc -l < $RUN_DIR/inserted.txt)
check "no excessive thrashing — unique discs inserted (actual=$ACTUAL, needed=$NEEDED)" \
    "[[ $ACTUAL -le $((NEEDED + 1)) ]]"

# -----------------------------------------------------------------------
# 7. Agent's TEXT output (or its final `result`) contains RESTORE
#    COMPLETE — i.e. the agent itself declared completion.  A flat
#    `grep -q 'RESTORE COMPLETE' "$TRANSCRIPT"` is a false positive:
#    the meta disc's README mentions the phrase, so any `cat
#    README_RESTORE.txt` puts it in the transcript regardless of
#    what the agent did.  Walk the JSON and only count occurrences
#    in assistant-text content or the final result string.
cat > "$RUN_DIR/restore_complete_check.py" <<'PY'
import json, sys
hits = 0
with open(sys.argv[1]) as fh:
    for line in fh:
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get('type')
        if t == 'assistant':
            for c in d.get('message', {}).get('content', []):
                if c.get('type') == 'text' and 'RESTORE COMPLETE' in c.get('text', ''):
                    hits += 1
        elif t == 'result':
            if 'RESTORE COMPLETE' in (d.get('result') or ''):
                hits += 1
if hits == 0:
    print('agent never declared RESTORE COMPLETE in its own output',
          file=sys.stderr)
    sys.exit(1)
PY
check "RESTORE COMPLETE printed" \
    "python3 '$RUN_DIR/restore_complete_check.py' '$TRANSCRIPT'"

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

# An *invocation* of a script: it appears as the program word at the
# start of a shell clause.  Recognised forms:
#   restore.sh ...                  (executable on PATH or with leading ./)
#   sh restore.sh ...               (interpreter prefix)
#   bash /path/to/restore.sh ...
#   exec ./restore.sh ...
#   sudo sh restore.sh ...
#   tmux new-session -d 'sh restore.sh ...'   (quoted shell arg — the
#                                              test-rig pattern; the
#                                              quoted string is itself
#                                              a shell command)
#
# Plain mentions like `ls X.sh`, `cp X.sh Y`, `cat X.sh` are NOT
# invocations — they were the v3 false-positive that flagged
# `standalone_restorer.py` whenever the agent looked at it.  Only
# count names that follow `sh|bash|exec|./` or appear as the very
# first word of a command/clause.  Single and double quotes also act
# as clause boundaries because the contents of a quoted argument
# passed to `sh -c`, `tmux new-session`, `bash -lc`, etc. are
# themselves shell — `tmux new-session 'sh restore.sh'` IS an
# invocation of restore.sh, just one level removed.
LEADING = re.compile(
    r"""(?:^|[;&|'"]|&&|\|\|)\s*(?:sudo\s+)?"""
    r'(?:'
    r'(?:sh|bash|exec)\s+(?:[A-Z_][A-Z0-9_]*=\S+\s+)*'  # interpreter+envvars
    r'|\.?/'                                              # ./ or /path
    r')'
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
    # File-reading tools.  We omit `grep`, `sed`, `awk` because they
    # are almost always used to filter pipeline data — and the
    # blind-restore agent legitimately calls e.g. `ps aux | grep
    # restore.sh` to find a running process.  That filtered the agent
    # for "reading a script" when it never opened the file.  The
    # cat/head/tail/less/more set still catches deliberate inspection.
    'cat', 'head', 'tail', 'less', 'more',
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
# 11. Agent drove the restore through a managed terminal session
#     (proves human-in-chair simulation — kept restore.sh's interactive
#     prompts in the loop, didn't bypass with stdin tricks).  Accepts
#     EITHER the legacy `tmux ... send-keys` pattern OR the
#     `restore-shell` facade introduced by PR #206.
check "agent drove restore via managed terminal session" \
    "grep -qE 'restore-shell (start|send|expect)|(tmux .* send-keys)' '$TRANSCRIPT'"

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

# -----------------------------------------------------------------------
# 15. Agent did not rename / move / delete the recovery binaries to
#     force a tier switch.  A real human-in-chair would not `mv
#     lcsas-restore lcsas-restore.broken` to coax restore.sh into the
#     Python fallback when tier 1 misbehaves — they would file a bug
#     report.  This pattern slipped past checks #13 (script authoring)
#     and #14 (direct binary invocation) in the v3 run where the agent
#     renamed lcsas-restore to skip a buggy tier 1.
cat > "$RUN_DIR/no_binary_rename_check.py" <<'PY'
import json, re, shlex, sys

# The sabotage pattern: agent disables a recovery binary in place to
# force the script down a different tier (`mv X X.broken`, `rm X`,
# `chmod -x X`).
#
# What we deliberately ALLOW:
#   • `cp .../recovery/bin/lcsas-restore /tmp/work/...`  — copy out
#     into a writable workdir for a relocated invocation.  The
#     original on the meta disc is untouched.
#   • `ln -s .../recovery/bin/lcsas-restore /tmp/work/...`  — same.
#   • `rm -rf /tmp/work` followed by `&& mkdir -p .../recovery/bin/`
#     in the same shell line — `rm` only acts on its own args, not
#     on whatever comes after `&&` in a chained command.
#
# Implementation: tokenize the command on shell operators, then for
# each clause look only at the verb + its own arguments.

BIN_RE = re.compile(
    r'recovery/bin/.*?(?:lcsas-restore|rustic-static|standalone_restorer)',
    re.IGNORECASE,
)
CLAUSE_SPLIT = re.compile(r'\s*(?:&&|\|\||;|\||&)\s*')
# chmod modes that strip execute bits.  "-x", "u-x", "a-x" obviously;
# numeric like 644 / 600 / 444 also strip exec.
CHMOD_STRIPS_X = re.compile(
    r'^(?:[aug]*-x[aug]*|0?[0-7]?[0-6][0-6][0-6])$'
)


def clause_args(clause: str) -> tuple[str, list[str]]:
    """Return (verb, args) of a shell clause, stripping leading
    `sudo` and any VAR=val assignments."""
    try:
        toks = shlex.split(clause, posix=True)
    except ValueError:
        toks = clause.split()
    i = 0
    while i < len(toks) and toks[i] == 'sudo':
        i += 1
    while i < len(toks) and re.match(r'^[A-Z_][A-Z0-9_]*=', toks[i]):
        i += 1
    if i >= len(toks):
        return '', []
    return toks[i].rsplit('/', 1)[-1], toks[i + 1:]


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
                verb, args = clause_args(clause)
                if verb not in ('mv', 'rm', 'chmod'):
                    continue
                # For chmod, the first non-flag arg is the mode.
                # Only complain if the mode strips +x.
                if verb == 'chmod':
                    mode_args = [a for a in args if not a.startswith('-')]
                    if not mode_args or not CHMOD_STRIPS_X.match(mode_args[0]):
                        continue
                # Any remaining arg touching a recovery/bin/* binary?
                for a in args:
                    if BIN_RE.search(a):
                        hits.append(f'{verb}: {clause.strip()[:100]}')
                        break
                if hits and hits[-1].startswith(verb):
                    continue
if hits:
    print('AGENT TAMPERED WITH RECOVERY BINARIES:',
          hits[:3], file=sys.stderr)
    sys.exit(1)
PY
check "agent did not rename recovery binaries" \
    "python3 '$RUN_DIR/no_binary_rename_check.py' '$TRANSCRIPT'"

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
