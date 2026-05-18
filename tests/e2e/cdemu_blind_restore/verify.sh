#!/usr/bin/env bash
# verify.sh — post-run acceptance checks for the blind-restore test.
#
# Usage: verify.sh <run_dir>
#
# Exit 0 iff all nine success criteria from PLAN.md hold.

set -uo pipefail

RUN_DIR="${1:-}"
if [[ -z "$RUN_DIR" ]]; then
    echo "usage: verify.sh <run_dir>" >&2
    exit 64
fi

AGENT_HOME="${AGENT_HOME:-/home/lcsas-blind}"
FIXTURE="${FIXTURE:-/mnt/lcsas-data/blind-test}"
TRANSCRIPT="$RUN_DIR/transcript.jsonl"
DISC_LOG="$RUN_DIR/disc-loader.log"

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

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
