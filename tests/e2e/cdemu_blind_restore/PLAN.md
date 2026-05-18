# LCSAS — Blind-Agent Disaster Recovery Test Plan

**Version: v2 (Interactive driver — May 2026)**

**Scenario:** A disaster wipes everything except a stack of labelled optical
discs, a single CD/DVD drive, and a sticky note with one encryption password.
An operator with **zero prior knowledge of LCSAS** must restore one
repository's data using only those discs. We simulate that operator with an
LLM sub-agent given minimal context, driving a virtual optical drive via
`cdemu`. The sub-agent must believe the drive is real **and must drive the
restore through an interactive `restore.sh` session — exactly like a human
at the keyboard.**

This is the ultimate end-to-end test of the **single-drive** holographic
restore claim: any disc, picked blind, should be enough to bootstrap a full
restore using nothing but what the meta disc ships out of the box.

> **Important:** this test exercises the **production** `lcsas meta build`
> output unmodified. There are no overlay scripts, no overlay README, no
> patched `restore.sh`. If the blind agent cannot complete the restore using
> only the production meta disc and the prompts the production `restore.sh`
> emits, the **feature** has not shipped — that is the acceptance gate.

---

## Success Criteria

A run passes iff **all** of the following hold after the agent declares
`RESTORE COMPLETE`:

1. Every file originally backed up for **repository alpha** exists under
   `~agent/restored/` with byte-identical contents (SHA-256 match against
   the pre-disaster manifest).
2. **Zero** files belonging to repository **bravo** appear anywhere in
   `~agent/restored/` — bravo's password was never given to the agent and
   its data lives on the same discs (multi-tenant isolation proof). The
   check is a SHA-256 set intersection, not a filename grep.
3. The agent never touched `/dev/sr0` directly (only via the disc-loader
   wrapper) — verified from the transcript.
4. The agent's transcript contains **none** of the forbidden illusion-leak
   tokens: `cdemu`, `vhba`, `\.iso(\b|$)`, `dmesg`, `lsmod`, `modinfo`,
   `udevadm`, `/var/lib/disc-vault`, `libexec`, `loopback`. Any hit
   invalidates the run.
5. The first successful `disc-loader insert` in the run timeline is
   `LCSAS_META`. Proves the agent honoured the one hint in the prompt.
6. The set of inserted disc labels is a **superset** of the
   pre-computed `expected_alpha_volumes.txt` (every required disc was
   visited). Bounded above by `5 × needed` to catch thrashing.
7. The full transcript is captured to `transcript.jsonl` for review.
8. **(v2)** The agent invoked the interactive `restore.sh`.  Direct
   invocation of `restore-auto.sh`, `restore_legacy.sh`,
   `restore_c89.sh`, `restore.bat`, or `standalone_restorer.py`
   fails the run.  (`restore.sh` may itself `exec` whichever tier is
   appropriate — that is allowed; we are checking what the *agent*
   typed at the top of a shell command.)
9. **(v2)** The agent never *read* a script file.  No
   `cat`/`head`/`tail`/`less`/`more`/`sed`/`awk`/`grep`/`strings`/
   `od`/`xxd`/`view`/`vi`/`vim`/`nano` applied to a file ending in
   `.sh`, `.py`, `.bat`, `.c`, or `.h`.  Reading markdown / text /
   JSON is fine — the rule covers code only.
10. **(v2)** The agent drove the restore from a `tmux` session and
    used `tmux send-keys` to respond to the script's prompts.  This
    proves the test exercised the same UX a human operator would
    see, not a scripted-pipe shortcut.

---

## Threat Model (what this test is actually probing)

- **Blind bootstrap:** can someone who has never heard of LCSAS figure out
  the restore procedure from the meta disc alone?
- **Single-drive default:** does the production `restore.sh` actually drive
  the swap loop without `--isos`, just `--key`/`--target`/`--repo`?
- **Holographic catalog:** does every disc carry enough metadata that any
  one of them, inserted first, identifies the remaining pick list?
- **Multi-tenant isolation:** cannot decrypt foreign repos even when their
  metadata and packs are physically present on the same discs.
- **Self-describing instructions:** are the docs on the meta disc good
  enough that an LLM with no prior context converges on the right commands?
- **Interactive UX parity (v2):** the recovery script the user is asked
  to run is `restore.sh` — interactive, prompts for password and for
  disc swaps.  The test must exercise that flow as a human would: a
  persistent terminal (tmux), typed password via `send-keys`, response
  to disc-swap prompts.  Pipe-based / env-var-fed shortcuts and direct
  invocations of `restore-auto.sh` are explicitly out-of-scope and are
  detected by `verify.sh`.

---

## Architecture

```
┌─────────────────────────────── Operator (us) ──────────────────────────────┐
│  setup.py                                                                  │
│  ├── generate alpha + bravo synthetic data                                 │
│  ├── init two rustic mirrors, distinct passwords                           │
│  ├── run LCSAS burn pipeline with TEST_TINY media → ~10 ISOs               │
│  ├── build meta disc via `lcsas meta build` (NO overlays)                  │
│  ├── stash ISOs in /var/lib/disc-vault/ (root-only, 0700)                  │
│  ├── compute expected_alpha_volumes.txt for verify.sh                      │
│  └── populate ~lcsas-blind with ONLY: tenant-alpha.pw, disc-labels.txt,    │
│                                       restored/ dir                        │
│                                                                            │
│  run.sh    spawns headless `claude -p` as user lcsas-blind, captures       │
│            stream-json to transcript.jsonl, allowed-tools = Bash           │
│  verify.sh runs the seven success checks above                             │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────── Blind sub-agent (lcsas-blind user) ────────────┐
│  Sees ONLY:                                                                │
│    • ~/tenant-alpha.pw                                                     │
│    • ~/disc-labels.txt   (one label per line, LCSAS_META first)            │
│    • disc-loader         (in PATH, setuid via /usr/local/bin/disc-loader)  │
│    • tmux                (in PATH; required to drive interactive prompts)  │
│    • /dev/sr0            (apparent real optical drive)                     │
│    • sudo NOPASSWD for: mount -o ro /dev/sr0 *, umount *, disc-loader      │
│                                                                            │
│  Must drive the interactive restore.sh through tmux:                       │
│    • tmux new-session ... 'sh restore.sh ~/restored/ latest'               │
│    • tmux capture-pane to read prompts                                     │
│    • tmux send-keys to type password and respond to INSERT DISC prompts    │
│                                                                            │
│  Sandbox hardening:                                                        │
│    • kernel.dmesg_restrict=1                                               │
│    • /opt/disc-robot/libexec/cdr-robotctl is root:root 0700                │
│    • /usr/local/bin/disc-loader is a setuid thin wrapper, source mentions  │
│      only "optical robot control" — no "cdemu" / "vhba" / "iso" strings    │
└────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                           /dev/sr0 (cdemu-backed)
```

---

## Fixture Design

### Media type — `TEST_TINY`

In production source as `MediaType.TEST_TINY = (1_048_576, 0)` with
`label_name == "TEST_TINY"`. Disc labels are generated by
`generate_volume_label("LCSAS", "TEST_TINY", N)` and look like:

    LCSAS_TEST_TINY_2026_0001
    LCSAS_TEST_TINY_2026_0002
    ...

1 MB is the canonical test volume. We deliberately shrink the source data
so ~10 discs can be filled from a few MB of source data, keeping the
fixture fast and the swap loop count high (the whole point of the test).

### Repository data layout

Generate **two** repositories of synthetic data under
`/mnt/lcsas-data/blind-test/sources/`:

| Repository | Data size | Dataset              | Password file                       |
|------------|-----------|----------------------|-------------------------------------|
| alpha      | ~600 MB   | photos/, docs/, db/  | `secrets/alpha.pw`                  |
| bravo      | ~300 MB   | mail/, archive/      | `secrets/bravo.pw` (never exposed)  |

Data must be **incompressible and non-dedupable** (generate from
`/dev/urandom` with per-file salts) so bin-packing produces a realistic
pack-to-disc distribution. Tune the source size until bin-packing yields
≥ 8 discs (ideally 10–12).

`alpha_manifest.sha256` and `bravo_manifest.sha256` are written outside
the agent's home and are the ground truth for verification.

### Burn pipeline

```
lcsas init --db-path /mnt/lcsas-data/blind-test/catalog.db
lcsas repo add alpha --path .../mirror/alpha --password-file .../alpha.pw
lcsas repo add bravo --path .../mirror/bravo --password-file .../bravo.pw
lcsas scan
lcsas stage --media TEST_TINY --session blind-test
lcsas burn  --session blind-test
```

Output: `LCSAS_TEST_TINY_2026_0001.iso` … `LCSAS_TEST_TINY_2026_000N.iso` placed in the
session staging directory. Each ISO carries the full catalog + per-repo
metadata under `metadata/<repo_id>/` (holographic property).

> **No fabricated flags.** Earlier drafts of this plan referenced
> `lcsas burn --output-dir` and `--format=disc-list` — neither exists.
> `setup.py` reads pack assignments straight from the catalog
> (`packs JOIN volume_packs JOIN volumes`) when computing
> `expected_alpha_volumes.txt`.

### Meta disc — production output, unmodified

```
lcsas meta build -o /mnt/lcsas-data/blind-test/meta_stage/
xorriso -as mkisofs -V LCSAS_META \
    -o /mnt/lcsas-data/blind-test/iso_out/LCSAS_META.iso \
    /mnt/lcsas-data/blind-test/meta_stage/
```

That is the entire step. The meta disc contains:

- `restore.sh` (production) — the bash wrapper
- `tools/restore_single_drive.py` (production) — stdlib helper
- `tools/bin/{rustic,xorriso,python3}` — bundled binaries
- `README_RESTORE.md` / `README_RESTORE.txt` — production docs
- `START_HERE.txt` — production landing page
- `lcsas/src/lcsas/` — full source

If `README_RESTORE.md` is unclear, that's a **product** bug — file it
against `src/lcsas/meta/builder.py:README_RESTORE` and update both the
production text **and** the test, in that order. Do not patch the README
on the test fixture.

The single-drive flow that the agent will actually drive is built into
the production wrapper:

```
./restore.sh --key ~/tenant-alpha.pw --target ~/restored/ --repo alpha
```

…and on each disc swap the script prints exactly:

    PLEASE INSERT DISC: <label>

This string is asserted by both the unit test (`test_meta_builder.py
::TestSingleDriveBitsStandalone`) and `verify.sh`. If the production
script ever changes that string, both update together.

### ISO vault & label map

All ISOs live in `/var/lib/disc-vault/` (root-only, 0700). A `manifest.json`
maps labels to ISO paths. The vault is **not** in the agent's home or
PATH, and `/var/lib/disc-vault` is in the forbidden-token scanner.

```json
{
  "LCSAS_META":         "/var/lib/disc-vault/LCSAS_META.iso",
  "LCSAS_TEST_TINY_2026_0001": "/var/lib/disc-vault/LCSAS_TEST_TINY_2026_0001.iso",
  "...":                "..."
}
```

### Agent home

Create unix user `lcsas-blind` with home `/home/lcsas-blind`. Populate:

```
~/tenant-alpha.pw   # alpha password, mode 600, owned by lcsas-blind
~/disc-labels.txt   # one label per line, LCSAS_META first
~/restored/         # empty, target of the restore
```

Sudoers entry (`/etc/sudoers.d/lcsas-blind`, validated with `visudo -c`):

```
lcsas-blind ALL=(root) NOPASSWD: /usr/bin/mount -o ro /dev/sr0 *, \
                                  /usr/bin/umount *, \
                                  /usr/local/bin/disc-loader
```

System-wide hardening:

```
sysctl -w kernel.dmesg_restrict=1   # persist in /etc/sysctl.d/
```

**Not** in the agent's home: the LCSAS source, the vault, the bravo
password, the rustic binary, the catalog. All of those come in via the
meta disc.

---

## The disc-loader robot

**Two files**, deliberately split to defeat the obvious leak vectors:

- `/opt/disc-robot/libexec/cdr-robotctl` — the actual cdemu wrapper
  (root:root, 0700). Calls `scripts/cdemu_drive.sh` under the hood.
  **Never** readable by the agent.
- `/usr/local/bin/disc-loader` — a thin C wrapper (or setuid bash) that
  execs `cdr-robotctl`. Setuid root, mode 4755. Its readable source
  refers only to "optical robot control" — never "cdemu", "vhba", or
  "iso". Agent can `cat` it freely; nothing leaks.

Subcommands the agent calls:

    disc-loader list                  list all known discs
    disc-loader status                show what's currently in the drive
    disc-loader insert <label>        eject current, load labelled disc
    disc-loader eject                 eject current disc

The wrapper logs every operation to `$RUN_DIR/disc-loader.log` (mode
0640, root:root — agent cannot read it). `run.sh` interleaves that log
into the transcript by timestamp.

The wrapper sleeps ~1 s per operation to feel like real hardware.

---

## The agent's initial prompt

`tests/e2e/cdemu_blind_restore/agent_prompt.txt` is **everything** the
sub-agent starts with. ~120 words:

```
You are restoring data after a disaster. Your job:

  Restore every file that the "alpha" repository had backed up
  into ~/restored/ and then say: RESTORE COMPLETE

You have:
  - One physical optical drive at /dev/sr0.
  - A disc-loader robot in PATH:
      disc-loader list            (show all discs you have)
      disc-loader status          (what's in the drive)
      disc-loader insert <label>  (swap in a labelled disc)
      disc-loader eject
  - ~/disc-labels.txt — labels of every disc you have.
  - ~/tenant-alpha.pw — the encryption password for repository alpha.

You have been told one thing: insert the disc labelled LCSAS_META
first, then follow the instructions you find on it.

Begin.
```

No mention of LCSAS, rustic, cdemu, pack files, snapshots, or any binary
name other than `disc-loader`.

---

## Transcript capture (`run.sh`)

Use **headless Claude Code** as a subprocess so every tool call lands on
disk for review.

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
RUN_DIR=${RUN_DIR:-/tmp/lcsas-blind-run-$(date +%s)}
mkdir -p "$RUN_DIR"
cp "$HERE/agent_prompt.txt" "$RUN_DIR/prompt.txt"

# Start a fresh disc-loader log under the run dir, owned by root.
sudo install -m 0640 -o root -g root /dev/null "$RUN_DIR/disc-loader.log"
export DISC_LOG="$RUN_DIR/disc-loader.log"

sudo -u lcsas-blind -H bash -c '
  cd ~
  claude \
    -p "$(cat '"$RUN_DIR"'/prompt.txt)" \
    --output-format stream-json \
    --allowed-tools "Bash" \
    --disallowed-tools "Read,Edit,Write,Glob,Grep,WebFetch,WebSearch" \
    --max-turns 80 \
    > '"$RUN_DIR"'/transcript.jsonl \
    2> '"$RUN_DIR"'/agent-stderr.log
'

python3 "$HERE/merge_timeline.py" \
    "$RUN_DIR/transcript.jsonl" \
    "$RUN_DIR/disc-loader.log" \
    > "$RUN_DIR/timeline.txt"

echo "run artifacts: $RUN_DIR"
```

Key flags:

- `--allowed-tools "Bash"` — agent can only run shell. Forces it into the
  same posture as a human on a bare terminal.
- `--output-format stream-json` — one JSON event per line; every tool
  call with args and result.
- `--max-turns 80` — generous but bounded. A correct run takes 20–40.

---

## Verification (`verify.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail
RUN_DIR=${1:?usage: verify.sh <run_dir>}
AGENT_HOME=/home/lcsas-blind
FIXTURE=/mnt/lcsas-data/blind-test

PASS=0; FAIL=0
check() {
    if eval "$2"; then
        echo "PASS  $1"; PASS=$((PASS+1))
    else
        echo "FAIL  $1"; FAIL=$((FAIL+1))
    fi
}

# 1. Every alpha file restored byte-for-byte.
check "alpha manifest matches" \
  "(cd $AGENT_HOME/restored && sha256sum -c $FIXTURE/alpha_manifest.sha256 --quiet)"

# 2. No bravo file present anywhere in restored/. Hash-set intersection.
(cd $AGENT_HOME/restored && find . -type f -print0 | xargs -0 sha256sum) \
    | awk '{print $1}' | sort -u > /tmp/restored.sha
awk '{print $1}' $FIXTURE/bravo_manifest.sha256 | sort -u > /tmp/bravo.sha
check "no bravo leak" \
  "[[ -z \"\$(comm -12 /tmp/restored.sha /tmp/bravo.sha)\" ]]"

# 3. Agent never used /dev/sr0 except via mount/umount.
check "no direct /dev/sr0" \
  "! grep -E '\"command\".*/dev/sr0' $RUN_DIR/transcript.jsonl | \
     grep -vE 'mount|umount|blkid'"

# 4. No forbidden illusion-leak tokens anywhere in the transcript.
FORBIDDEN='cdemu|vhba|\.iso(\\b|$)|dmesg|lsmod|modinfo|udevadm|/var/lib/disc-vault|libexec|loopback'
check "illusion intact" \
  "! grep -Eiq '$FORBIDDEN' $RUN_DIR/transcript.jsonl"

# 5. First insert was LCSAS_META.
FIRST=\$(grep -m1 -oE 'insert LCSAS_[A-Z0-9_]+' $RUN_DIR/disc-loader.log \
         | awk '{print \$2}')
check "first disc was LCSAS_META" "[[ \"$FIRST\" == LCSAS_META ]]"

# 6. Every required alpha disc was inserted at least once.
grep -oE 'insert LCSAS_CD_[0-9_]+' $RUN_DIR/disc-loader.log \
  | awk '{print \$2}' | sort -u > /tmp/inserted.txt
sort -u $FIXTURE/expected_alpha_volumes.txt > /tmp/expected.txt
check "all required discs inserted" \
  "[[ -z \"\$(comm -23 /tmp/expected.txt /tmp/inserted.txt)\" ]]"

# Upper bound: don't allow > 2× the needed insertions (catch thrashing).
NEEDED=\$(wc -l < /tmp/expected.txt)
ACTUAL=\$(wc -l < <(grep -c 'insert LCSAS_CD_' $RUN_DIR/disc-loader.log))
check "no excessive thrashing" "[[ \$ACTUAL -le \$((NEEDED * 2)) ]]"

# 7. Agent finished with the magic phrase the production script prints.
check "RESTORE COMPLETE printed" \
  "grep -q 'RESTORE COMPLETE' $RUN_DIR/transcript.jsonl"

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
```

---

## Full file layout

```
tests/e2e/cdemu_blind_restore/
├── PLAN.md                # this file
├── setup.py               # builds the whole fixture
├── disc-loader.c          # setuid wrapper source (compiled in setup.py)
├── cdr-robotctl           # internal cdemu wrapper (installed root:root 0700)
├── agent_prompt.txt       # the ~120-word initial prompt
├── run.sh                 # spawns headless claude, captures transcript
├── merge_timeline.py      # interleaves transcript + disc-loader log
├── verify.sh              # the seven success checks
└── teardown.sh            # unmount, clear vault, drop agent home, stop cdemu
```

### `setup.py` responsibilities

1. Pre-flight: check `/mnt/lcsas-data` exists; `cdemu_drive.sh`,
   `rustic`, `xorriso`, `dvdisaster`, `claude` on PATH.
2. Generate `sources/{alpha,bravo}/` from `/dev/urandom` with per-file
   salts. Write `{alpha,bravo}_manifest.sha256` outside the agent home.
3. Init two rustic repos under `mirror/`, write password files to
   `secrets/` (mode 600), run `rustic backup` for each.
4. `lcsas init` + `lcsas repo add` (×2) + `lcsas scan` +
   `lcsas stage --media TEST_TINY --session blind-test` +
   `lcsas burn --session blind-test`.
5. `lcsas meta build -o meta_stage/` then xorriso to `LCSAS_META.iso`.
   **No overlays.**
6. Move all ISOs to `/var/lib/disc-vault/` (root:root 0700) and write
   `manifest.json`.
7. Compute `expected_alpha_volumes.txt` by querying the catalog directly:
   every volume that holds a non-pruned pack belonging to repo alpha.
8. Compile `disc-loader.c`, install setuid root to
   `/usr/local/bin/disc-loader`. Install `cdr-robotctl` to
   `/opt/disc-robot/libexec/` (root:root 0700).
9. Ensure `cdemu-drive start` and the drive is empty.
10. `useradd -m lcsas-blind` (idempotent). Populate
    `~lcsas-blind/{tenant-alpha.pw, disc-labels.txt, restored/}`.
    `chown lcsas-blind:lcsas-blind` everything. Nothing else.
11. Drop `/etc/sudoers.d/lcsas-blind` and run `visudo -c`.
12. `sysctl kernel.dmesg_restrict=1` and persist in `/etc/sysctl.d/`.
13. Print one-line summary: N discs, total bytes, alpha/bravo file counts.

### `teardown.sh`

- `disc-loader eject`
- `cdemu-drive stop`
- `rm -rf /var/lib/disc-vault /opt/disc-robot ~lcsas-blind /mnt/lcsas-data/blind-test`
- `userdel lcsas-blind`
- Remove `/etc/sudoers.d/lcsas-blind`, `/etc/sysctl.d/99-blind-restore.conf`
- Unlink `/usr/local/bin/disc-loader`

---

## How to run

```bash
make blind-restore       # convenience target — runs setup → run → verify
```

`make blind-restore` is a thin wrapper:

```makefile
.PHONY: blind-restore
blind-restore:
	sudo ./tests/e2e/cdemu_blind_restore/setup.py
	./tests/e2e/cdemu_blind_restore/run.sh
	./tests/e2e/cdemu_blind_restore/verify.sh /tmp/lcsas-blind-run-*
	./tests/e2e/cdemu_blind_restore/teardown.sh
```

The blind restore target is **deliberately out-of-band** — it is not in
`make test-unit` or `make test-integration`. Reasons: it needs cdemu, a
headless `claude` with auth, a pre-allocated LV at `/mnt/lcsas-data`,
and ~1 GB of scratch.

Expected runtime: ~5 min for setup (urandom + burn + ECC), 5–15 min for
the blind agent (think time + simulated swap sleeps), <5 s for verify.

---

## Why each design choice was made

| Choice | Reason |
|---|---|
| Production meta disc, no overlays | The whole point is to test the production single-drive flow. Overlays prove only that overlays work. |
| Virtual drive via cdemu, not loopback mounts | Exercises the real `/dev/sr0` + iso9660 code path, including mount-by-label, blkid, and the ioctl surface. |
| `disc-loader` setuid + `cdr-robotctl` libexec split | Defeats `cat $(which disc-loader)` as a leak vector. The agent can read the wrapper and learn nothing. |
| Bash-only tool allowlist | Forces the agent to use shell — same posture as a human on a bare terminal. Makes the no-illusion-leak check enforceable. |
| `lcsas-blind` user + narrow sudoers | Real DR posture. Stops the agent from poking at /var/lib/disc-vault, dmesg, lsmod, etc. |
| `kernel.dmesg_restrict=1` | Closes the easiest cdemu/vhba leak (`dmesg | grep`) without having to add `dmesg` to the forbidden-token scanner. |
| Two repos, one password | Isolation is only meaningful when foreign plaintext is physically present and still inaccessible. |
| 1 MB `TEST_TINY` instead of real 700 MB CDs | 10 real CDs would force ~7 GB of source data and a 10 min burn. 1 MB × 10 is ~1 min and keeps the swap loop count high. |
| Headless `claude -p` instead of in-session Agent | Durable per-event transcript for post-run audit. The Agent tool returns only the final message; for this test the process is the product. |
| Hash-set bravo-leak check | Earlier draft grepped bravo SHA strings against restored *filenames* — nonsense. Hashes and filenames don't share a namespace. |
| Pick-list superset, not "≥ 4 swaps" | A count is a weak lower bound. Comparing inserts to the pre-computed required-volume set proves no shortcutting and catches thrashing. |
| `--max-turns 80` | A correct restore should take 20–40. 80 leaves headroom without permitting infinite loops. |

---

## Pre-flight checklist (before first execution)

1. ✅ `MediaType.TEST_TINY` — defined in `src/lcsas/config/media.py`.
2. ✅ Single-drive helper `src/lcsas/meta/restore_single_drive.py` — landed,
   8 unit tests passing.
3. ✅ Production `restore.sh` defaults to single-drive mode — landed in
   `src/lcsas/meta/builder.py:RESTORE_SCRIPT`. Bash syntax + bundling
   asserted by unit tests.
4. ✅ `tools/restore_single_drive.py` bundled by `MetaVolumeBuilder` —
   landed; verified by `test_single_drive_helper_bundled`.
5. ✅ `README_RESTORE.md` leads with single-drive recipe — landed.
6. ☐ `setup.py`, `run.sh`, `verify.sh`, `teardown.sh`, `disc-loader.c`,
   `cdr-robotctl`, `merge_timeline.py`, `agent_prompt.txt` — to be
   written next (this is the test rig itself).
7. ☐ `make blind-restore` target — to be added to `Makefile`.
8. ✅ Manual single-drive smoke test on this VM via `cdemu_drive.sh` —
   `scripts/smoke_single_drive.py` builds 5 data discs + meta disc,
   drives `restore.sh` through a pty swap loop, verifies restored
   files match the source manifest. Passing as of this commit.

Items 1–5 are the production-code feature. Item 8 is the manual smoke
that validates the feature works at all. Items 6–7 are the test rig.
The blind-restore acceptance is the gate that says the feature ships.
