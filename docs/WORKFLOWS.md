# LCSAS Workflow Matrix

## Intro

This document is the entry point to the LCSAS **workflow matrix**: a living
test plan that enumerates every supported workflow and every variant axis it
must be validated against. Each workflow has a dedicated detail file under
`docs/workflows/`; this index defines the catalog, the variant-axis registry,
and the conventions every detail file follows.

Use the matrix as a structured test plan: pick a detail file, walk its steps,
and record pass/fail per variant. If a behavior is not in the matrix, it is
not a supported workflow.

The matrix mirrors LCSAS's three storage stages. Workflows on the Rustic
mirror (backup-and-scan) belong to the **mirror**. Workflows that assemble
ISO images in the staging area (stage-and-burn, burn-iso-portable, consolidate)
belong to the **staging area**. Workflows that produce, verify, or restore from
discs (location-management, the five restore variants, meta-disc,
verify-and-audit) belong to the **discs**. Workflows that span stages (init,
multi-tenant, recovery-toolchain) are called out as such in their detail files.

## Workflow catalog

| Category | File | One-line description |
|----------|------|----------------------|
| init-and-config | [workflows/init-and-config.md](workflows/init-and-config.md) | First-time setup and TOML config validation. |
| multi-tenant | [workflows/multi-tenant.md](workflows/multi-tenant.md) | Repo add/list/remove and per-repository passwords. |
| backup-and-scan | [workflows/backup-and-scan.md](workflows/backup-and-scan.md) | Rustic backup integration and `lcsas scan` of the mirror. |
| stage-and-burn | [workflows/stage-and-burn.md](workflows/stage-and-burn.md) | Burn-session-based stage + burn pipeline across every media type. |
| burn-iso-portable | [workflows/burn-iso-portable.md](workflows/burn-iso-portable.md) | Standalone `burn-iso`, remote burner, `catalog import-receipts`. |
| location-management | [workflows/location-management.md](workflows/location-management.md) | `location add/list/status/move` and multi-copy sync. |
| restore-host-linux | [workflows/restore-host-linux.md](workflows/restore-host-linux.md) | Restore from a running Linux host with `lcsas restore`. |
| restore-host-macos | [workflows/restore-host-macos.md](workflows/restore-host-macos.md) | Restore from a running macOS host (Apple Silicon or Intel). |
| restore-live-usb | [workflows/restore-live-usb.md](workflows/restore-live-usb.md) | No-OS restore: boot a live-Linux USB (discs are not bootable), then restore. The route that works today. |
| restore-bare-metal | [workflows/restore-bare-metal.md](workflows/restore-bare-metal.md) | initramfs / direct-boot restore from cold start — describes the quarantined boot stack (`experimental/boot/`); use restore-live-usb for the no-OS path that ships today. |
| restore-windows | [workflows/restore-windows.md](workflows/restore-windows.md) | `restore.bat` end-to-end from a Windows host. |
| restore-disc-only | [workflows/restore-disc-only.md](workflows/restore-disc-only.md) | Tier-3 single-disc restore with the Python reader. |
| recovery-toolchain | [workflows/recovery-toolchain.md](workflows/recovery-toolchain.md) | `recovery build/test/manifest/verify`, cross-arch. |
| meta-disc | [workflows/meta-disc.md](workflows/meta-disc.md) | The meta disc (not bootable): bundled recovery binaries + source. |
| consolidate-and-catalog-ops | [workflows/consolidate-and-catalog-ops.md](workflows/consolidate-and-catalog-ops.md) | `consolidate` plus `catalog validate/rebuild`. |
| verify-and-audit | [workflows/verify-and-audit.md](workflows/verify-and-audit.md) | `verify`, `status`, `session list`. |

## Variant axis registry

Detail files cite these axes by name (e.g. "Media type (see registry)") rather
than redefining them. An axis applies to a workflow when changing it changes
the code path or required setup.

The matrix assumes a **single optical drive** as the canonical case. Multi-drive
setups are supported, but the only workflows whose behavior materially changes
between single-drive and multi-drive are the bare-metal and Windows restore
paths (they document a RAM-relocation pattern for the single-drive case). Those
two detail files — `workflows/restore-bare-metal.md` and
`workflows/restore-windows.md` — still call out drive count in their own
"Variant axes that apply" sections. Treat drive count as a per-workflow concern
rather than a top-level axis.

- **Media type** — one of `CD700`, `BD25`, `BD50`, `BDXL100`, `MDISC25`,
  `MDISC50`, `MDISC100`, `TEST_TINY` (defined in
  `src/lcsas/config/media.py`). The seven production types carry 15% ECC
  overhead; the test-only `TEST_TINY` carries 0% (see the ECC axis below).
  `CD700` suits bounded collections and realistic-size testing, not
  decades-scale archival — see the sizing and durability notes in
  `media.py`.
- **Multi-repository** — single registered repository vs. several repositories
  sharing the same discs, each with its own password.
- **OS** — Linux host, Linux bare-metal initramfs, Windows, macOS. Determines
  which entry point (`lcsas`, `restore.sh`, `restore.bat`) is exercised.
- **Storage stage** — the mirror (Rustic mirror on NAS/local disk), the
  staging area (assembled ISO image on SSD/HDD), the discs (burned discs).
- **Multi-copy** — exactly 1 location vs. N locations holding discs of the
  same volume; exercises `volume_copies` rows and `location move`.
- **ECC** — Always enabled for production media types (BD-R, M-Disc, BDXL — 15%
  RS03 overhead). The test-only `TEST_TINY` media type is implicitly skipped
  (`MediaType.ecc_overhead_pct == 0`). There is no owner-facing toggle.
- **Live distro** — yes (restore run from a live system booted from USB) vs. no
  (workflow runs under the host OS).
- **Recovery tier** — `1` prebuilt static `lcsas-restore`, `2` pinned
  upstream `rustic-static`, `3` the Python reader (`standalone_restorer.py`).
  Full definitions in `recovery/docs/TIERS.txt`; tiers 1–2 are the
  bare-minimum path and must remain Python-free.

## Status legend

Each detail file ends with a "Test coverage" table using these tokens:

- `[covered]` — automated test exists and is green on the current branch.
- `[partial]` — code path is exercised but the variant axis is not, or
  assertions are incomplete.
- `[gap]` — no test exists; manual validation only.
- `[N/A]` — variant does not apply to this workflow.

## How to validate a workflow

1. Read the detail file's "Preconditions" and prepare the required repository,
   locations, and media.
2. For each applicable variant axis, pick one value per run and record it.
3. Execute the numbered steps in order; use the source refs to confirm each
   handler actually runs.
4. Compare the result against the "Expected outcome" section.
5. Update the "Test coverage" table — flip `[gap]` to `[covered]` only when
   an automated test pins the exact variant.

## Conventions

Every step in every detail file carries a source reference pointing at the
code that implements it — by symbol name, e.g. `` `cmd_restore_plan()` in
`src/lcsas/cli/main.py` `` (symbol names survive the refactors that rot line
numbers; a few older detail files still carry legacy `file.py:LINE` refs
pending migration). Paths are repo-relative (rooted at the LCSAS checkout)
so they remain stable across worktrees and forks.
