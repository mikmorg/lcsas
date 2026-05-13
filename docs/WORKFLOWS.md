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

The matrix mirrors LCSAS's three-tier storage model. Workflows on the Rustic
mirror (backup-and-scan) live in **Tier 0 — HOT**. Workflows that assemble
staging trees and master ISOs (stage-and-burn, burn-iso-portable, consolidate)
live in **Tier 1 — WARM**. Workflows that produce, verify, or restore from
physical media (location-management, the four restore variants, meta-volume,
verify-and-audit) live in **Tier 2 — COLD**. Cross-tier workflows (init,
multi-tenant, recovery-toolchain) are called out as such in their detail files.

## Workflow catalog

| Category | File | One-line description |
|----------|------|----------------------|
| init-and-config | [workflows/init-and-config.md](workflows/init-and-config.md) | First-time setup, TOML config validation, database export. |
| multi-tenant | [workflows/multi-tenant.md](workflows/multi-tenant.md) | Repo add/list/remove and per-repo encryption keys. |
| backup-and-scan | [workflows/backup-and-scan.md](workflows/backup-and-scan.md) | Rustic backup integration and `lcsas scan` of the mirror. |
| stage-and-burn | [workflows/stage-and-burn.md](workflows/stage-and-burn.md) | Session-based stage + burn pipeline across every media type. |
| burn-iso-portable | [workflows/burn-iso-portable.md](workflows/burn-iso-portable.md) | Standalone `burn-iso`, remote burner, `catalog import-receipts`. |
| location-management | [workflows/location-management.md](workflows/location-management.md) | `location add/list/status/move` and multi-copy sync. |
| restore-host-linux | [workflows/restore-host-linux.md](workflows/restore-host-linux.md) | Restore from a running Linux host with `lcsas restore`. |
| restore-bare-metal | [workflows/restore-bare-metal.md](workflows/restore-bare-metal.md) | initramfs + live-USB recovery from cold start. |
| restore-windows | [workflows/restore-windows.md](workflows/restore-windows.md) | `restore.bat` end-to-end from a Windows host. |
| restore-disc-only | [workflows/restore-disc-only.md](workflows/restore-disc-only.md) | Tier-5 pure-Python single-disc restore. |
| recovery-toolchain | [workflows/recovery-toolchain.md](workflows/recovery-toolchain.md) | `recovery build/test/manifest/verify`, cross-arch. |
| meta-volume | [workflows/meta-volume.md](workflows/meta-volume.md) | Bootable disaster-recovery disc with bundled binaries + source. |
| consolidate-and-catalog-ops | [workflows/consolidate-and-catalog-ops.md](workflows/consolidate-and-catalog-ops.md) | `consolidate` plus `catalog validate/rebuild`. |
| verify-and-audit | [workflows/verify-and-audit.md](workflows/verify-and-audit.md) | `verify`, `status`, `session list`. |

## Variant axis registry

Detail files cite these axes by name (e.g. "Media type (see registry)") rather
than redefining them. An axis applies to a workflow when changing it changes
the code path or required setup.

- **Media type** — one of `BD25`, `BD50`, `BDXL100`, `MDISC25`, `MDISC100`,
  `LTO8`, `LTO9`, `TEST_TINY` (defined in `src/lcsas/config/media.py`).
  Optical types carry 15% ECC overhead; LTO carries 0%.
- **Multi-tenant** — single registered repo vs. multiple repos sharing
  physical volumes, each with its own encryption key.
- **OS** — Linux host, Linux bare-metal initramfs, Windows, macOS. Determines
  which entry point (`lcsas`, `restore.sh`, `restore.bat`) is exercised.
- **Optical drive count** — single drive (sequential burn) vs. multiple drives
  (parallel burn / multi-copy sync).
- **Data tier location** — HOT (Rustic mirror on NAS/local disk), WARM
  (assembled ISO on staging SSD/HDD), COLD (burned disc or tape).
- **Multi-copy** — exactly 1 location vs. N locations holding copies of the
  same volume; exercises `volume_copies` rows and `location move`.
- **ECC** — DVDisaster RS03 augmentation enabled (BD-R, M-Disc) vs. skipped
  (LTO, since tape provides its own ECC).
- **Live distro** — yes (recovery booted from USB / meta-volume) vs. no
  (workflow runs under the host OS).
- **Recovery tier** — `1` prebuilt static `lcsas-restore`, `2` vendored
  `rustic-static`, `3` rebuild `lcsas-restore` from C source, `4` rebuild
  `rustic` from vendored Rust source, `5` pure-Python `standalone_restorer.py`.
  Full definitions in `recovery/docs/TIERS.txt`; tiers 1–4 are the
  bare-minimum path and must remain Python-free.

## Status legend

Each detail file ends with a "Test coverage" table using these tokens:

- `[covered]` — automated test exists and is green on the current branch.
- `[partial]` — code path is exercised but the variant axis is not, or
  assertions are incomplete.
- `[gap]` — no test exists; manual validation only.
- `[N/A]` — variant does not apply to this workflow (e.g. ECC on LTO).

## How to validate a workflow

1. Read the detail file's "Preconditions" and prepare the required repo,
   locations, and media.
2. For each applicable variant axis, pick one value per run and record it.
3. Execute the numbered steps in order; use the source refs to confirm each
   handler actually runs.
4. Compare the result against the "Expected outcome" section.
5. Update the "Test coverage" table — flip `[gap]` to `[covered]` only when
   an automated test pins the exact variant.

## Conventions

Every step in every detail file carries a source reference of the form
`(path/to/file.py:LINE)` pointing at the handler that implements it. Paths
are repo-relative (rooted at the LCSAS checkout) so they remain stable across
worktrees and forks.
