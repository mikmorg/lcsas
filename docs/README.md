# LCSAS Documentation

This is the map of the LCSAS documentation tree. Each area below has its own
home; start here and follow the link that matches what you need.

> **In an emergency and need to restore data?** Go straight to the
> [Recovery Guide](RECOVERY_GUIDE.md) — it routes you to the right
> OS-specific walkthrough in one click.

## Architecture

- [architecture.md](architecture.md) — synthesized
  architecture reference: storage stages (mirror, staging area, discs),
  burn pipeline, disc catalog, multiple repositories, and the recovery
  cascade.

## On-disc formats

How the bytes on a disc are laid out, encrypted, and protected:

- [RESTIC_FORMAT_SPEC.md](RESTIC_FORMAT_SPEC.md) — Rustic/restic pack file
  format (the encrypted, deduplicated blobs LCSAS archives).
- [DVDISASTER_RS03_FORMAT.md](DVDISASTER_RS03_FORMAT.md) — DVDisaster RS03 error-correction
  layer wrapped around every burned image.
- [SHARE_FORMAT.md](SHARE_FORMAT.md) — SLIP-0039 Shamir share
  format used by the `keyshare` package and the `lcsas key` commands
  to split a password.
- [DISC_CONFIDENTIALITY.md](DISC_CONFIDENTIALITY.md) — what a
  disc does and does not reveal to someone who finds it.

## Guides

Task-oriented walkthroughs for running an archive and restoring from it:

- [guides/recovery-runbook.md](guides/recovery-runbook.md) — owner-side Linux
  restore context. The heir's steps are `recovery/docs/RECOVER.txt`.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — diagnosing and
  fixing common failures.
- [guides/survivability.md](guides/survivability.md) — durability rationale:
  why the cascade and disc-integrity layers survive decades.
- [RECOVERY_GUIDE.md](RECOVERY_GUIDE.md) — emergency router to the OS-specific
  restore path (Linux, macOS, Windows, live USB, disc-only fallback).
- [ESTATE_PLANNING.md](ESTATE_PLANNING.md) — handing the archive to an heir.
- [PASSWORD_CARD.txt](PASSWORD_CARD.txt) — printable fill-in-the-blanks
  password-card template for one repository's password (never kept with
  the discs — password and data stay physically apart).
- [RUNBOOK_migration_recovery.md](RUNBOOK_migration_recovery.md) — runbook for
  repairing a catalog wedged by an interrupted schema migration.

## Workflows

- [WORKFLOWS.md](WORKFLOWS.md) — the workflow matrix: the catalog of every
  supported workflow, the variant-axis registry, and one detail file per
  workflow under [workflows/](workflows/).

## Development

- [development/roadmap.md](development/roadmap.md) — phased development plan and
  roadmap.
- [adr/](adr/) — architecture decision records:
  - [0001](adr/0001-audit-gate-exemptions-reconciliation.md) — audit-gate
    exemptions reconciliation.
  - [0002](adr/0002-tier-means-only-a-recovery-tier.md) — "tier" means only
    a recovery tier; storage stages are named, disc sizes are media types.
  - [0003](adr/0003-lost-volumes-are-not-a-schema-state.md) — lost volumes
    are recorded as DEPRECATED and shown as LOST; no schema change.

## History

Dated records, kept as written. They are not current descriptions. See
[history/README.md](history/README.md).

- [history/PROJECT_REVIEW_2026-07.md](history/PROJECT_REVIEW_2026-07.md) — full-project
  review at `f181f11` (2026-07-01) that seeded the #361–#415 remediation.
- [history/READINESS_ASSESSMENT_2026-06.md](history/READINESS_ASSESSMENT_2026-06.md) —
  "is it ready?" assessment against the decades-long survivability bar.
- [history/CROSS_PLATFORM_META_RFC.md](history/CROSS_PLATFORM_META_RFC.md) — the
  design RFC for the cross-platform meta disc (implemented 2026-05).

## On-disc recovery manuals

Plain-text manuals burned onto every meta disc and followed literally by an
heir on a machine with nothing else installed:

- [../recovery/docs/](../recovery/docs/) — `RECOVER.txt`, `TIERS.txt`,
  `ENV_VARS.txt`, `PHYSICAL_DISC_VALIDATION.txt`, and the rest of the on-disc
  recovery manual.

## Project root

- [../README.md](../README.md) — developer intro and usage guide.
- [../CLAUDE.md](../CLAUDE.md) — architecture map and contributor guidance.
