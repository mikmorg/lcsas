#!/usr/bin/env bash
# run.sh — spawn a headless claude sub-agent as the lcsas-blind user,
# capture its stream-json transcript, and interleave the disc-loader
# log into a human-readable timeline.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
RUN_DIR="${RUN_DIR:-/tmp/lcsas-blind-run-$(date +%s)}"
mkdir -p "$RUN_DIR"

# Prevent overlapping runs — only one blind-restore at a time.
LOCKFILE="/tmp/lcsas-blind-restore.lock"
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    echo "ERROR: another blind-restore run is already in progress." >&2
    echo "       Lock held by: $(cat "$LOCKFILE" 2>/dev/null || echo unknown)" >&2
    exit 1
fi
echo "PID $$ started $(date -Iseconds)" >&9

cp "$HERE/agent_prompt.txt" "$RUN_DIR/prompt.txt"

# Clean stale state inside the agent's playground so each run starts
# from a known-empty world. The agent typically scratches in /tmp and
# /home/lcsas-blind/{mnt,restored}; leftover state from a prior run
# (especially a half-built rustic cache or extracted meta tree) misleads
# the next agent into "discovering" data it didn't actually restore.
sudo bash -c '
    # Kill any stale lcsas-blind processes from a prior run so they
    # do not race on disc-loader and pollute the log.
    pkill -u lcsas-blind 2>/dev/null || true
    sleep 1
    pkill -9 -u lcsas-blind 2>/dev/null || true
    umount /dev/sr0 2>/dev/null || true
    umount /home/lcsas-blind/mnt 2>/dev/null || true
    rm -rf /home/lcsas-blind/restored/* /home/lcsas-blind/mnt/* 2>/dev/null || true
    rm -rf /tmp/lcsas-meta /tmp/lcsas-work /tmp/disc /tmp/disc1 \
           /tmp/lcsas-restore-* /tmp/catalog.db /tmp/lcsas_cache \
           /tmp/alpha_packs* 2>/dev/null || true
'
# Eject any leftover disc so the agent starts with an empty drive.
disc-loader eject >/dev/null 2>&1 || true

# Root-owned log file — the lcsas-blind user writes to it only via the
# setuid disc-loader wrapper and cannot read it back.
sudo install -m 0640 -o root -g root /dev/null "$RUN_DIR/disc-loader.log"
sudo ln -sf "$RUN_DIR/disc-loader.log" /var/log/disc-loader.log

MAX_TURNS="${MAX_TURNS:-150}"
PROMPT="$(cat "$RUN_DIR/prompt.txt")"

# Run claude as lcsas-blind. --allowed-tools is restricted to Bash so
# the agent has to use the shell for everything — same posture as a
# human at a bare terminal.
PROMPT_FILE="$RUN_DIR/prompt.txt"
sudo -u lcsas-blind -H bash -lc "
    cd ~ &&
    HOME=/home/lcsas-blind \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    /usr/local/bin/claude \
        -p \"\$(cat '$PROMPT_FILE')\" \
        --output-format stream-json \
        --verbose \
        --allowed-tools Bash \
        --disallowed-tools Read,Edit,Write,Glob,Grep,WebFetch,WebSearch \
        --max-turns $MAX_TURNS
" > "$RUN_DIR/transcript.jsonl" 2> "$RUN_DIR/agent-stderr.log" \
    || echo "claude exited non-zero (see agent-stderr.log)" >&2

python3 "$HERE/merge_timeline.py" \
    "$RUN_DIR/transcript.jsonl" \
    "$RUN_DIR/disc-loader.log" \
    > "$RUN_DIR/timeline.txt" 2>/dev/null || true

echo "run artifacts: $RUN_DIR"
echo "  transcript:  $RUN_DIR/transcript.jsonl"
echo "  disc log:    $RUN_DIR/disc-loader.log"
echo "  timeline:    $RUN_DIR/timeline.txt"
