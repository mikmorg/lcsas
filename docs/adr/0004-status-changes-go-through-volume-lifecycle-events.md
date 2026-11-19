# ADR 0004 — Status changes go through VolumeLifecycle events

- Status: Accepted
- Date: 2026-11-19
- Issue: #892 (architecture review of 2026-11-18; the table of present
  behaviour it rests on is #891)

## Context

"What may happen to a volume in status X?" had no single answer in the
code. The status graph (`VALID_TRANSITIONS` in `db/volumes.py`) was
enforced by `update_status()`, which had 20 callers in four files. Five
raw `UPDATE volumes SET status` writes went around it: two in a catalog
rebuild, the auto-deprecation of a lost volume and the two halves of its
revival (ADR 0003). Five `delete_volume()` calls each had their own
eligibility guard, built from `burn_evidence()` and
`rebuild_burn_unknown()` in `db/volume_events.py`.

The bugs were in the callers' sequences, not in the graph:

- #810: a failed burn sent a BURNED volume back to STAGING, and then
  `session abort` deleted it.
- #668: a killed burn left a volume BURNING, and the next burn did not
  know which status to restore.
- Hand-written STAGING → BURNING → VERIFIED steps were repeated in
  `verify --mark-verified` and in receipt import.

A safer `update_status` would still have left each sequence to its
caller.

## Options considered

1. **Keep `update_status` public and add checks.** Each caller still
   picks the target status and the order of steps, so the next rule
   change still means finding every caller.
2. **A state-machine library.** This would break the project's rule of
   no runtime dependencies (`CLAUDE.md`), and would add nothing a dict
   and a class cannot do.
3. **One module that takes events.** A caller says what happened. The
   module decides the status change. It also writes the `Status changed`
   note, the disc row and the volume events that belong to that event.

## Decision

Option 3. `src/lcsas/db/lifecycle.py` holds `VolumeLifecycle(conn)`.
It has one method per event, named for what the owner does or what
happens. Examples are `burn_started`, `burn_succeeded`,
`disc_check_passed`, `passing_receipt_imported`, `last_disc_lost`,
`consolidation_finished` and `never_burned_deleted`. The table in
`docs/workflows/consolidate-and-catalog-ops.md` lists them all.

- **The graph is private.** `_TRANSITIONS` and the functions that write
  through it (`_update_status`, `_status_before_burning`,
  `_delete_volume`) are private to the module.
- **Burn evidence stays readable.** `_burn_evidence` and
  `_rebuild_burn_unknown` are private too. Read-only code (status,
  reconcile, rebuild) gets them through the read-only methods
  `burn_evidence` and `burn_unknown`.
- **One deletion rule.** `may_delete` holds the rule for which volumes
  may be deleted. It returns a `Refusal` whose kind each command turns
  into its own message.
- **The writes outside the graph stay, inside the module.** These are
  ADR 0003's auto-deprecation and revival (`last_disc_lost`,
  `disc_back_in_service`) and a catalog rebuild's status adoption.
- **The module lives in `db/`.** It is transactional catalog logic with
  no I/O beyond SQLite, and every package that changes a status already
  imports `db`.
- **A burn's outcome takes a record.** `burn_started` returns a `Burn`
  record that the outcome events take. The start decides whether this is
  a re-burn or a first burn, and the outcome never decides it again from
  a status that has since moved.
- **Commit points did not move.** Each method writes in the caller's
  transaction, unless its docstring says it commits, exactly as the
  calls it replaced did.

## Consequences

- **A rule change is made in one place.**
  - Removing STAGING → DEPRECATED (#905) is a `_TRANSITIONS` edit plus a
    check in `consolidation_finished`.
  - Stopping an ISO check from promoting a volume (#906) is a change to
    `iso_check_passed`.
  - Widening burn evidence (#912) is a change to `_burn_evidence`.
- **A guard keeps it that way.**
  `tests/unit/test_volume_status_writers.py` fails on any SQL in
  `src/lcsas` outside `db/lifecycle.py` that writes `volumes.status` or
  deletes a volume. Schema migrations in `db/schema.py` are allowed. It
  also fails on any call of `update_status` or `delete_volume`.
- **The behaviour is pinned.** `tests/unit/test_volume_lifecycle_table.py`
  (#891) pins what every event does from every start state. It checks
  that every event call, every graph write and every status write in the
  module is reached by some row.
- **The docs table is checked.**
  `tests/unit/test_lifecycle_docs_table.py` checks the transition table
  in `docs/workflows/consolidate-and-catalog-ops.md` against
  `_TRANSITIONS` and against the event list.
- **Tests and scripts have one way in.** Tests that need a status
  without driving its event use `tests/unit/volume_status.py`.
  `scripts/e2e_test.py` imports the private `_update_status` to make a
  duplicate volume VERIFIED with no disc, which no event models.
- **New code adds an event.** A command that needs a status change no
  event covers adds a method here, rows to the #891 table, and a line to
  the docs table. It never writes the status itself.
