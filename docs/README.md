# LCSAS Documentation

This is the map of the LCSAS documentation tree. Each area below has its own
home; start here and follow the link that matches what you need.

> **In an emergency and need to restore data?** Go straight to the
> [Recovery Guide](RECOVERY_GUIDE.md) — it routes you to the right
> OS-specific walkthrough in one click.

## Architecture

- [architecture.md](architecture.md) — synthesized
  architecture reference: storage-tier model, burn pipeline, holographic
  catalog, multi-tenancy, and the recovery cascade.

## On-disc formats

How the bytes on a disc are laid out, encrypted, and protected:

- [RESTIC_FORMAT_SPEC.md](RESTIC_FORMAT_SPEC.md) — Rustic/restic pack file
  format (the encrypted, deduplicated blobs LCSAS archives).
- [DVDISASTER_RS03_FORMAT.md](DVDISASTER_RS03_FORMAT.md) — DVDisaster RS03 error-correction
  layer wrapped around every burned image.
- [KEY_SHARE_FORMAT.md](KEY_SHARE_FORMAT.md) — SLIP-0039 Shamir key-share
  format used by the `keyshare` package and `lcsas key` escrow.
- [DISC_CONFIDENTIALITY.md](DISC_CONFIDENTIALITY.md) — what a
  disc does and does not reveal to someone who finds it.

## Operator guides

Task-oriented walkthroughs for running and recovering an archive:

- [guides/recovery-runbook.md](guides/recovery-runbook.md) — step-by-step
  disaster-recovery runbook.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — diagnosing and
  fixing common failures.
- [guides/survivability.md](guides/survivability.md) — durability rationale:
  why the cascade and disc-integrity layers survive decades.
- [RECOVERY_GUIDE.md](RECOVERY_GUIDE.md) — emergency router to the OS-specific
  recovery path (Linux, macOS, Windows, bare-metal, disc-only fallback).
- [ESTATE_PLANNING.md](ESTATE_PLANNING.md) — handing the archive to an heir.

## Workflows

- [WORKFLOWS.md](WORKFLOWS.md) — the workflow matrix: the catalog of every
  supported workflow, the variant-axis registry, and one detail file per
  workflow under [workflows/](workflows/).

## Development

- [development/roadmap.md](development/roadmap.md) — phased development plan and
  roadmap.
- [CROSS_PLATFORM_META_RFC.md](CROSS_PLATFORM_META_RFC.md)
  — RFC for cross-platform meta-volume / tier-1 recovery-binary coverage.

## On-disc recovery manuals

Plain-text manuals burned onto every meta-volume and followed literally by an
heir on a machine with nothing else installed:

- [../recovery/docs/](../recovery/docs/) — `RECOVER.txt`, `TIERS.txt`,
  `ENV_VARS.txt`, `PHYSICAL_DISC_VALIDATION.txt`, and the rest of the on-disc
  operator manual.

## Project root

- [../README.md](../README.md) — developer intro and usage guide.
- [../CLAUDE.md](../CLAUDE.md) — architecture map and contributor guidance.
