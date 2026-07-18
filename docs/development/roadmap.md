# LCSAS — Roadmap & Shipped-Phases Summary

> Forward-looking roadmap plus a concise record of completed work.  For the
> living architecture reference see [`../architecture.md`](../architecture.md);
> for the cross-platform meta-volume design see
> [`../CROSS_PLATFORM_META_RFC.md`](../CROSS_PLATFORM_META_RFC.md).

LCSAS (Linux Cold Storage Archival Suite) orchestrates Rustic, Xorriso, and
DVDisaster to write deduplicated, encrypted data packs onto optical media
(BD-R, M-Disc). Core capabilities:

- **CDC-based infinite incrementalism** via Rustic (zero-cost renames/moves)
- **Multi-tenant encryption isolation** (per-repo keys, shared physical media)
- **Holographic indexing** (complete SQLite catalog on every disc)
- **Multi-copy location tracking** (burn N copies, each tagged to a location)
- **Session-based multi-volume staging** (decouple ISO creation from burning)
- **DVDisaster RS03 ECC** (image-level error correction, always-on for production media)
- **3-tier recovery cascade** (in-house C `lcsas-restore` → pinned upstream
  `rustic-static` → pure-Python `standalone_restorer.py`; tiers 1–2 are Python-free)
- **Shamir key escrow** (recorded K/N + SLIP-0039 split; `key_escrow` table, KEY-08)

The codebase has zero runtime pip dependencies (pure stdlib; `zstandard` is
optional for the pure-Python tier-3 fallback). Catalog schema is at **v9**.

---

## 1. Forward roadmap

Items below are deferred beyond the work already shipped and are listed
roughly in descending order of "addresses a real limitation users have hit."

| Item | Status | Notes |
|---|---|---|
| **RISC-V meta-volume target** | Blocked on upstream | `riscv64gc-*` has no upstream rustic / python-build-standalone artifact yet. Add when upstream ships. See [`../CROSS_PLATFORM_META_RFC.md`](../CROSS_PLATFORM_META_RFC.md) §6 Q1. |
| **Windows ARM64 (`aarch64-pc-windows-msvc`)** | Blocked on upstream | No upstream rustic artifact. Non-goal until it lands. |
| **FreeBSD / OpenBSD targets** | Deferred | No upstream rustic artifact; different binary formats. |
| **Cloud tier (S3/rclone)** | Out of scope | Architectural extension; would grow the storage-tier model from HOT/WARM/COLD to HOT/WARM/COLD/REMOTE. |
| **Multi-session optical writing** | Out of scope | Adds complexity; the current whole-disc model is simpler and sufficient. |
| **Dashboard / rich status TUI** | Out of scope | `lcsas status` and `verify --all` are sufficient for operator use. |
| **Email / webhook notifications** | Out of scope | External orchestration (cron, systemd timers) can handle this. |

---

## 2. Shipped phases (summary)

All planned phases through Phase 21.12 are complete. The detail tables below
are a historical record; the canonical behavior is the code + tests.

### Phases 1–20 — foundation, hardening, completeness

| Phase | Title | Status |
|---|---|---|
| 1 | Bug fixes & code cleanup | ✅ |
| 2 | Wire `scan` CLI command (extended `--no-prune-sync` in 16) | ✅ |
| 3 | Wire `restore plan` + `restore exec` | ✅ |
| 4 | Wire `verify` CLI | ✅ |
| 5 | Wire `consolidate` CLI | ✅ |
| 6 | Wire `burn-iso` CLI | ✅ |
| 7 | Snapshot persistence | ✅ |
| 8 | Prune synchronization (delivered as Phase 16) | ✅ |
| 9 | Verification tracking (delivered as Phases 12 + 14) | ✅ |
| 10 | Stage dry-run + config validation | ✅ |
| 11 | 50-year survivability hardening | ✅ |
| 12 | Schema v4 (locations, volume_copies, burn_sessions, volume_events) | ✅ |
| 13 | Orchestrator refactoring (session-based staging) | ✅ |
| 14 | Verification pipeline (`cmd_verify --all`, event emission) | ✅ |
| 15 | Resilient restore (`PackSource`, alternates, `collect_failures`) | ✅ |
| 16 | Prune sync (`detect_pruned()`, integrated into `cmd_scan()`) | ✅ |
| 17 | Two-level staging layout (`data/<prefix>/<hash>`) | ✅ |
| 18 | Pure-Python restore improvements (hardlink dedup, xattr) | ✅ |
| 19 | CLI & operational (locked_connection, repo remove, XDG db_path) | ✅ |
| 20 | Documentation refresh (architecture, security, plan) | ✅ |

Key Phase 12–20 design decisions retained for traceability:

- **Volume status transitions** (12.3): `update_status()` enforces a
  `VALID_TRANSITIONS` graph (`STAGING → BURNING → BURNED → VERIFIED →
  DEPRECATED → DESTROYED`, with retry/re-burn back-edges); `force_status()`
  is the admin escape hatch.
- **Per-volume pack integrity** (D6): tracked via `volume_events` rather than
  denormalizing `sha256` into `volume_packs`.
- **Remote verification** (S1): `lcsas verify --mark-verified` /
  `--mark-failed` support burning on machine A and verifying on machine B.
- **Backward-compatible restore** (A6): the executor retains a
  flat-then-two-level pack search so pre-Phase-17 discs remain readable.
- **Catalog merge** (S3): skipped — the holographic design (latest disc holds
  the cumulative catalog) makes it unnecessary in practice.
- **LTO tape I/O** (O2): dropped; LTO support was removed. Media is optical-only.

### Post-Phase-20 cleanup wave

Follow-up PRs covered: location-event audit, `--config` flag honoring,
receipt-provenance persistence, unknown-`--location` rejection, ECC
verify-or-repair on mounted ISOs, LTO removal, test-media simplification,
`lcsas db export` removal (redundant with holographic injection), recovery
cascade collapse from 5 tiers to 3, vestigial-flag removal (`--key-file`,
`--skip-ecc`), always-on ECC for production media, `cmd_burn_legacy` removal,
and a GitHub Actions test workflow exercising real rustic.

### Phase 21 — cross-platform meta-volume (SHIPPED 2026-05-17)

Phases 21.1–21.12 delivered the full cross-platform meta-volume. All three
recovery tiers now cover **all six approved targets**:

- `x86_64-unknown-linux-musl`
- `aarch64-unknown-linux-musl`
- `armv7-unknown-linux-gnueabihf`
- `aarch64-apple-darwin`
- `x86_64-apple-darwin`
- `x86_64-pc-windows-gnu`

Tier-1 (`lcsas-restore`) cross-compiles via `zig cc -target X-{musl,windows-gnu,macos}`
— notably the macOS targets need no Apple SDK because zig bundles enough
libSystem. See [`../CROSS_PLATFORM_META_RFC.md`](../CROSS_PLATFORM_META_RFC.md)
§6 Q6 for the full sequence. Phase 21 also added 90+ tests across
`cmd_consolidate`, the dispatcher, bundlers, and verify paths.

### Later format/recovery work (SHIPPED)

- **FMT-01 — in-house RS03 ECC (`lcsas-ecc`):** the RS03 error-correction
  encode/decode path is implemented in-house (C, alongside the vendored
  recovery toolchain) for the recovery-side path, rather than depending on the
  upstream dvdisaster binary. Validated against the real dvdisaster binary for
  interoperability.
- **KEY-08 — Shamir key escrow:** the `key_escrow` table records a Shamir
  split (K/N threshold + SLIP-0039 identifier); `lcsas-keyshare` performs the
  split/combine. Catalog at schema v9 includes this table.

---

## 3. Definition of done (per phase)

- [x] All new code has corresponding unit tests
- [x] All existing tests still pass (zero regressions)
- [x] `ruff check` passes
- [x] `mypy --strict` passes
- [x] Changes committed with a descriptive commit message
- [x] README / workflow docs updated when user-facing behavior changes

---

## 4. Verification strategy

After each phase:

1. Full test suite green (`make test-all`).
2. New tests cover every changed function.
3. Integration tests verify end-to-end flows (gated on rustic/xorriso/dvdisaster).
4. `ruff check src/ tests/` — no lint regressions; `mypy --strict` clean.
5. For burn/restore-affecting changes: a manual burn + verify + restore on
   real BD-R media (the hardware drill in
   `recovery/docs/PHYSICAL_DISC_VALIDATION.txt`).
