# Archived audit plans — deep audit 2026-06 (RESOLVED)

All 82 remediation plans from the 2026-06 deep audit have **landed in master**.
Each plan below carries a `STATUS: RESOLVED` stamp citing its commit and guarding test.

Two plans (FUP-03, BOOT-07) are RESOLVED but remain under `plans/audit-2026-06/` because
shipped artifacts link to them by path (`docs/DISC_CONFIDENTIALITY.md`,
`tests/recovery_hardening/test_no_unpinned_boot_artifacts.py`); moving them would break those references.

**82 resolved plans** (80 archived here + 2 kept in place).

| ID | Resolving commit | Subject | Location |
|----|------------------|---------|----------|
| BOOT-01 | `cecba69` | test: accept root-parser flags before the subcommand in doc gate [BOOT-01] | plans/done/ |
| BOOT-02 | `023e697` | recovery: hard-fail initramfs build on missing sources [BOOT-02] | plans/done/ |
| BOOT-03 | `9579623` | boot: align menu-entry paths with the staged tree; drop phantom FreeBSD entries [BOOT-03] | plans/done/ |
| BOOT-04 | `17cf25d` | boot+test: gate the live-USB no-OS path -- doc pins + Secure-Boot QEMU e2e [BOOT-04] | plans/done/ |
| BOOT-05 | `1a22c2c` | recovery: warn-and-confirm on RAM-backed (tmpfs) restore targets [BOOT-05] | plans/done/ |
| BOOT-06 | `fc3ea83` | boot+recovery: record lcsas-init optical-only scan defect; spec sentinel fix [BOOT-06] | plans/done/ |
| BOOT-07 | `309ee99` | meta+boot: delete the Alpine live stack; quarantine BootableISOBuilder; pin-or-fail guard [BOOT-07] | plans/audit-2026-06/ (kept in place — referenced) |
| BOOT-08 | `ab641d8` | boot+test: quarantine tripwire + QEMU boot-smoke revival spec [BOOT-08] | plans/done/ |
| BURN-01 | `9e2811b` | burn: fail loud when a repo mirror is missing at stage time [BURN-01] | plans/done/ |
| BURN-02 | `a0d034d` | burn: hash-verify every staged pack against its catalog SHA-256 [BURN-02] | plans/done/ |
| BURN-03 | `6f83722` | burn: compensate failed stages; guard clean_session; add session abort [BURN-03] | plans/done/ |
| BURN-04 | `16e0cc9` | burn+db+cli: VERIFIED requires device read-back matching the ISO SHA-256 [BURN-04] | plans/done/ |
| BURN-05 | `4c74bde` | burn+db+cli: failed verify records no copy; session ends PARTIAL [BURN-05] | plans/done/ |
| BURN-06 | `bab7442` | burn: retain session ISOs until clean; unbreak multi-location re-burn [BURN-06] | plans/done/ |
| BURN-07 | `67235bf` | ecc+burn: drop placebo RS03 -n; pre-flight budgets padded-medium size [BURN-07] | plans/done/ |
| BURN-08 | `66a0a4f` | burn+staging: durable receipts beside the catalog; disclose self-stale on-disc catalog [BURN-08] | plans/done/ |
| BURN-09 | `565c6a8` | fix(e2e): use MirrorScanResult.packs in e2e driver [BURN-09] | plans/done/ |
| BURN-10 | `5c82769` | db+cli: replica truth counts ACTIVE copies, not volume status [BURN-10] | plans/done/ |
| FMA-01 | `12cd469` | db+burn+cli: archived requires a burned volume; reclaim ghost volumes [FMA-01] | plans/done/ |
| FMA-02 | `d03b7f6` | db+cli: ensure_schema choke point — auto-migrate, refuse future schemas [FMA-02] | plans/done/ |
| FMA-03 | `4795d20` | iso+burn+db+cli: verify checks disc identity; evidence survives rebuild [FMA-03] | plans/done/ |
| FMA-04 | `cc2f893` | db+test: volume-copy UPSERT never destroys verification evidence [FMA-04] | plans/done/ |
| FMA-05 | `c871280` | cli+db+docs: batch disc re-verification stamps last_verified_at [FMA-05] | plans/done/ |
| FMA-06 | `412910a` | db+docs: recency-aware catalog rebuild; never resurrect destroyed volumes [FMA-06] | plans/done/ |
| FMA-07 | `4d0e5bd` | db+docs: crash-atomic migrations; refuse wedged catalogs [FMA-07] | plans/done/ |
| FMA-08 | `61d7ae3` | db+cli+docs: blast-radius reporting — status --redundancy + volume impact [FMA-08] | plans/done/ |
| FMA-09 | `647db88` | restore+recovery: free-space preflight before any restore prompt [FMA-09] | plans/done/ |
| FMA-10 | `249f8f8` | db+docs: rebuild keeps burn provenance; document newest-session STAGING gap [FMA-10] | plans/done/ |
| FMT-01 | `c2c3ee8` | recovery: regenerate 6 lcsas-ecc bins with the augment encoder [FMT-01] | plans/done/ |
| FMT-02 | `d8e87a2` | meta+docs+recovery: complete RS03 spec; pin+bundle dvdisaster source [FMT-02] | plans/done/ |
| FMT-03 | `587ab1a` | burn+config+ci: gate burns on pinned-reader format proof [FMT-03] | plans/done/ |
| FMT-04 | `12a5669` | iso+binpack+docs: reject ≥4 GiB files at ISO mastering [FMT-04] | plans/done/ |
| FMT-05 | `601c687` | recovery+db: tier-1 catalog schema-skew contract; freeze v5 query surface [FMT-05] | plans/done/ |
| FMT-06 | `54f20bf` | docs+tests: correct inflated ~30% ECC repair-capacity claims [FMT-06] | plans/done/ |
| FMT-07 | `d496aff` | docs+staging: temper M-DISC claims, disclose BDXL drive requirement [FMT-07] | plans/done/ |
| FUP-01 | `ba1f735` | burn+cli+iso: operator burn protocol — swap prompts, blank-check, eject, per-disc receipts [FUP-01] | plans/done/ |
| FUP-02 | `9e5b9ab` | db+cli+burn: catalog concurrency mitigations — staging-clean TOCTOU, loud lock waits, read-only safety [FUP-02] | plans/done/ |
| FUP-03 | `f42775f` | docs+cli+staging: stolen-disc threat model + honest key-on-disc wording [FUP-03] | plans/audit-2026-06/ (kept in place — referenced) |
| GATE-01 | `6be886e` | recovery+ci: commit x86_64-macos tier-1 binary; gate git-tracked status [GATE-01] | plans/done/ |
| GATE-02 | `bf9d338` | ci+tests: run recovery-hardening suite in CI with skip-rot floor [GATE-02] | plans/done/ |
| GATE-03 | `2531a86` | ci+tests: execute Mach-O tier-1 binaries on macOS runners [GATE-03] | plans/done/ |
| GATE-04 | `e0cdd1e` | ci+tests: broaden audit-gate path filter + always-on C smoke [GATE-04] | plans/done/ |
| GATE-05 | `2fe775d` | ci+docs: run RS03 ECC repair proof weekly in CI [GATE-05] | plans/done/ |
| GATE-06 | `445d912` | test(blind): promote tier1-missing out of the XFAIL ledger [GATE-06] | plans/done/ |
| GATE-07 | `81140c9` | ci+recovery: fail-closed coverage check, CI threshold to measured floor [GATE-07] | plans/done/ |
| GATE-08 | `c038291` | recovery+ci: rebuild-and-diff gate for committed bin artifacts [GATE-08] | plans/done/ |
| GATE-09 | `23971b1` | ci+tests: enforce restore.sh shell-coverage floor; wire into gate + CI [GATE-09] | plans/done/ |
| GATE-10 | `c977b82` | tests+ci: make e2e pipeline test portable via LCSAS_E2E_BASE [GATE-10] | plans/done/ |
| INFRA-01 | `f52f0da` | tests: tolerate missing upstream recovery cache in windows fixture [INFRA-01] | plans/done/ |
| KEY-01 | `b5fd7b2` | keyshare: accept real share-card files in all three combiners [KEY-01] | plans/done/ |
| KEY-02 | `33eb565` | recovery: real --target/--key flags in restore.sh + doc-flag contract gate [KEY-02] | plans/done/ |
| KEY-03 | `a8b2091` | key+cli+docs: verify-at-split + lcsas key verify drill [KEY-03] | plans/done/ |
| KEY-04 | `5e463f3` | test(blind): keep split-docs prompt spoon-feed-free [KEY-04] | plans/done/ |
| KEY-05 | `e310881` | docs+meta: lcsas-keyshare is the primary combiner in all heir docs [KEY-05] | plans/done/ |
| KEY-06 | `704686c` | recovery+ci: bring lcsas-keyshare under coverage/fuzz/audit gates [KEY-06] | plans/done/ |
| KEY-07 | `a5d6280` | keyshare+cli+docs: per-share typo diagnostics + 4-letter prefix entry [KEY-07] | plans/done/ |
| KEY-08 | `cc5d390` | db+cli+staging: record key split state; fail burn on escrow drift [KEY-08] | plans/done/ |
| KEY-09 | `9b0b3df` | cli+docs: ship single-key Recovery Card generator + template [KEY-09] | plans/done/ |
| KEY-10 | `9232815` | docs+test: truthful wordlist provenance with doc-embedded hash [KEY-10] | plans/done/ |
| RST-01 | `04b68a3` | cli: prune recovered packs against cache before failing restore [RST-01] | plans/done/ |
| RST-02 | `f844930` | restore: gate blob decompression on index uncompressed_length, not magic sniff [RST-02] | plans/done/ |
| RST-03 | `79b1392` | restore: tolerant tier-3 traversal — skip bad blobs, write failure manifest, atomic writes [RST-03] | plans/done/ |
| RST-04 | `b6d3a37` | test(restore): cover tier-3 no-zstandard import wiring [RST-04] | plans/done/ |
| RST-05 | `0d37af0` | meta+cli: required-contents completeness gate at build and verify [RST-05] | plans/done/ |
| RST-06 | `0aafd27` | restore: fix dead symlink-escape guard in tier-3 [RST-06] | plans/done/ |
| RST-07 | `68c1079` | cli: route skipped discs into alternates retry; name volume labels in errors [RST-07] | plans/done/ |
| RST-08 | `64b5bde` | restore: share hardlink map across tier-3 tree traversal [RST-08] | plans/done/ |
| RST-09 | `6ee4717` | restore: lower standalone restorer to true Python 3.10 floor [RST-09] | plans/done/ |
| T1C-01 | `1e6a726` | recovery: size-adaptive tier-1 JSON parsing; never skip an index silently [T1C-01] | plans/done/ |
| T1C-02 | `fe506a2` | recovery: guard json decode_int against signed overflow; fuzz NUMBER tokens [T1C-02] | plans/done/ |
| T1C-03 | `9e99a94` | recovery: reject embedded-NUL path names; reconcile FORMAT.txt symlink claim [T1C-03] | plans/done/ |
| T1C-04 | `d2dbfa1` | recovery: depth-cap tree walk + fuzz lcsas_tree_restore [T1C-04] | plans/done/ |
| T1C-05 | `bb48bfe` | recovery: fail loud on over-cap/corrupt index in tier-1 load_index [T1C-05] | plans/done/ |
| UX-01 | `387eb10` | test: match restore.bat's improved missing-backup-set message [UX-01] | plans/done/ |
| UX-02 | `e6c7e48` | docs+test: bundled-python combiner fallback + generic doc-flag gates [UX-02] | plans/done/ |
| UX-03 | `602e4f9` | docs+meta: truthful no-OS routing; meta-build flag contract gate [UX-03] | plans/done/ |
| UX-04 | `7e3bb94` | docs(recovery): sweep RECOVER_WINDOWS.txt paths + restorer commands [UX-04] | plans/done/ |
| UX-05 | `af52bde` | meta: one START_HERE generator for both build variants [UX-05] | plans/done/ |
| UX-06 | `5289f58` | recovery: --help QUICK START starts from the META disc [UX-06] | plans/done/ |
| UX-07 | `c46fd4d` | test: cover restore.sh target-guard + tmpfs-warning paths [UX-07] | plans/done/ |
| UX-08 | `0241ea4` | recovery: pin test_restore_bat_e2e.sh; refresh stale doc manifest hashes [UX-08] | plans/done/ |
| UX-09 | `fbf529f` | cli+docs: ship whole-archive `lcsas estate card` generator [UX-09] | plans/done/ |
