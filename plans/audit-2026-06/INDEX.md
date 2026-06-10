# Remediation plan index — deep audit 2026-06

One implementation-ready plan per confirmed audit finding (see [`DEEP_AUDIT_2026-06.md`](../../DEEP_AUDIT_2026-06.md)),
plus three follow-up audit areas (FUP) and the Windows-e2e CI scaffolding (INFRA).
Workflow: one GitHub issue per plan (each file carries a suggested issue title), issue → branch → PR per the repo convention.

**82 plans.** Severity counts: 6 critical / 36 high / 33 medium / 7 low.

## P0 — Do before the next real burn — closes data-loss windows or unbreaks an heir path.

17 plans, ~40 focused days (estimates overlap where plans share scaffolding).

| ID | Plan | Severity | Days | Area |
|----|------|----------|-----:|------|
| INFRA-01 | [Windows-journey e2e on GitHub CI (wine loop + tmate + two-job gate)](INFRA-01-windows-e2e-ci-scaffolding.md) | high | 5 | Infrastructure |
| BURN-01 | [Fail loud when a repo mirror is missing at stage time](BURN-01-fail-loud-missing-mirror-stage.md) | critical | 1.5 | Burn pipeline |
| BURN-02 | [Hash-verify every staged pack against its catalog SHA-256](BURN-02-hash-verify-staged-pack-content.md) | critical | 2.5 | Burn pipeline |
| BURN-03 | [Compensate failed stages; guard clean_session; add session abort](BURN-03-reclaim-packs-from-failed-staging.md) | critical | 3 | Burn pipeline |
| BURN-04 | [Post-burn verify must compare the physical disc to the ISO SHA-256](BURN-04-device-readback-sha256-verify.md) | high | 2.5 | Burn pipeline |
| BURN-05 | [A failed verify must not record an ACTIVE copy or a COMPLETE session](BURN-05-failed-verify-no-active-copy.md) | high | 1 | Burn pipeline |
| FMA-01 | [Packs on never-burned STAGING volumes are counted 'archived' forever](FMA-01-staged-never-burned-counted-archived.md) | critical | 3 | Failure modes / catalog lifecycle |
| FMA-03 | [Post-burn 'verification' never compares disc content to the ISO hash](FMA-03-verified-without-content-compare.md) | high | 3 | Failure modes / catalog lifecycle |
| FMA-04 | [A failed post-burn verify still records an ACTIVE volume copy](FMA-04-failed-verify-records-active-copy.md) | high | 1.5 | Failure modes / catalog lifecycle |
| UX-01 | [restore.bat cannot discover the repo on a real meta-volume](UX-01-restore-bat-repo-discovery.md) | critical | 3 | Heir UX journey |
| UX-02 | [Split-key on-disc docs cite nonexistent restore.sh flags; add docs-vs-reality gate](UX-02-split-key-phantom-flags-doc-gate.md) | high | 2 | Heir UX journey |
| UX-03 | [Burned docs promise 'boot from the disc' that no build can produce](UX-03-boot-promise-routing-rewrite.md) | high | 1.5 | Heir UX journey |
| UX-04 | [RECOVER_WINDOWS.txt gives wrong binary paths and wrong restorer commands throughout](UX-04-recover-windows-doc-sweep.md) | high | 1 | Heir UX journey |
| KEY-01 | [Real share-card files are rejected by every combiner](KEY-01-card-files-rejected-by-combiners.md) | high | 2.5 | Key escrow |
| KEY-02 | [On-disc split-key docs reference restore.sh flags that do not exist (--target, --key)](KEY-02-restore-sh-phantom-target-key-flags.md) | high | 2 | Key escrow |
| KEY-03 | [lcsas key split never verifies the escrowed secret](KEY-03-key-split-never-verifies-secret.md) | high | 2.5 | Key escrow |
| BOOT-01 | [Drop the bootable meta-volume; quarantine the scaffolding; reroute the no-OS journey](BOOT-01-drop-bootable-path-quarantine.md) | high | 2 | Boot / live-distro |

## P1 — Make the gates real and fix remaining high-severity issues.

30 plans, ~68 focused days (estimates overlap where plans share scaffolding).

| ID | Plan | Severity | Days | Area |
|----|------|----------|-----:|------|
| BURN-06 | [Stop auto-deleting ISOs after the first burn — multi-location re-burn is broken](BURN-06-retain-iso-for-multi-location-burns.md) | high | 1 | Burn pipeline |
| FMA-02 | [Schema migrations are never executed by any production code path](FMA-02-schema-migrations-never-run.md) | high | 1.5 | Failure modes / catalog lifecycle |
| FMA-07 | [Table-recreating migrations are not crash-atomic; create_all masks the wreck as EMPTY](FMA-07-migrations-not-crash-atomic.md) | medium | 1.5 | Failure modes / catalog lifecycle |
| UX-05 | [Production META discs get the weakest START_HERE.txt](UX-05-meta-start-here-variant.md) | high | 1.5 | Heir UX journey |
| UX-08 | [The only end-to-end journey gate is Linux-only, local-only, opt-in](UX-08-cross-os-journey-gates.md) | medium | 2 | Heir UX journey |
| KEY-04 | [Blind split-key drill spoon-feeds commands and stages non-production share artifacts](KEY-04-blind-split-key-spoonfed-prompt.md) | high | 1.5 | Key escrow |
| KEY-06 | [lcsas-keyshare C combiner sits outside every tier-1 audit gate](KEY-06-keyshare-outside-audit-gates.md) | medium | 1.5 | Key escrow |
| RST-01 | [Multi-disc `lcsas restore standalone` falsely fails after a complete restore cache](RST-01-multidisc-restore-false-failure.md) | high | 1.5 | Python restore |
| RST-02 | [zstd-magic sniff falsely decompresses uncompressed blobs (tiers 1 + 3)](RST-02-zstd-magic-misclassifies-uncompressed-blobs.md) | high | 2 | Python restore |
| RST-03 | [One corrupt/missing blob aborts the entire tier-3 restore](RST-03-tier3-skip-and-continue-restore.md) | high | 2 | Python restore |
| RST-04 | [Tier-3 zstd only works on the build host's arch + CPython minor](RST-04-tier3-zstd-cross-target-portability.md) | high | 3 | Python restore |
| RST-05 | [No meta-volume completeness gate — incomplete rescue discs pass every check](RST-05-meta-volume-completeness-gate.md) | high | 2 | Python restore |
| FMT-01 | [Bring RS03 ECC repair onto the bare recovery path (decision + implementation)](FMT-01-rs03-repair-in-house-decision.md) | critical | 12 | Format durability |
| FMT-02 | [Complete the RS03 spec; bundle pinned dvdisaster source on the meta-volume](FMT-02-rs03-spec-completion-source-bundle.md) | high | 3 | Format durability |
| FMT-03 | [Burn-time gate against rustic writer / pinned-reader format drift](FMT-03-rustic-writer-drift-burn-preflight.md) | high | 2.5 | Format durability |
| GATE-01 | [Intel-Mac tier-1 binary is gitignored and absent from the repo](GATE-01-commit-intel-mac-tier1-binary.md) | high | 0.5 | Test & CI gates |
| GATE-02 | [The 'shippable build' gate (recovery-hardening + e2e) never runs in CI](GATE-02-wire-hardening-e2e-into-ci.md) | high | 2 | Test & CI gates |
| GATE-03 | [macOS tier-1 binaries are built but never executed anywhere](GATE-03-execute-macos-tier1-binaries-ci.md) | high | 1.5 | Test & CI gates |
| GATE-04 | [audit-gate path filter excludes vendored C, keyshare, and all recovery scripts; test.yml never compiles C](GATE-04-close-audit-gate-path-filter-holes.md) | high | 1 | Test & CI gates |
| GATE-05 | [RS03 ECC repair proof is opt-in and never runs in CI](GATE-05-schedule-ecc-repair-proof-ci.md) | high | 1 | Test & CI gates |
| GATE-06 | [tier1-missing blind variant is permanently XFAIL — the cascade's headline hedge can never fail a run](GATE-06-tier1-missing-blind-variant-xfail.md) | high | 2.5 | Test & CI gates |
| GATE-07 | [CI audit-gate runs THRESHOLD=60 vs documented 88, and coverage_check fails open on an empty report](GATE-07-coverage-threshold-parity-fail-closed.md) | medium | 1 | Test & CI gates |
| T1C-01 | [Adaptive JSON token buffers; fail loud on index parse overflow](T1C-01-adaptive-json-token-caps.md) | high | 3 | Tier-1 C binary |
| T1C-04 | [Depth-cap the tree walk; fuzz lcsas_tree_restore](T1C-04-tree-depth-cap-and-fuzz.md) | medium | 2 | Tier-1 C binary |
| BOOT-02 | [build_initramfs.sh must hard-fail on missing sources, not ship zero-byte placeholders](BOOT-02-initramfs-hard-fail-missing-sources.md) | high | 1 | Boot / live-distro |
| BOOT-03 | [Boot menu entries point at paths the builder never stages](BOOT-03-boot-config-path-mismatches.md) | high | 1 | Boot / live-distro |
| BOOT-04 | [Validate the live-USB replacement path (Secure Boot, ARM, future drivers)](BOOT-04-live-usb-replacement-validation.md) | high | 2.5 | Boot / live-distro |
| BOOT-05 | [restore.sh must warn when the restore target is RAM-backed (tmpfs)](BOOT-05-tmpfs-restore-target-warning.md) | high | 1 | Boot / live-distro |
| FUP-01 | [Operator burn protocol — disc swaps, blank checks, identity readback, labeling](FUP-01-burn-operator-protocol.md) | high | 4.5 | Follow-up areas |
| FUP-02 | [Catalog concurrency — staging-clean TOCTOU, silent lock waits, unlocked writers](FUP-02-catalog-concurrency.md) | high | 4.5 | Follow-up areas |

## P2 — Hardening, friction fixes, docs, and polish.

35 plans, ~44 focused days (estimates overlap where plans share scaffolding).

| ID | Plan | Severity | Days | Area |
|----|------|----------|-----:|------|
| BURN-07 | [RS03 ignores the redundancy knob; pre-flight must budget full-medium padding](BURN-07-rs03-redundancy-knob-and-preflight.md) | medium | 1.5 | Burn pipeline |
| BURN-08 | [Durable burn receipts; document the self-stale on-disc catalog](BURN-08-durable-receipts-stale-disc-catalog.md) | medium | 1 | Burn pipeline |
| BURN-09 | [Prune-sync guards — incomplete scans, mass-prune threshold, unprune](BURN-09-prune-sync-safety-guards.md) | medium | 1.5 | Burn pipeline |
| BURN-10 | [Replica safety math must count ACTIVE copies, not volume status](BURN-10-replica-truth-active-copies.md) | medium | 1.5 | Burn pipeline |
| FMA-05 | [No tooled disc-rot re-verification; last_verified_at is a dead column](FMA-05-no-disc-rot-reverification.md) | medium | 2 | Failure modes / catalog lifecycle |
| FMA-06 | [Catalog rebuild resurrects DEPRECATED/DESTROYED volumes to VERIFIED](FMA-06-rebuild-resurrects-destroyed-volumes.md) | medium | 2 | Failure modes / catalog lifecycle |
| FMA-08 | [No blast-radius reporting: what is lost if all copies of disc X fail](FMA-08-no-blast-radius-reporting.md) | medium | 2.5 | Failure modes / catalog lifecycle |
| FMA-09 | [Heir restore has no disk-space preflight; ENOSPC mid-restore is the failure mode](FMA-09-restore-disk-space-preflight.md) | medium | 2 | Failure modes / catalog lifecycle |
| FMA-10 | [Burn provenance for the newest session is never holographic; rebuild drops the audit trail](FMA-10-burn-provenance-not-holographic.md) | low | 1 | Failure modes / catalog lifecycle |
| UX-06 | [restore.sh --help QUICK START points at 'ANY data disc', which never carries restore.sh](UX-06-help-quick-start-meta-disc.md) | medium | 0.25 | Heir UX journey |
| UX-07 | [Restore silently overwrites a non-empty target directory](UX-07-nonempty-target-restore-guard.md) | medium | 1.5 | Heir UX journey |
| UX-09 | [Promised printable Recovery Card was never built](UX-09-printable-recovery-card.md) | low | 1 | Heir UX journey |
| KEY-05 | [Heir docs name only the python3 combiner; C/Windows/printed-guide paths uncovered](KEY-05-c-combiner-undocumented-for-heirs.md) | medium | 1 | Key escrow |
| KEY-07 | [C combiner gives no actionable typo feedback; 4-letter-prefix property unused](KEY-07-typo-feedback-and-prefix-entry.md) | medium | 2 | Key escrow |
| KEY-08 | [key_split/K/N on-disc instructions are self-reported config, never reconciled with the actual split](KEY-08-key-split-config-drift-unreconciled.md) | medium | 2 | Key escrow |
| KEY-09 | [Promised Recovery Card template doesn't exist; single-key printed artifacts are manual homework](KEY-09-recovery-card-template-missing.md) | medium | 1.5 | Key escrow |
| KEY-10 | [KEY_SHARE_FORMAT.md falsely claims wordlist/combiner are pinned in MANIFEST.sha256](KEY-10-keyshare-manifest-pinning-claim.md) | low | 0.5 | Key escrow |
| RST-06 | [Tier-3 symlink escape guard is dead code](RST-06-dead-symlink-escape-guard.md) | medium | 0.5 | Python restore |
| RST-07 | [Interactive 'skip' bypasses alternates; missing-pack errors print hashes not labels](RST-07-skip-disc-alternates-and-labels.md) | medium | 1.5 | Python restore |
| RST-08 | [Tier-3 hardlinks reconstructed per-directory only — cross-dir links become full copies](RST-08-cross-directory-hardlink-copies.md) | low | 0.5 | Python restore |
| RST-09 | [standalone_restorer.py claims Python ≥ 3.10 but imports datetime.UTC (3.11+)](RST-09-standalone-python-floor-claim.md) | low | 0.5 | Python restore |
| FMT-04 | [Guard against >=4 GiB files in ISOs (Windows CDFS multi-extent trap)](FMT-04-iso-4gib-multiextent-guard.md) | medium | 1 | Format durability |
| FMT-05 | [Tier-1 catalog reader: schema forward-compat contract](FMT-05-tier1-catalog-schema-forward-compat.md) | medium | 2 | Format durability |
| FMT-06 | [Correct the inflated ~30% ECC repair-capacity claims](FMT-06-ecc-capacity-claims-correction.md) | medium | 0.5 | Format durability |
| FMT-07 | [Temper M-DISC longevity claims; disclose the BDXL drive requirement](FMT-07-mdisc-claims-bdxl-drive-caveat.md) | medium | 1 | Format durability |
| GATE-08 | [No gate proves committed recovery/bin artifacts match current source — stale binaries ship on meta discs](GATE-08-committed-bins-rebuild-diff-gate.md) | medium | 1.5 | Test & CI gates |
| GATE-09 | [shell-coverage gate enforces 60% vs documented 90%, swallows pytest failures, wired into nothing](GATE-09-shell-coverage-threshold-and-wiring.md) | medium | 1 | Test & CI gates |
| GATE-10 | [test-e2e hard-skips off-host (/mnt/lcsas-data), making the gate green while running nothing](GATE-10-portable-e2e-pipeline-test.md) | low | 1 | Test & CI gates |
| T1C-02 | [Guard lcsas_json_decode_int against signed overflow; fuzz NUMBER tokens](T1C-02-decode-int-overflow-guard.md) | medium | 1 | Tier-1 C binary |
| T1C-03 | [Reconcile FORMAT.txt path-safety claims; reject NUL-in-name](T1C-03-format-path-safety-reconcile.md) | medium | 1 | Tier-1 C binary |
| T1C-05 | [Fail loud when an index file exceeds the decompress cap](T1C-05-decompress-cap-fail-loud.md) | low | 0.5 | Tier-1 C binary |
| BOOT-06 | [lcsas-init probes only optical device nodes — USB boot dead-ends at a shell](BOOT-06-init-optical-only-device-scan.md) | medium | 0.5 | Boot / live-distro |
| BOOT-07 | [Remove the Alpine live stack; one (or zero) boot implementations, all artifacts pinned](BOOT-07-remove-alpine-live-stack.md) | medium | 1.5 | Boot / live-distro |
| BOOT-08 | [Quarantine tripwire + boot-gate spec — no boot claim without a boot test](BOOT-08-boot-path-quarantine-tripwire-gate.md) | medium | 1 | Boot / live-distro |
| FUP-03 | [Disc confidentiality — threat model for a stolen disc, passphrase reality, no-rotation story](FUP-03-disc-confidentiality-threat-model.md) | medium | 2.5 | Follow-up areas |

## Suggested order of attack

1. **INFRA-01 + UX-02's docs-vs-reality gate** — scaffolding and the cheapest tripwire; everything restore.bat-related develops on top.
2. **BURN-01..05 + FMA-01..02** — the catalog-integrity family; do these before burning another real disc.
3. **UX-01 (on INFRA-01) + KEY-01..03** — unbreak the Windows journey and the share-card path.
4. **UX-03/BOOT-01..02** — kill the boot dead-end routing.
5. **GATE plans** — wire the local suites into CI so none of the above regresses.
6. Remaining P1, then P2 in any order; FUP areas as a follow-up audit round.
