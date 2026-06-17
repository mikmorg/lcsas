# FUP-02: Catalog concurrency — staging-clean TOCTOU, silent lock waits, unlocked writers

> **STATUS: RESOLVED** — landed in `9e5b9ab` (db+cli+burn: catalog concurrency mitigations — staging-clean TOCTOU, loud lock waits, read-only safety [FUP-02]); guarded by `tests/integration/test_concurrency.py`.

**Priority:** P1 · **Severity:** high · **Dimension:** follow-up: catalog-concurrency · **Audit status:** flagged by completeness critic (citations re-verified against code 2026-06-10) · **Ledger:** untracked (named only in DEEP_AUDIT Appendix C / P2 roadmap)
**Suggested GH issue title:** Fix staging-clean TOCTOU and silent catalog-lock waits; audit two-process races

## Problem

Concurrency was designed half-way: some CLI commands serialize through an `fcntl.flock`
on `<db>.lock` (`locked_connection`), others open the catalog bare (`get_connection`),
and nothing was ever audited for what happens when two `lcsas` processes overlap. Three
concrete hazards exist today.

First, `lcsas staging clean` is a **TOCTOU that can delete an in-flight stage's tree**:
it detects "orphaned" staging directories on an *unlocked* connection, then sits at an
interactive `input()` confirm prompt for arbitrarily long, then deletes the directories
with **no re-check and no lock**. The stage orchestrator also creates the session
directory on disk *before* committing the `burn_sessions` row, so a concurrent
staging-clean can flag a brand-new in-flight session as orphaned and later rm -rf it
while `lcsas stage` is hardlinking packs or mastering the ISO into it. Because packs are
permanently "claimed" at staging-commit (the confirmed BURN-03/FMA-01 critical), the
blast radius is not just a failed stage — it can strand packs the catalog forever
reports as archived.

Second, the flock is **blocking, timeout-free, and silent**: `lcsas burn session` holds
it for the entire multi-hour burn, so any concurrent `lcsas scan`/`stage`/`verify` hangs
indefinitely with zero output. The realistic operator response to a hung command is
Ctrl-C — and when that doesn't "work", killing processes, possibly the one that is
mid-burn. A concurrency primitive that invites killing a burn is itself a data-loss
surface. Third, the unlocked commands are not actually read-only: every one of them runs
`create_all()` (a committing write transaction) outside the flock, so the lock does not
even serialize all writers.

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/db/connection.py:78-81` — `locked_connection`:
  `lock_fd = open(lock_path, "a", ...)` then `fcntl.flock(lock_fd, fcntl.LOCK_EX)` —
  blocking, no timeout, no message before or while waiting. Nothing identifies the
  holder.
- `src/lcsas/cli/main.py:1087` — `cmd_burn_session`: `with locked_connection(args.db or
  config.db_path) as conn:` wraps the entire command body including every physical burn;
  `cmd_stage` (main.py:1016) likewise holds it across staging + ISO mastering + ECC.
- `src/lcsas/cli/main.py:968-989` — `cmd_staging_clean`: `conn =
  get_connection(...)` (968, **no flock**), `create_all(conn)` (970),
  `detect_orphaned_staging(config, conn)` (971), connection closed (973), interactive
  `confirm = input("Remove these directories? [y/N] ")` (984), then
  `clean_orphaned_staging(orphans)` (989) deletes the previously computed list — no lock
  taken at any point, no re-detection after the prompt.
- `src/lcsas/staging/cleanup.py:17-47` — `detect_orphaned_staging` flags any
  session-named dir under `staging_path` with no non-CLEANED `burn_sessions` row;
  `clean_orphaned_staging` (:50-56) `safe_remove_tree`s whatever list it is handed.
- `src/lcsas/burn/orchestrator.py:586-594` — `stage()` calls `ensure_dir(session_dir)`
  (:587) *before* `create_session(...)` commits the row (:589, commit=True default in
  `db/sessions.py:30-47`) — the dir-exists-but-no-row window in which detect flags an
  in-flight session.
- Lock-usage inventory (`grep -n "locked_connection\|get_connection" src/lcsas/cli/main.py`):
  **unlocked** — `cmd_init` (:605), `cmd_repo_list` (:651), `cmd_status` (:917),
  `cmd_staging_clean` (:968), `cmd_restore_plan` (:2087), `cmd_restore_exec` (:2229),
  `cmd_session_list` (:3360); **locked** — `cmd_repo_add` (:623), `cmd_repo_remove`
  (:677), `cmd_scan` (:779), `cmd_stage` (:1016), `cmd_burn_session` (:1087),
  `cmd_burn_iso` (:1220), `cmd_location` (:1310), `cmd_consolidate` (:1528),
  `cmd_verify` (:1648). (`cmd_restore_from_disc` at :2550/:2735 opens a *temp copy* of a
  disc catalog — out of scope.) All unlocked commands except `cmd_session_list` call
  `create_all()` (`db/schema.py:171-198` — ends in `conn.commit()`), i.e. they are
  writers outside the lock, relying solely on WAL + `busy_timeout=30000`
  (connection.py:39-42).

## Fix design

### Part 1 — immediate mitigations (implement now)

**1. Lock with the whole command, re-check under the lock — `cmd_staging_clean`**
(`src/lcsas/cli/main.py`). Switch to `locked_connection` and re-run detection *after*
the confirm prompt, deleting only the intersection:

```python
with locked_connection(db_path) as conn:
    create_all(conn)
    orphans = detect_orphaned_staging(config, conn)
    ...print list...
    if not args.force:
        confirm = input("Remove these directories? [y/N] ")
        ...
        # Re-check: the prompt may have sat for minutes.
        still_orphaned = set(detect_orphaned_staging(config, conn))
        orphans = [p for p in orphans if p in still_orphaned]
    removed = clean_orphaned_staging(orphans)
```

Holding the flock across the prompt is acceptable here (staging-clean is a maintenance
command; blocking a concurrent stage *is the point*), but the re-check stays anyway —
it also covers `--force` runs racing each other. Note for reviewers: with mitigation 2
in place, a concurrent `lcsas stage` waiting on this prompt prints a "waiting for lock"
message naming staging-clean, instead of silently hanging.

**2. Row before dir** (`src/lcsas/burn/orchestrator.py:584-594`). Swap the order:
commit the `burn_sessions` row first, then `ensure_dir(session_dir)`. Invariant after
the swap: any session-named directory on disk either has a non-CLEANED row (never
flagged orphan) or belongs to a process that crashed between commit and mkdir (row
without dir — detect already ignores it; `clean_session` tolerates a missing dir). The
in-flight-dir-flagged-orphan window closes completely. No schema change.

**3. Loud, identified lock waits** (`src/lcsas/db/connection.py`). Try
`LOCK_EX | LOCK_NB` first; on `BlockingIOError`, read holder info from the lock file,
print to stderr, then block:

```python
@contextmanager
def locked_connection(db_path, *, exclusive=True, timeout: float | None = None):
    # timeout: None = wait forever (interactive default);
    # LCSAS_LOCK_TIMEOUT env or --lock-timeout flag for scripts.
```

On acquiring, write holder JSON into the lock file (`{"pid": ..., "cmd": "lcsas burn
session", "since": "<iso8601>"}` — truncate+write while holding; older code never reads
lock-file content, so this is compatible). Waiting message wording:

```
Waiting for the catalog lock: held by 'lcsas burn session' (pid 41023, since 14:02).
Ctrl-C safely cancels THIS command. Do NOT kill the other process — it may be burning.
```

On `timeout` expiry: raise a `CatalogLockTimeout` that the CLI maps to exit code 75
(EX_TEMPFAIL) with the same holder identification. Crash-safety note: flock releases
automatically when the holding process dies; the `.lock` file itself is inert and must
never be deleted manually — add one line saying so to `docs/TROUBLESHOOTING.md`.

**4. Stop writing outside the lock.** Drop `create_all()` from the read-only commands
(`cmd_status`, `cmd_repo_list`, `cmd_restore_plan`, `cmd_restore_exec`) — they should
*fail* on an uninitialized catalog ("No catalog at <path>. Run `lcsas init` first."),
not create one as a side effect. `cmd_init` keeps `create_all` but moves to
`locked_connection`. This makes "unlocked ⇒ genuinely read-only" true, so WAL snapshot
isolation is sufficient for everything outside the flock.

**Catalog/schema note:** no schema-v5 change; no catalog-semantics change. The only new
artifact is JSON content inside `<db>.lock`, which nothing on burned discs or in
restore-side code ever reads (restore paths copy `catalog.db` to a temp dir and never
touch `.lock`).

### Part 2 — follow-up audit charter (run after mitigations land)

Scope: two-process catalog and filesystem races, against the bar "no interleaving of
two lcsas commands may strand packs, corrupt the catalog, or invite killing a burn."

- (a) **Reproduce, then regress**: a harness that runs `lcsas stage` and
  `lcsas staging clean --force` with injected barriers (monkeypatched `ensure_dir` /
  `detect_orphaned_staging`) to demonstrate the pre-fix deletion and prove the fix; same
  for two concurrent `stage` runs (label-sequence collision: `next_seq_num` at
  orchestrator.py:600-601 is read-then-use under the flock — verify it stays safe).
- (b) **WAL sufficiency for readers**: what does a reader see mid-stage / mid-burn
  (snapshot taken at first read)? Confirm no command makes decisions from a stale
  snapshot that drive filesystem mutations (staging-clean was the known case; sweep
  consolidate and verify).
- (c) **Lock granularity**: should `burn_session`/`stage` hold the flock only around DB
  transactions instead of across hours of xorriso/dvdisaster work? Enumerate what the
  coarse lock currently (accidentally) protects — staging-dir lifetime, session "latest"
  resolution, label sequencing — before narrowing it. Decide and document; do not
  narrow casually.
- (d) **NFS / multi-host**: the tier model puts the hot mirror on a NAS and the catalog
  may live there too. `flock(2)` over NFSv3 with `nolock` is local-only; NFSv4 emulates
  it via byte-range locks. Decide: document a single-host requirement, or detect
  network filesystems and warn at startup.
- (e) **Signals**: Ctrl-C while *waiting* vs while *holding*; verify `ShutdownManager`
  (used by `cmd_stage`) leaves a consistent catalog + lock state at every interruption
  point.

Deliverable: classification table for all subcommands + new findings filed as plans;
expected 1.5-2 focused days.

## Tests & gates

Unit tests are always-on via `make test-unit` (.github/workflows/test.yml). Existing
files: `tests/unit/test_db_connection.py` (already exercises `locked_connection`),
`tests/unit/test_staging.py`, `tests/unit/test_burn_orchestrator.py`.

- `tests/unit/test_db_connection.py::test_locked_connection_writes_holder_info` —
  acquire, assert lock file contains pid/cmd/since JSON.
- `::test_locked_connection_prints_waiting_and_blocks` — hold the flock from a child
  process; assert stderr names the holder command and pid; release; assert acquisition
  completes.
- `::test_locked_connection_timeout_raises` — holder child + `timeout=0.2`; assert
  `CatalogLockTimeout` naming the holder; CLI-level test asserts exit code 75.
- `tests/unit/test_staging.py::test_staging_clean_rechecks_after_prompt` — monkeypatch
  `input` to register a new `burn_sessions` row pointing at one flagged dir before
  answering "y"; assert that dir survives and the others are removed.
- `tests/unit/test_burn_orchestrator.py::test_session_row_committed_before_dir` —
  monkeypatch `ensure_dir` to assert the `burn_sessions` row for `session_id` is already
  committed (query via a second connection) when the dir is created.
- `tests/unit/test_cli.py::test_readonly_commands_fail_without_catalog` — `lcsas status`
  against a nonexistent db path exits non-zero with the "Run `lcsas init`" message and
  creates no file.
- Integration (always-on, no external binaries): `tests/integration/test_concurrency.py`
  — real two-process race: process A holds `locked_connection`; process B runs
  `lcsas status` (must succeed, read-only) and `lcsas scan` (must print the waiting
  message); plus the staging-clean-vs-stage barrier reproduction from charter (a).

## Acceptance criteria

- [ ] `lcsas staging clean` acquires the catalog flock and re-runs orphan detection
      after the confirm prompt; the barrier reproduction test deletes nothing in-flight.
- [ ] In `stage()`, the `burn_sessions` row is committed before the session directory
      exists on disk (asserted by unit test).
- [ ] A second `lcsas` command run during a held lock prints, within 1s, a message
      naming the holding command and pid; `--lock-timeout`/`LCSAS_LOCK_TIMEOUT` exits 75
      instead of waiting forever.
- [ ] `lcsas status`/`repo list`/`restore plan`/`restore exec` perform zero catalog
      writes (no `create_all`); `grep -n create_all src/lcsas/cli/main.py` shows it only
      under `locked_connection`.
- [ ] All new unit + integration tests pass in `make test-unit` / `make test-integration`;
      `make gate` green.
- [ ] Charter questions (a)-(e) answered in writing; subcommand classification table
      committed; new findings filed as plans.

## Dependencies & related plans

- BURN "reclaim packs from failed staging" (BURN-03) and FMA "staged-never-burned
  counted archived" (FMA-01) — the stranding this TOCTOU triggers; the staging-clean fix
  here removes one *cause*, those plans fix the *bookkeeping*. Land this in the same
  milestone but no code dependency.
- FMA "schema migrations never run" (FMA-02) — mitigation 4 changes where `create_all`
  runs; coordinate so the migration-execution fix lands in the same locked entry points.
- FUP-01 burn-operator-protocol — operator checkpoints make burns (and lock holds) even
  longer; the waiting-message mitigation here is its prerequisite for sane UX.

## Effort

Mitigations: 1.5 days impl + 1 day tests (two-process tests need care on CI but no
special hardware). Charter audit: 1.5-2 days. Total ≈ 4-4.5 days. No Windows/qemu needs;
NFS charter question needs a NAS mount (available on the home network, not this VM).

---
**Implemented:** 2026-06-13. Part 1 only (part 2 charter remains a document). As
planned, adapted to post-FMA-02 reality: read-only commands previously called
`ensure_schema` (not bare `create_all`); mitigation 4 now routes them through a new
`_open_existing_catalog()` helper that fails (CatalogError → "Run `lcsas init`") on an
uninitialized catalog and never writes, while `cmd_init` migrated to `locked_connection`.
Added `CatalogLockTimeout` (→ exit 75), loud holder-stamped lock waits with
`--lock-timeout`/`LCSAS_LOCK_TIMEOUT`, staging-clean re-check under the held lock,
row-before-dir in `stage()`, and a TROUBLESHOOTING.md lock note. Two pre-existing tests
that encoded the old auto-create/`status`-migrates behavior were updated to assert the new
read-only semantics.
