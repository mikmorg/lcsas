# BOOT-05: restore.sh must warn when the restore target is RAM-backed (tmpfs)

> **STATUS: RESOLVED** — landed in `1a22c2c` (recovery: warn-and-confirm on RAM-backed (tmpfs) restore targets [BOOT-05]); guarded by `tests/recovery_hardening/test_boot_path_quarantined.py`.

**Priority:** P1 · **Severity:** high · **Dimension:** boot-live-distro · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Warn loudly when restore target resolves to tmpfs

## Problem

The designed boot flow restores into RAM: `lcsas-init` mounts `/tmp` as a tmpfs
capped at **256 MB**, then execs restore.sh with target `/tmp/restored`. Even in
the fully-built version of the boot path, the flagship "run lcsas-restore
directly" flow would (a) hit ENOSPC for any archive over 256 MB — in a system
designed for hundreds of discs — and (b) write every restored file to memory: an
heir who sees "restore complete" and powers off has lost everything restored,
with no warning anywhere in init.c or the docs. The boot path is being dropped
(BOOT-01), which removes the init.c entry point — but the underlying hazard
survives the drop, because `restore.sh` itself defaults `TARGET` to
`/tmp/restored`, and on many mainstream distros (Fedora, Arch, openSUSE — and
most live-USB environments, which BOOT-01 now *routes heirs into*) `/tmp` is a
RAM-backed tmpfs. A host-OS or live-USB user who accepts the documented default
gets exactly the silent-loss failure: restore succeeds, reboot, gone.

The audit verdict says this plainly: "If the boot path is dropped, the restore.sh
tmpfs warning is still worth adding." That warning is this plan. Severity stays
high because the live-USB route (the new supported no-OS path) makes a tmpfs
`/tmp` the *common* case, not an edge case, and the failure mode is silent data
loss of the restored copy plus heir despair — recoverable only by someone who
understands what tmpfs is.

## Evidence

(Re-checked 2026-06-10.)

- `recovery/src/lcsas-init/init.c:111` —
  `try_mount("tmpfs", "/tmp", "tmpfs", 0, "mode=1777,size=256m");`
- `recovery/src/lcsas-init/init.c:136-143` — execs restore.sh with args
  `"/mnt/recovery", "/tmp/restored", "latest"`.
- `recovery/scripts/restore.sh:351` — `TARGET="${TARGET:-/tmp/restored}"`; the
  same default is advertised in the usage text at `:282` and `:358`.
- `recovery/scripts/restore.sh:740` — `mkdir -p "$TARGET_DIR"` with no
  filesystem-type check; grep for tmpfs/RAM warnings finds only the *meta-disc
  relocate-to-RAM* logic (`:224-225` uses `findmnt -n -o FSTYPE` for the script's
  own location — a ready-made detection pattern, never applied to the target).
- Target-disk selection exists only in the Alpine wizard
  (`src/lcsas/meta/live/restore_wizard.py:546-595`), which BOOT-07 deletes.

## Fix design

**A. Detection + warning in `recovery/scripts/restore.sh`**, immediately after
`mkdir -p "$TARGET_DIR"` (line 740). Reuse the script's existing pattern:

```sh
target_fstype() {
    # FSTYPE of the filesystem holding "$1"; empty if undetectable.
    if command -v findmnt >/dev/null 2>&1; then
        findmnt -n -o FSTYPE --target "$1" 2>/dev/null
    elif [ -r /proc/mounts ]; then
        # longest-prefix match of $1 against mount points
        awk -v p="$(cd "$1" && pwd -P)" '
            p == $2 || index(p, $2 "/") == 1 { if (length($2) > l) { l = length($2); t = $3 } }
            END { print t }' /proc/mounts
    fi
}
```

If the result is `tmpfs` or `ramfs`:

```
==========================================================
  WARNING: the restore target
      <TARGET_DIR>
  is on a RAM-backed filesystem (tmpfs).  Restored files
  WILL BE LOST when this computer powers off or reboots.
  Choose a real disk instead, e.g.:
      sh restore.sh /media/<your-usb-drive>/restored latest
==========================================================
```

Behavior: when a TTY is available (`[ -t 0 ] || [ -r /dev/tty ]`), prompt
"Continue restoring into RAM anyway? [y/N]" via /dev/tty and abort on anything
but y/Y. When non-interactive, print the warning and **continue** unless
`LCSAS_FORBID_TMPFS_TARGET=1` (new env, documented in the usage heredoc).
Rationale for not hard-failing non-interactive runs: the documented default is
`/tmp/restored` and existing automation (blind-restore drills, test harnesses)
restores to tmp paths legitimately; a hard fail would break the drills on
tmpfs-/tmp hosts, while the interactive prompt protects the human heir — the
party who cannot diagnose the loss. `LCSAS_ALLOW_TMPFS_TARGET=1` suppresses the
prompt for scripted-but-interactive use.

**B. Doc line.** Add one sentence to the rewritten `recovery/docs/BOOT.txt` /
`docs/workflows/restore-live-usb.md` (BOOT-01): in a live-USB session, restore to
a plugged-in external drive, not the live system's home folder.

**C. Out of scope.** `restore.bat`: Windows has no tmpfs — no change. The
lcsas-init refuse-handoff policy from the audit applies only to the KEEP variant;
recorded in `experimental/boot/README.md` (BOOT-06 carries the init-side notes).

No catalog/schema impact.

## Tests & gates

`tests/recovery_hardening/test_restore_sh_tmpfs_target_warns.py` (new, always-on;
PATH-stub style of the existing `test_restore_sh_*` suite — no mount privileges
needed):

- `test_tmpfs_target_warns_and_prompts` — stub `findmnt` on PATH to print
  `tmpfs`; run restore.sh with a tmp target and a pty/`/dev/tty`-less stdin plus
  scripted "n"; assert the RAM warning text appears and exit is non-zero before
  any tier runs.
- `test_tmpfs_target_noninteractive_warns_continues` — same stub, stdin from
  /dev/null: warning printed on stderr, script proceeds past the check (assert it
  reaches the next phase marker), exit not caused by the check.
- `test_forbid_env_blocks` — `LCSAS_FORBID_TMPFS_TARGET=1`, non-interactive:
  non-zero exit, warning text present.
- `test_disk_target_silent` — stub `findmnt` printing `ext4`: no warning text.
- `test_proc_mounts_fallback` — remove findmnt from stub PATH, point the awk
  fallback at a fixture `/proc/mounts` copy via a test seam (factor
  `target_fstype` to honor `LCSAS_PROC_MOUNTS` override for testability).

Runs in `make test-recovery-hardening`; CI wiring per GATE-02. The blind-restore
drills (haiku runner) must stay green — they are the canary that the
non-interactive behavior didn't break automation.

## Acceptance criteria

- [ ] On a tmpfs target with a TTY, restore.sh shows the RAM warning and aborts
      on default (N).
- [ ] Non-interactive tmpfs restore warns on stderr but completes;
      `LCSAS_FORBID_TMPFS_TARGET=1` makes it fail.
- [ ] Non-tmpfs targets produce no warning and no prompt.
- [ ] Usage text documents both env knobs.
- [ ] `pytest tests/recovery_hardening/test_restore_sh_tmpfs_target_warns.py -v`
      passes; `make blind-restore` (single-key) still scores clean.

## Dependencies & related plans

- **BOOT-01** — live-USB routing makes this the common path; doc line lands there.
- **BOOT-06** — carries the lcsas-init-side notes for the KEEP variant.
- **UX-07** (non-empty-target restore guard) — adjacent target-dir check in the
  same code region; coordinate so the two checks share the post-mkdir hook point
  and prompt style.

## Effort

1 day: 0.4 shell implementation (portable fstype probe + prompt), 0.6 tests
(PATH stubs, pty handling). No special environment.

---
**Implemented:** 2026-06-11. As planned; also added ENV_VARS.txt entries for both knobs and refreshed MANIFEST entries for the touched recovery files. `make blind-restore` not run (cost-gated via LCSAS_BLIND_ACK_COST; host /tmp is ext4 so the drills never hit the tmpfs path, and non-interactive behavior is warn-and-continue by design).
