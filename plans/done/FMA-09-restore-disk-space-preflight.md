# FMA-09: Heir restore has no disk-space preflight; ENOSPC mid-restore is the failure mode

> **STATUS: RESOLVED** — landed in `647db88` (restore+recovery: free-space preflight before any restore prompt [FMA-09]); guarded by `tests/unit/test_cli_restore.py`.

**Priority:** P2 · **Severity:** medium · **Dimension:** failure-modes · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: partial — recovery/docs/AUDIT_FINDINGS.md "Remaining gaps" (disc_locator fs-full drain coverage); the user-facing preflight is untracked
**Suggested GH issue title:** Add free-space preflight to restore.sh, restore exec, and wizard

## Problem

The burn side checks staging free space before writing a byte
(`orchestrator.py:567-582`), but the restore side — the path the non-technical heir
actually walks — has no space check at all: `restore.sh`, the live restore wizard, and
`cmd_restore_exec` never compare required bytes against free space at the target or the
pack cache, even though the catalog knows the total (`PickList.total_bytes`).
`RECOVER.txt` just tells the reader to have "enough free space". An heir restoring a
multi-hundred-GB archive onto a smaller laptop fails mid-restore *after* a long
disc-swapping session, with whatever message the underlying write error produces — the
worst possible point to discover a sizing problem.

One verifier correction narrows the tier-1 sub-claim: the **target**-full ENOSPC
diagnostic in the C binary is already implemented and pinned by
`tests/recovery_hardening/test_tier1_target_full.py` (issue #221, environment-gated). The
genuinely uncovered C branch is the `disc_locator.c` **drain** path under fs-full, per
AUDIT_FINDINGS.md.

## Evidence

Re-verified 2026-06-10:

- `grep -rn "disk_usage\|statvfs" src/lcsas/restore/ src/lcsas/meta/
  recovery/scripts/restore.sh` → nothing (the `df` at `restore.sh:85-90` is mount-point
  detection only).
- `recovery/docs/RECOVER.txt:16` — "A target directory with enough free space" with no
  check.
- `src/lcsas/burn/orchestrator.py:567-582` — burn-side `shutil.disk_usage` preflight with
  the exact "X GB available, ~Y GB needed" message shape to mirror.
- `recovery/docs/AUDIT_FINDINGS.md` (Remaining gaps table) — `disc_locator.c` 81.6%:
  "Drain edge cases (fs-full, missing source)" uncovered.
- `tests/recovery_hardening/test_tier1_target_full.py` — tier-1 target ENOSPC already
  pinned (skips without rustic + built binary + passwordless sudo mount).

## Fix design

Per the verdict refinement: **no tier-1 target-path change** (already done); add the
user-facing preflights and close the drain-branch coverage gap.

1. **`cmd_restore_exec` (`cli/main.py:2211`)**: before the first disc prompt, compute
   `required = plan.total_bytes` (already computed from the catalog) and compare against
   `shutil.disk_usage(target).free` and, when the pack cache is on a different filesystem,
   `disk_usage(cache_dir).free` (cache needs ~`total_bytes` too — packs accumulate before
   rustic restore runs). On shortfall, hard-confirm:
   `"Restoring ~X GB but <target> has only Y GB free. Continue anyway? [y/N]"`
   (non-interactive/`--yes` ⇒ refuse with exit 1). Mirror the burn-side message wording.
2. **`recovery/scripts/restore.sh`**: after target + catalog selection and before the
   password/disc prompts, derive required bytes — from the selected snapshot's size when
   the catalog/tier provides it, else from summed pack sizes for the pick list; compare via
   `df -Pk` on `$TARGET` and `$LCSAS_PACK_CACHE_DIR`. POSIX-sh, no bashisms (matches the
   existing script style). Failure message:
   `"Need about X GB free at <target>; only Y GB available. Free space or choose a
   different --target, then re-run."` Allow override with `LCSAS_SKIP_SPACE_CHECK=1`
   (documented in the env-var help block at `restore.sh:305+`).
3. **Live restore wizard (`src/lcsas/meta/live/restore_wizard.py`)**: same check before
   starting; the wizard's RAM-tmpfs context makes this the difference between a clear
   refusal and a silent kill (BOOT plans own the tmpfs question itself).
4. **`recovery/docs/RECOVER.txt:16`**: replace "enough free space" with the concrete rule
   ("at least the size shown by `lcsas restore plan` / the snapshot size, on BOTH the
   target and the cache disk — the script checks this for you").
5. **C coverage**: add a `recovery/tests` unit driving the `disc_locator.c` drain path
   against a full-filesystem fixture (tiny tmpfs or `setrlimit(RLIMIT_FSIZE)` where
   tmpfs/sudo is unavailable), closing the AUDIT_FINDINGS gap; wire into
   `make -C recovery test` and the coverage-c numbers the audit gate reads.

No schema change; no catalog semantics touched.

## Tests & gates

- `tests/unit/test_cli_restore.py::test_restore_exec_refuses_insufficient_target_space` —
  monkeypatch `shutil.disk_usage` small; assert refusal before any executor call, message
  names required vs available; `--yes` path refuses, interactive `y` proceeds. Always-on.
- `tests/unit/test_cli_restore.py::test_restore_exec_checks_cache_filesystem` — cache on a
  separate (mocked) filesystem also checked. Always-on.
- `tests/recovery_hardening/test_restore_space_preflight.py` — run `restore.sh` with
  `--target` on a 1 MiB tmpfs (reuse the `_can_sudo_mount` gating pattern from
  `test_tier1_target_full.py`); assert exit before any disc/password prompt with the
  required-vs-available message; `LCSAS_SKIP_SPACE_CHECK=1` proceeds to the next prompt.
  Environment-gated like its sibling tests; runs in the recovery-hardening suite (whose CI
  wiring is a GATE plan).
- `recovery/tests` drain fs-full unit (item 5) — asserted diagnostic + non-zero exit;
  always-on in `make -C recovery test`.

## Acceptance criteria

- [ ] `lcsas restore exec` against a too-small target refuses up front with sizes in the
  message; no disc prompt is reached.
- [ ] `restore.sh` on a 1 MiB tmpfs target exits with the sizing message before asking for
  a password or disc.
- [ ] `disc_locator.c` drain fs-full branch covered (AUDIT_FINDINGS "Remaining gaps" row
  updated); coverage-c does not regress.
- [ ] RECOVER.txt prerequisite text updated; restore.sh `--help` documents
  `LCSAS_SKIP_SPACE_CHECK`.

## Dependencies & related plans

- **GATE — recovery-hardening suite never runs in CI**: the restore.sh test only bites
  once that suite is wired in; reference, not a blocker.
- **UX — restore overwrites non-empty target without warning**: same preflight location in
  restore.sh/exec — implement the two prompts together.
- **BOOT — 256 MB RAM tmpfs restore target**: wizard check here is a stopgap; the routing
  rewrite owns the real fix.
- Already covered (do not redo): tier-1 target ENOSPC diagnostic
  (`test_tier1_target_full.py`).

## Effort

2 days: 1 impl (Python + sh + C test fixture), 1 test. Needs passwordless-sudo tmpfs
mounts locally for the gated tests (available on this VM); C part needs
`make -C recovery` toolchain.

---
**Implemented:** 2026-06-12. As planned, with deviations: (1) item 3 (live
restore wizard) skipped — `src/lcsas/meta/live/restore_wizard.py` was deleted
by BOOT-07 (Alpine live-stack removal), nothing to patch; (2) `restore exec`
has no `--yes` flag, so the non-interactive refusal keys on stdin not being a
TTY; (3) restore.sh derives required bytes from the catalog via the sqlite3
CLI when present, else the tier-1 binary's `--list-pending-packs` total (no C
source change), and the catalog-discovery block moved ahead of the password
prompt to make the preflight fire before any secret is typed; (4) the C
drain fs-full unit covers the mid-drain write-failure branch always-on via
RLIMIT_FSIZE and the `<10% free` guard branch via the `LCSAS_TEST_FULL_FS_DIR`
seam driven by the gated tmpfs test; (5) coverage-c surfaced a latent
environment dependence — on hosts whose /tmp is <10% free, every drain test
silently took the guard branch, flipping ~80 disc_locator lines in/out of
coverage.  Fixed by moving the drain-cache fixtures to a roomy fs base
(`TMPDIR` → `/dev/shm` → `/tmp`, first with >=11% free) and reclassifying the
genuinely host-dependent lines (457/458, 567/568/574/576) as VOLATILE in
EXEMPTIONS.md.
