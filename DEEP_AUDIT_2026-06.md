# LCSAS Deep Audit — 2026-06-10

**Goal judged against:** a non-technical heir, decades from now, holding a box of discs, must be able to restore the data with no outside help. Everything below is scored against that bar, not against what a developer could work around.

**Method.** Multi-agent audit: 9 dimension auditors (each deep-reading one subsystem), every finding then adversarially re-verified against the actual code and the full test/gate surface by an independent verifier instructed to refute it, plus a completeness critic. **80 findings raised → 78 confirmed (6 critical / 33 high / 32 medium / 7 low), 2 refuted.** 40 of the 78 confirmed findings appear in **no existing known-issues ledger** (UX_CONCERNS, DEFERRED_WORK, AUDIT_FINDINGS, READINESS_CHECKLIST). Several were reproduced empirically, not just read (share-card rejection, zero-byte initramfs, combiner failures).

---

## Executive summary

The headline is an inversion of where the hardening effort has gone: **the recovery side is the strong half of this system; the burn side and the last mile of the heir journey are the weak half.** The tier-1 C binary's crypto is verified sound (Poly1305-MAC + SHA-256 before any plaintext reaches the target — corrupt data is rejected, never silently restored), the SLIP-0039 escrow core passes official vectors and fails closed, the planner is alternates-aware, and the blind-restore drill concept is genuinely ahead of normal practice. That work is real and it shows.

Five themes cover nearly all 78 confirmed findings:

**1. The catalog can lie about data being safe.** Three criticals + several highs share one root cause: *"archived" is recorded at staging-commit, and "VERIFIED" never compares content.* A pack counts as archived the moment a `volume_packs` row exists — before any burn, with no content hash anywhere in the pipeline (the pack's *filename* is trusted as its SHA-256), and post-burn "verification" is a readability smoke test that never checks the physical disc against the recorded ISO hash. Concrete consequences confirmed in code: an unmounted NAS mirror at stage time produces discs **without their packs** that still reach VERIFIED; a crash or `stage --clean` on an unburned session permanently strands packs the catalog forever reports as archived; a **failed** verify still records an ACTIVE copy. The owner can run this system for years, see green everywhere, and be holding nothing.

**2. The heir journey works on exactly one path: Linux, working computer, single key.** Every other advertised path breaks at step 1 or step 2, and every break is a doc/code contract drift that no gate can currently catch: `restore.bat` probes a repo layout that no built meta-volume has ever had (the Windows journey — statistically the most likely one — dead-ends at the first double-click); the printed share **cards** that holders will actually possess are rejected by *all three* combiners (reproduced empirically — only the bare mnemonic file works, and the tests deliberately filter card files out); the on-disc split-key instructions reference `restore.sh --target/--key` flags that don't exist; `RECOVER_WINDOWS.txt` cites wrong binary paths throughout; and the no-OS branch routes heirs to booting a disc that is not bootable (theme 4).

**3. The 15% ECC parity on every disc is a shield nothing shipped can raise.** Every disc pays RS03 parity, but no LCSAS tooling can *spend* it at restore time: tier-1 has zero RS03 code, restore.sh/restore.bat never mention repair, dvdisaster is bundled only opportunistically (build-host arch, dynamically linked, unpinned, upstream abandoned), and the on-disc format doc admits it isn't sufficient for re-implementation. The disc-integrity layer is therefore *reject-only* for a non-technical user: damage is detected and refused, but the paid-for repair path requires an expert with a 2026-era Linux x86_64 dvdisaster.

**4. The bootable meta-volume is fiction, and on-disc docs route heirs into it.** No kernel, busybox, or initramfs artifact exists or has ever been built; the documented `--recovery-boot` flag doesn't exist; the build script ships zero-byte placeholders and exits 0; the designed flow would restore into a 256 MB RAM tmpfs that evaporates on power-off. Meanwhile START_HERE.txt tells heirs with no working computer to "boot directly from the disc."

**5. The deep gate surface mostly doesn't run.** CI runs unit+integration only. The 58-file recovery-hardening suite ("the final gate that says this build is shippable"), e2e, shell-coverage, every blind-restore drill, and the ECC repair proof are local-only or opt-in; the audit-gate coverage threshold in CI (60) is 28 points below the documented floor (88) with a fail-open checker; the Intel-Mac tier-1 binary is gitignored and absent (fresh clones silently build 5/6-target meta discs); both macOS binaries have never been executed by any test anywhere; the tier-2-fallback blind variant is permanently XFAIL.

### The three questions you asked

**Should we build new e2e tests? — Yes; it is the single highest-leverage investment in the whole report.** Nearly every critical/high above is a *contract* break between components (docs↔scripts, catalog↔discs, builder↔restore.bat) that unit tests structurally cannot catch and that one journey-level test catches instantly. The five to build first, in order:

1. **Windows journey e2e** (GitHub `windows-latest` runner, no optical hardware needed): build a real meta tree with `lcsas meta build`, run `restore.bat` against it as a directory, assert a snapshot restores. Today this fails at repo discovery — and nothing can catch it.
2. **Heir-docs-vs-reality contract test** (always-on unit test): extract every command from every *generated/burned* doc (START_HERE, KEY_INFO, RECOVER*.txt, BOOT.txt, --help texts) and assert each referenced flag/subcommand exists in the corresponding parser. Catches three confirmed findings at once and prevents the whole class forever.
3. **Share-card round trip** (always-on): `lcsas key split` → feed the actual `-card.txt` artifacts (not the bare share files) through all three combiners (Python module, CLI, C binary) → assert byte-exact password recovery, including with typos rejected helpfully.
4. **Burn-pipeline fault-injection e2e**: unmount the mirror mid-pipeline, kill xorriso/dvdisaster mid-run, `stage --clean` an unburned session — assert the catalog never reports archived/VERIFIED/ACTIVE for data not durably on a verified disc, and that stranded packs are surfaced and reclaimable.
5. **Bundled-tools-only damaged-disc e2e**: corrupt an ECC-augmented image below threshold, then repair **using only what the meta-volume bundles** (this is the gate that forces the RS03 decision in question 3; it fails today by construction).

Plus: promote the cdemu blind-restore to a weekly scheduled CI job (loopback-mount variant if cdemu won't run on hosted runners), and remove the permanent XFAIL on the tier1-missing variant.

**Drop or improve the live distro? — Drop it, and replace the no-OS story.** It isn't a method today — it's scaffolding that has never produced a bootable artifact, with internally inconsistent configs, a phantom CLI flag, a zero-byte-busybox build script, and a RAM-tmpfs restore target that loses everything on power-off. Aging it forward (Secure Boot signing, ARM/Apple Silicon, future chipset drivers, kernel maintenance forever) is a permanent OS-vendor-sized liability that the 6-target host-OS tier-1 path gets for free by riding contemporary OSes. Replace with: (a) rewrite the no-OS branch of RECOVER.txt/START_HERE → "use any other computer, or boot any current live-Linux USB (they are Secure-Boot signed and have current drivers), then run restore.sh from the disc"; (b) a tested *live-USB walkthrough* doc with the two gate tests from the boot findings; (c) move `recovery/boot/` + `meta/live` to `experimental/`, fix README.txt's false "Phase 2 COMPLETE" claim. If you ever revive boot, the QEMU/OVMF smoke gate is specified in the boot findings below.

**Different format? — Keep the stack; change one posture (RS03 repair) and add four guards.** Per-bet verdicts from the format dimension, all confirmed: **ISO9660+Rock Ridge+Joliet: keep** (two independent reader implementations exist; add a >4 GiB-file guard for the Windows native-mount path). **restic v1/v2 repo format: keep** — it is the *best-hedged* bet in the system (full on-disc spec + two independent re-implementations); add the missing writer-drift gate (live rustic writes ↔ pinned tier-1/tier-3 readers, asserted in CI on every rustic version bump). **SQLite: keep** (vendored amalgamation, statically linked); add a schema-skew contract test for tier-1 reading future schema versions. **zstd: keep** (fully vendored decoder). **RS03: keep the parity format for the burned back-catalog, but bring repair in-house** — either vendor + pin dvdisaster source and ship static binaries for all 6 targets wired into a guided restore.sh/bat flow, or implement the RS03 decoder in the tier-1 C codebase (the parity math is Reed-Solomon over known geometry; the blocker the on-disc doc admits is *documentation*, which in-housing fixes). Gate it with e2e test #5 above. **Media guidance: temper** M-DISC "1000-year" vendor claims and document the BDXL-reader-availability hedge. PAR2 sidecars are worth considering **only** if RS03 in-housing proves infeasible — they'd be a second parity system to carry, not a free upgrade.

### The 6 criticals at a glance

1. **(burn-pipeline)** Missing/unmounted mirror at stage time silently burns volumes WITHOUT their packs while the catalog marks them archived
2. **(burn-pipeline)** Pack content is never hash-verified anywhere in the burn pipeline — bit-rotted mirror data is faithfully burned, ECC-protected, and 'VERIFIED'
3. **(burn-pipeline)** Packs are permanently 'claimed' at staging-commit; ISO/ECC failure mid-stage, crash, or `stage --clean` on an unburned session silently excludes them from all future burns
4. **(ux-journey)** restore.bat cannot discover the repository on a real meta-volume — the entire advertised Windows journey dead-ends at step 1
5. **(failure-modes)** Packs linked to a never-burned STAGING volume are counted 'archived' forever; cleaning an unburned session silently strands data on the NAS
6. **(format-durability)** RS03 ECC repair is expert-only: the repair half of the disc-integrity layer is outside the bare-minimum path and depends on an abandoned, unpinned, host-arch-only dvdisaster binary

---

## Prioritized roadmap

**P0 — stop the catalog lying; unbreak the heir paths (do before the next real burn):**
1. Fail loud on unavailable mirror at stage time; never burn a volume missing selected packs (burn-pipeline #1).
2. Redefine "archived" to require a VERIFIED burn (or at minimum: surface + reclaim packs claimed by never-burned volumes; guard `stage --clean`) (burn-pipeline #3, failure-modes #1).
3. Hash pack content mirror→staging, and make post-burn verify compare the physical disc against the recorded ISO SHA-256; a failed verify must never record an ACTIVE copy (burn-pipeline #2, #4, #5).
4. Fix `restore.bat` repo discovery for the real holographic layout + Windows e2e on CI (ux-journey #1).
5. Make all three share combiners accept real card files; verify-at-split (recombine + unlock repo); add `lcsas key verify` for the annual drill (keys-escrow #1, #3).
6. Fix every phantom-flag/wrong-path instruction in generated on-disc docs + add the docs-vs-reality contract gate (ux-journey, keys-escrow #2).
7. Rewrite the no-OS branch; demote the boot path (boot #1, #2).

**P1 — make the gates real:**
8. Wire recovery-hardening + e2e + shell-coverage into CI; fix fail-open coverage checker; raise CI threshold 60→88; close audit-gate path-filter holes (vendored C, keyshare, scripts).
9. Commit/build the Intel-Mac binary; add macOS runners that *execute* both tier-1 mac binaries; binary-staleness gate (rebuild from source, compare).
10. Weekly scheduled CI: ECC-repair proof, blind restore (loopback variant), boot-docs parity; un-XFAIL tier1-missing.
11. RS03 repair in-housing decision + bundled-tools-only repair e2e.
12. Writer-drift gate (rustic bump ↔ pinned readers); tier-1 JSON token-cap fix (silent file drops) + fuzz the tree walk.

**P2 — hardening & polish:** remaining mediums/lows per dimension below, the three follow-up audit areas in Appendix C (operator burn protocol, catalog concurrency, disc-confidentiality threat model — note the last one: every disc ships the rustic `keys/` dir + plaintext backup topology, enabling unlimited offline brute-force from any single escrowed disc), and the periodic disc-rot re-verification story (failure-modes).

---

## Reading the findings

Each finding below: corrected severity, CONFIRMED/REFUTED with verifier confidence, whether it was already tracked in a ledger, the gap, file:line evidence, what the independent verifier checked, the fix, and the concrete tests/gates to add. Refuted findings are kept (struck through) for honesty. Dimensions appear in priority order.

---

## Burn pipeline as a data-loss surface  `burn-pipeline`

> The burn pipeline's catalog bookkeeping is optimistic in ways that directly contradict the heir-proof goal: packs are marked archived at staging-commit (not at verified burn), pack content is never hash-verified between the rustic mirror and the disc, and post-burn "verification" is a readability smoke test that never compares the physical disc to the recorded ISO SHA-256 — so the catalog can durably claim data is safe on discs that are missing, corrupt, or were never burned at all. The two strongest untracked data-loss windows are the silent skip of an unmounted mirror during staging (volume burns and VERIFIES without its packs) and the family of failure paths (mid-stage ISO failure, crash, stage --clean on an unburned session) that permanently exclude packs from all future burns with no surfacing or reclamation tool. Multi-copy bookkeeping is also dishonest at the margins: a failed verify still records an ACTIVE copy at the location, the in-tool multi-location re-burn flow is broken because the ISO is deleted after the first burn, and destroyed copies never demote volume-level redundancy math. None of these are tracked in the existing ledgers, which focus almost entirely on the C tier-1 recovery path; the unit suite mocks or skip_burns past every one of these windows.

#### 1. [CRITICAL] Missing/unmounted mirror at stage time silently burns volumes WITHOUT their packs while the catalog marks them archived

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** In _stage_single_volume, packs whose repo mirror is unavailable are silently skipped: `if mirror_path is None: continue` and `if data_dir.is_dir(): builder.stage_packs(...)` — there is no else-branch error. The very next step still runs `bulk_link_packs(...)` for ALL selected packs, commits, injects the catalog, masters the ISO and burns it. stage_packs' MissingPacksError guard is never invoked because stage_packs is never called. Trigger is mundane: NAS mirror unmounted (or one repo's mirror_path moved) between `lcsas scan` and `lcsas stage`. Result: a verified disc that physically lacks the packs, a catalog that says they are archived forever (get_unarchived_packs only checks volume_packs existence), and an heir whose restore hits 'pack not found' decades later when the hot mirror no longer exists. Size pre-flight only checks too-LARGE, never too-small vs used_bytes.

**Evidence.** src/lcsas/burn/orchestrator.py:374-380 (`if mirror_path is None: continue` ... `if data_dir.is_dir(): builder.stage_packs(repo_packs, data_dir)`); orchestrator.py:398-401 (bulk_link_packs + commit regardless of staged count); staging/builder.py:180-181 (MissingPacksError raised only inside stage_packs); db/queries.py:30-47 (archived == any volume_packs row); staging/metadata.py:106-115 (inject_metadata also silently skips missing metadata dirs). No test covers this: grep of tests/unit/test_burn_orchestrator.py and test_session_pipeline.py shows MissingPacksError tested only via direct StagingBuilder calls (tests/unit/test_staging.py:72,91), never through the orchestrator with a missing data dir.

**Verifier.** Confirmed at orchestrator.py:374-380: `if mirror_path is None: continue` and `if data_dir.is_dir(): builder.stage_packs(...)` with no else; bulk_link_packs(398-401) then commits ALL selected packs. stage_packs' MissingPacksError never fires when the dir is absent. Only mitigation found: load_config logs a WARNING for missing config mirror_path (settings.py:188-198) — non-fatal, pipeline proceeds. No post-stage count/size invariant; ISO pre-flight checks too-large only. No orchestrator-level test exists (MissingPacksError tested only via direct StagingBuilder calls in test_staging.py/test_filesystem_failures.py).

**Fix.** In _stage_single_volume, treat an absent mirror/data dir for any repo with selected packs as fatal: raise MissingPacksError listing every pack of that repo before any DB write. Additionally add a post-stage invariant: count and sum staged files under staging_root/data and assert they equal len(selected_packs)/total_bytes before create_volume/bulk_link_packs commit.

**Tests / gates to add:**
- tests/unit/test_burn_orchestrator.py::test_stage_raises_when_repo_data_dir_missing — register 2 repos, delete one repo's data/ dir after seeding the catalog, call orch.stage(); assert it raises and that NO volume row or volume_packs rows were committed (get_unarchived_packs still returns the packs).
- tests/unit/test_burn_orchestrator.py::test_stage_asserts_staged_count_equals_selected — monkeypatch StagingBuilder.stage_packs to stage only N-1 packs without raising; assert the orchestrator fails loud before bulk_link_packs.
- Always-run CI (make test-unit already in .github/workflows/test.yml) — these are pure-unit, no opt-in flag.

#### 2. [CRITICAL] Pack content is never hash-verified anywhere in the burn pipeline — bit-rotted mirror data is faithfully burned, ECC-protected, and 'VERIFIED'

**CONFIRMED** (verifier confidence: high) — tracked: Partially mitigated operationally by recovery/docs/READINESS_CHECKLIST.txt 'MONTHLY TEST-RESTORE' (only exercises packs of the latest snapshot) — the code-level gap itself is untracked.

**Gap.** The whole pipeline trusts the rustic pack FILENAME as its SHA-256. The scanner registers name+size only (never hashes); staging hash-checks ONLY a pre-existing destination file and only if <=500 MB (fresh hardlinks are never hashed; >500 MB files are size-checked only); `catalog validate` (db/verify.py) compares pack NAMES on disc to the catalog, not content; post-burn verify is a readability check. So a pack corrupted on the NAS mirror after rustic wrote it (bit rot, rsync truncation, fs corruption) is burned identically to every copy at every location, RS03 dutifully protects the corrupt bytes, the volume is marked VERIFIED, and the corruption is only discovered when tier-1's Poly1305/SHA-256 gate REJECTS the blob at restore time — decades later, when the mirror is gone and no good copy exists. docs/architecture.md:356 claims 'Post-burn: read-back the entire disc and verify SHA-256 of every pack' — this is not implemented anywhere.

**Evidence.** src/lcsas/packs/scanner.py:13,23-34 (registers `_PACK_NAME_RE`-matching names + st_size; no hashing); src/lcsas/staging/builder.py:113-145 (hash check only when `dst.exists()` and `dst_size <= 500_000_000`), builder.py:155-176 (fresh hardlink path: only zero-size check); src/lcsas/db/verify.py:39-61 (_collect_disc_packs: 'A valid SHA-256 is 64 hex characters' — name match only); src/lcsas/iso/xorriso.py:307-325 (verify_disc = `-check_media` returncode); docs/architecture.md:356 ('read-back the entire disc and verify SHA-256 of every pack' — unimplemented).

**Existing partial coverage.** Partial/operational only: READINESS_CHECKLIST monthly test-restore (manual, latest snapshot only); no CI gate

**Verifier.** All evidence confirmed: scanner.py registers filename+size only; builder.py:113-145 hashes ONLY a pre-existing dst and only ≤500MB (fresh hardlinks never read; >500MB size-only); db/verify.py matches 64-hex names; verify_disc is `-check_media` returncode. architecture.md:356 claims post-burn per-pack SHA-256 read-back — grep finds no implementation. Corruption propagates identically to every copy (same mirror file feeds all ISOs); tier-1 rejects (not heals) at restore. Only operational mitigation is the opt-in monthly test-restore of the latest snapshot.

**Fix.** Add a mandatory content-verification pass before ISO mastering: sha256 every staged pack against its catalog hash (cheap relative to the burn; it is a one-time read of data already being read by xorriso) and fail the stage on any mismatch, marking the pack QUARANTINED in the catalog. Remove the 500 MB hash-skip. Extend `lcsas verify --disc` / `catalog validate` with an optional --content mode that hashes packs off a mounted disc.

**Tests / gates to add:**
- tests/unit/test_staging.py::test_stage_rejects_pack_with_content_not_matching_name — write a pack file whose bytes do not hash to its filename; assert orch.stage()/stage_packs fails loud.
- tests/unit/test_staging.py::test_large_pack_is_hash_verified — >threshold file with corrupted content must not be silently accepted (kills the 500 MB skip).
- tests/integration/test_catalog_validate_content.py — master a real ISO with one corrupted pack, mount/extract, assert `lcsas catalog validate --content` exits non-zero naming the pack.
- Doc gate: tests/unit/test_docs_claims.py asserting architecture.md's verification claims match an implemented code path (or fix the doc).

#### 3. [CRITICAL] Packs are permanently 'claimed' at staging-commit; ISO/ECC failure mid-stage, crash, or `stage --clean` on an unburned session silently excludes them from all future burns

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** A pack counts as archived the moment a volume_packs row exists (volume status irrelevant). Three paths strand packs on phantom STAGING volumes with no reclamation tool: (a) in _stage_single_volume the compensating delete_volume covers ONLY catalog-injection failure — if xorriso/dvdisaster fails afterwards (lines 446-469) the committed volume + links survive; (b) clean_session deletes ISOs and the staging tree for ANY session, including never-burned ones, without deleting volumes or unlinking packs — `lcsas stage --clean` on the wrong session destroys the only stageable artifacts while the catalog keeps claiming the packs are archived; (c) crash between the per-volume commit (line 401) and add_session_volume (line 637) leaves a volume outside session_volumes that burn_session can never burn. In every case future `lcsas stage` skips those packs (get_unarchived_packs), the redundancy report counts the STAGING volume as a copy, and restore pick lists send the heir to a disc that was never burned (status filter only excludes DEPRECATED/DESTROYED).

**Evidence.** src/lcsas/burn/orchestrator.py:398-420 (commit then try/except wraps only wal_checkpoint+inject_catalog; delete_volume at 416 only in that handler), 446-469 (ISO creation + post-checks raise with no compensation), 813-830 (clean_session: unlinks ISOs, removes staging dir, sets CLEANED — no volume/pack rollback, no status guard); cli/main.py:1025-1029 (`if args.clean: orch.clean_session(session_ref)` unconditional); db/queries.py:30-47 (archived == any volume_packs row), 168-169/181-182 (pick list includes STAGING volumes), 405-417 (redundancy report counts non-DEPRECATED/DESTROYED volumes, so STAGING counts). Tests assert only the raise and CLEANED status, never pack reclamation: tests/unit/test_session_pipeline.py:627-645, 525-553.

**Verifier.** All three paths confirmed: (a) commit at orchestrator.py:401; delete_volume(416) compensates only catalog-injection failure — ISO/ECC failures at 446-469 raise with committed volume_packs intact; stage() adds no compensation; (b) clean_session(813-830) and cli/main.py ~1025 unconditionally delete ISOs/staging for any session, no STAGING-volume guard, no pack reclamation; (c) commit(401)→add_session_volume(637) crash window real. get_unarchived_packs excludes any volume_packs row; pick lists (queries.py:169,182) and redundancy report (410) count STAGING volumes. Only reclamation is legacy abort() for the manifest path — not wired to sessions/CLI.

**Fix.** Make pack-claiming transactional with successful ISO creation: move bulk_link_packs commit after the ISO+ECC succeed, or add compensating delete_volume to ALL failure paths in _stage_single_volume. Make clean_session refuse (without --force) to clean a session containing volumes still in STAGING/BURNING, and when forced, delete those volumes + links so packs return to the unarchived pool. Add `lcsas session abort <id>` that deletes un-burned volumes and reclaims packs.

**Tests / gates to add:**
- tests/unit/test_session_pipeline.py::test_iso_failure_midstage_reclaims_packs — mock xorriso.create_iso to fail on volume 2 of 2; assert get_unarchived_packs() afterwards contains volume-2's packs and no orphan STAGING volume remains.
- tests/unit/test_session_pipeline.py::test_clean_session_unburned_refuses_or_reclaims — stage, then clean_session without burning; assert either ValueError or that all packs are unarchived again and volumes deleted.
- tests/unit/test_db_queries.py::test_pick_list_excludes_staging_volumes (or a doctor command test) — a pack only on a STAGING volume must not be presented to the restore planner as available.
- New `lcsas doctor` invariant check + tests/unit/test_doctor.py: flags volume_packs rows on STAGING volumes older than N days; wire into make gate.

#### 4. [HIGH] Post-burn 'verification' never compares the physical disc to the recorded ISO SHA-256 — VERIFIED is granted on a readability smoke test

**CONFIRMED** (verifier confidence: high) — tracked: docs/architecture.md:356 documents the intended read-back-SHA-256 behavior, but no ledger tracks that it is unimplemented.

**Gap.** verify_disc runs `xorriso -indev <dev> -check_media` and returns returncode==0. It does not compare disc content against the ISO, does not check the disc's volume label (any readable disc in the drive passes), and xorriso's exit code reflects event severity, not per-sector damage reports. Yet the pipeline computes and stores the post-ECC ISO SHA-256 (session_volumes.iso_sha256) at stage time and never uses it against the physical disc. VERIFIED — the catalog's strongest durability claim, and the gate check_deprecation_safe relies on — is therefore granted without ever proving the burned bytes match the mastered image. A drive that silently mis-burned or a wrong/leftover disc in the tray yields a VERIFIED volume whose data may be absent, and the heir discovers it only at restore.

**Evidence.** src/lcsas/iso/xorriso.py:307-325 (`-check_media`, `return result.returncode == 0`); src/lcsas/burn/orchestrator.py:624-626 (iso_hash computed and stored), 704-727 (verify_ok → VERIFIED based solely on verify_disc); cli/main.py:1713-1718 (`lcsas verify --disc` uses the same verify_disc); db/volumes.py:241-265 (deprecation safety trusts BURNED/VERIFIED status). The SHA-256 fallback compare exists only for ISO FILES (cli/main.py:1747-1757), never for the device. No test exercises a content-mismatched disc: tests mock verify_disc to True/False (tests/unit/test_session_pipeline.py:475,503).

**Verifier.** Confirmed: xorriso.py:307-325 returns `-check_media` returncode==0, no label or content check, so any readable disc passes. iso_sha256 stored in session_volumes at stage (orchestrator.py:624-637) and never compared against the device; burn_session's add_volume_copy(743-748) omits iso_sha256 → NULL on the copy row. SHA-256 fallback in cli/main.py applies only to ISO files, never devices. check_deprecation_safe trusts BURNED/VERIFIED status. Tests mock verify_disc to True/False only (test_session_pipeline.py 475/503).

**Fix.** After burn, read the device back (dd of the ISO's byte length from /dev/srX) and compare SHA-256 against sv.iso_sha256; only then grant VERIFIED, and write the hash into volume_copies.iso_sha256 (burn_session currently writes NULL there). Keep `-check_media` as a fast pre-pass. Record last_verified_at on the copy row.

**Tests / gates to add:**
- tests/unit/test_burn_orchestrator.py::test_verify_compares_device_hash — inject a fake device-reader returning bytes != ISO; assert volume stays BURNED and VERIFY_FAIL recorded with 'hash mismatch'.
- tests/e2e/cdemu_blind_restore: add a burn-side leg (CDEmu is available per project memory) that burns an ISO to a virtual device, flips one byte in the backing file, and asserts `lcsas verify --disc` fails — runnable in CI without hardware.
- tests/unit/test_burn_orchestrator.py::test_burn_session_writes_copy_iso_sha256 — assert volume_copies.iso_sha256 is populated (not NULL) after burn_session.

#### 5. [HIGH] A failed post-burn verify STILL records an ACTIVE volume copy at the location and marks the session COMPLETE — the bad disc permanently satisfies location-targeted staging

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** In burn_session, add_volume_copy(... location ...) executes unconditionally after the verify branch, even when verify_passed is False (the copy row's status defaults/upserts to 'ACTIVE'). The session is then set COMPLETE regardless. Consequences: get_unarchived_or_missing_at_location and get_packs_at_location treat the failed disc's packs as present at that location (they filter on vc.status='ACTIVE'), so `lcsas stage --for-location X` will never re-stage them for X; location summaries overstate redundancy; and the only trace of the failure is a VERIFY_FAIL event plus volume status BURNED that nothing downstream acts on. For a user building the offsite copy that is the heir's lifeline, one failed burn silently becomes a phantom copy.

**Evidence.** src/lcsas/burn/orchestrator.py:704-721 (verify_passed=False path records event only), 742-749 ('Record copy at location' — unconditional add_volume_copy + commit), 803 (update_session_status COMPLETE regardless); db/volume_copies.py:57-68 (UPSERT sets status='ACTIVE'); db/queries.py:454-463 and 479-488 (location queries require vc.status='ACTIVE' — a failed copy still matches). Test gap: tests/unit/test_session_pipeline.py:489-517 (test_burn_session_verify_fail_stays_burned) asserts status/events but never asserts no ACTIVE copy was recorded.

**Verifier.** Confirmed: add_volume_copy at orchestrator.py:742-749 runs unconditionally after the verify branch (VERIFY_FAIL only adds an event, doesn't raise); schema defaults status='ACTIVE' and the UPSERT explicitly sets status='ACTIVE' (volume_copies.py:64); update_session_status COMPLETE at line 803 regardless. get_unarchived_or_missing_at_location/get_packs_at_location filter vc.status='ACTIVE', so the failed disc satisfies location staging forever. test_burn_session_verify_fail_stays_burned (489-517) asserts status/event only, never copies.

**Fix.** Record the copy with status 'UNVERIFIED' (or skip recording) when verify fails, and exclude non-ACTIVE copies from location-satisfaction queries (already the case once status differs). Mark the session PARTIAL, not COMPLETE, when any receipt has verify_passed=False, and have `lcsas status`/location status surface UNVERIFIED copies.

**Tests / gates to add:**
- tests/unit/test_session_pipeline.py::test_verify_fail_does_not_create_active_copy — verify_disc→False; assert get_copies_for_volume(active_only=True) is empty and get_unarchived_or_missing_at_location(location) still returns the packs.
- tests/unit/test_session_pipeline.py::test_verify_fail_session_not_complete — assert session status != 'COMPLETE' when any volume failed verify.
- tests/unit/test_location_queries.py::test_failed_copy_does_not_satisfy_location.

#### 6. [HIGH] Multi-location re-burn of a session is broken in real operation: the ISO is deleted after the first verified burn, so the supported 'just add another copy' flow always raises FileNotFoundError

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** burn_session deletes each ISO after a successful verified burn ('Remove ISO after successful verified burn to free staging space'). But burn_session explicitly supports re-burning the same session at a second location (`is_reburn` branch: 'For multi-location re-burns, skip status transitions... just add another copy'), and its first action requires the ISO to exist, raising FileNotFoundError with the misleading hint 'Was the staging directory cleaned prematurely?'. So the in-tool path to the 2+ physical copies the durability model depends on (READINESS: 'the offsite copy is not a bonus — it is the archive') fails right after the first real burn succeeds; the user must re-stage everything from scratch (new volume labels per location, full ISO+ECC redo). The unit tests pass only because they use skip_burn=True, which skips both the deletion and the existence check — the real path is untested.

**Evidence.** src/lcsas/burn/orchestrator.py:789-800 (`if verify_passed and not skip_burn ... iso_path.unlink()`), 682-687 (`if not skip_burn and not iso_path.exists(): raise FileNotFoundError(... 'cleaned prematurely?')`), 690-697 (is_reburn multi-location support). Test gap: tests/unit/test_session_pipeline.py:393-413 (test_burn_session_multi_location uses skip_burn=True for both burns), 995-1016 (same).

**Verifier.** Confirmed: orchestrator.py:792-794 unlinks the ISO when verify_passed and not skip_burn; line 683-687 raises FileNotFoundError on the next burn_session. Worse than claimed: README Steps 4-5 (lines 245-266) and line 657-658 explicitly document `burn --session latest` to Home_Shelf then Offsite_Safe — 'Burns the same staged ISOs again. No re-staging' — so the documented primary multi-copy flow is broken. cmd_burn_session has no re-mastering path. Both multi-location tests (393-413, 995-1016) use skip_burn=True; the one skip_burn=False test burns a single location. Fails loud with a misleading hint; workaround is full re-stage --for-location.

**Fix.** Delete the ISO only when copies exist at >= config.min_copies locations (or behind an explicit `--free-space` flag / in clean_session), and make the FileNotFoundError message say the ISO was auto-deleted after burn and how to re-stage for the location.

**Tests / gates to add:**
- tests/unit/test_session_pipeline.py::test_multi_location_reburn_with_real_burn_path — skip_burn=False with mocked burn_iso/verify_disc=True; burn to Home_Shelf then Offsite_Safe; assert the second call succeeds and both ACTIVE copies exist (this currently fails, pinning the bug).
- tests/unit/test_session_pipeline.py::test_iso_retained_until_min_copies — assert ISO still exists after burn #1 when config requires 2 copies.

#### 7. [MEDIUM] default_ecc_redundancy_pct is silently ignored by dvdisaster RS03 augmented mode; ECC padding to full-medium size invalidates the staging disk-space pre-flight

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** augment_iso passes `-mRS03 -n <pct> -c`, but per the dvdisaster manual, for RS03 images 'Setting the redundancy is not possible due to constraints in the format. The codec will automatically choose the size of the smallest fitting medium.' Two consequences: (1) the documented/validated config knob (0-100, default 15) is a placebo — a user configuring 30% for extra protection gets whatever padding the medium leaves, with no warning; (2) RS03 pads every ISO to a full medium size (the project's own integration test notes a small ISO grows to ~700 MB), so the stage() disk-space pre-flight `overhead_factor = 1.05*(1+ecc/100)+1` wildly underestimates: a 1 GB incremental session targeting BD25 actually needs ~25 GB of staging space per volume, producing mid-pipeline ENOSPC/augment failures after volumes are already committed (feeding finding #3's orphaned-volume path).

**Evidence.** src/lcsas/ecc/dvdisaster.py:74-80 (`'-n', str(redundancy_pct)`); `man dvdisaster` on this host: 'RS03 images: Setting the redundancy is not possible... smallest fitting medium'; tests/integration/test_ecc_repair.py:15-17,53-55 ('RS03 augmented-image mode pads a small image up to a full optical medium (≈700 MB here)... the exact percentage barely affects the layout'); src/lcsas/burn/orchestrator.py:567-582 (pre-flight uses ecc_pct, not medium-size padding); src/lcsas/config/settings.py:32,315-318 (knob defined and range-validated as if effective).

**Existing partial coverage.** Partial: tests/unit/test_dvdisaster.py:25 pins '-n' in the command (always-on); tests/integration/test_ecc_repair.py documents full-medium padding (opt-in LCSAS_ECC_REPAIR=1)

**Verifier.** man dvdisaster on this host confirms: 'RS03 images: Setting the redundancy is not possible... smallest fitting medium.' Wrapper passes `-n 15` (dvdisaster.py:74-80; also missing the % suffix, so even ECC-file mode would read it as 15 roots). Pre-flight overhead_factor (orchestrator.py:571-573) ignores medium padding. One correction: padding targets the smallest fitting MEDIUM for the image size (1 GB → DVD ≈4.7 GB), not the configured BD25 — '~25 GB per volume' overstates; underestimate is still real (~2.2x budgeted vs ~5-6x actual for small sessions).

**Fix.** Either switch the knob to RS03 ECC-file mode (where -n x% is honored) or remove/deprecate default_ecc_redundancy_pct for augmented mode and document the smallest-fitting-medium behavior. Fix the stage() pre-flight to budget `media_type.capacity_bytes` (full padded image) + staging copy per volume.

**Tests / gates to add:**
- tests/unit/test_dvdisaster.py::test_augment_command_redundancy_semantics — pin the constructed command and add an xfail/assert documenting RS03 ignores -n, so any wrapper change is deliberate.
- tests/unit/test_session_pipeline.py::test_stage_preflight_budgets_full_medium — small session on BD25 config with a fake disk_usage reporting 5 GB free must raise the pre-flight OSError, not pass.
- Extend opt-in tests/integration/test_ecc_repair.py to assert augmented size == dvdisaster medium size and log effective redundancy.

*Verifier refinements:*
- Drop the 'pin the constructed command' half of proposal 1 — tests/unit/test_dvdisaster.py:25 already pins '-n' in args; keep only the semantics-documenting assert/xfail that RS03 augmented mode ignores -n.
- Fix the pre-flight test expectation: budget should be smallest-fitting-dvdisaster-medium per ISO (CD/DVD/DVD9/BD/BD2/BDXL3 ladder), not media_type.capacity_bytes of the configured target.

#### 8. [MEDIUM] The holographic catalog on every disc predates its own burn: no disc ever records burn results, verification, volume_copies, or locations for the final session — and nothing ever refreshes it

**CONFIRMED** (verifier confidence: high) — tracked: Partially: recovery/docs/MULTI_DISC_DESIGN.txt:327-329 (freshest-catalog selection) and UX_CONCERNS ID 008/010 (heir orientation) touch adjacent symptoms; the burn-time write-ordering gap itself is untracked.

**Gap.** inject_catalog copies the live DB into staging BEFORE the ISO is mastered and burned; volume_copies rows, VERIFY_PASS/FAIL events, VERIFIED statuses, and burn receipts are written to the hot DB AFTER mastering. So every disc of session N carries a catalog in which all of session N's volumes are STAGING with zero copies at zero locations; only a LATER session's discs supersede it, and the final session is always self-stale. For an heir working from discs alone (the design's core promise), the on-disc catalog can never answer 'where are the other copies of these discs kept?' — locations/copies for the newest (often only) session exist solely on the hot DB that the disaster destroyed. Burn receipts that capture this are written into the session staging dir (orchestrator._write_receipts) which clean_session later deletes; `catalog import-receipts` repairs only the hot DB.

**Evidence.** src/lcsas/burn/orchestrator.py:403-420 (catalog injected at stage time), 742-749 (volume_copies written at burn time, after every catalog was mastered), 805-809 (_write_receipts into session_dir; clean_session at 813-830 deletes it); restore pick-list location preference depends on catalog locations (db/queries.py:160-173). MULTI_DISC_DESIGN.txt:327-329 acknowledges 'the meta-volume's catalog is intentionally stale... uses whichever catalog is FRESHEST among currently-mounted discs' — which cannot help when ALL discs of the final session predate their own burns.

**Verifier.** Confirmed ordering: inject_catalog runs at stage time (orchestrator.py:403-420); volume_copies/VERIFY events/VERIFIED status are written at burn time (742-749), after every ISO of the session was mastered — so the final session's discs always show themselves as STAGING with zero copies. Receipts go to session_dir/receipts (965-999) which clean_session deletes (826-828); catalog import-receipts (cli/main.py:3116) repairs only the hot DB. MULTI_DISC_DESIGN.txt:327-329 acknowledges staleness only for the meta-volume's freshest-catalog selection, which cannot help the newest session.

**Fix.** Persist burn receipts somewhere durable and instruct (in START_HERE/RECOVER) printing or filing them with the discs; better, add a cheap 'catalog update disc' (or rewritable USB/last-disc append session) step after each burn session, or at minimum write planned copy locations into the catalog/volume rows BEFORE inject_catalog so the disc carries intended locations.

**Tests / gates to add:**
- tests/unit/test_holographic_catalog_freshness.py — stage+burn a session (skip_burn, mocked verify), open the catalog.db inside the staged tree, assert-and-document what it claims (volumes STAGING, volume_copies empty); then assert the chosen mitigation (e.g. intended-location pre-write) is present.
- tests/e2e: extend cdemu_blind_restore verify.sh to assert the agent can determine the full disc inventory AND copy locations from the on-disc catalog of the newest disc.

#### 9. [MEDIUM] Prune-sync trusts mirror absence as ground truth: a partially readable mirror (per-subdir PermissionError) marks live packs pruned, which consolidation then silently drops

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** cmd_scan auto-marks packs pruned whenever they are absent from the scan (default on; opt-out --no-prune-sync). The scanner swallows PermissionError per data/XY subdirectory ('Cannot scan ... continue'), so a transient NAS permission/mount glitch yields a NON-empty scan missing whole hash-prefix ranges — defeating delta.detect_pruned's only guard, which checks the fully-empty case. Wrongly pruned packs are then excluded from consolidation migration (get_packs_only_on_volumes filters is_pruned=0) and from the deprecation safety check (check_deprecation_safe filters is_pruned=0): a later consolidate+deprecate cycle deprecates the only discs holding them, and get_missing_packs reports them unrestorable. There is no un-prune command and no threshold/confirmation when a scan suddenly prunes thousands of packs.

**Evidence.** src/lcsas/packs/scanner.py:70-73 (`except PermissionError ... continue` per subdir); src/lcsas/packs/delta.py:96-104 (guard only `if not self._scanner_result`); src/lcsas/cli/main.py:819-829 (auto bulk_mark_pruned in scan); db/queries.py:384-388 (consolidation source excludes pruned); db/volumes.py:250-255 (check_deprecation_safe ignores pruned packs); no `is_pruned = 0` reset path exists in src/lcsas/db/packs.py.

**Verifier.** Confirmed: scanner.py:70-73 swallows per-subdir PermissionError and continues (test_scanner_delta.py:63-73 pins this swallowing); delta.detect_pruned guards only the fully-empty result (delta.py:95-104); cmd_scan auto-bulk_mark_pruned by default (cli/main.py:819-829) with no threshold/confirmation; consolidation source (queries.py:387) and check_deprecation_safe (volumes.py:255) both filter is_pruned=0; no unprune path exists in db/packs.py or the CLI. test_cli_scan.py has zero prune tests. Borderline high (deprecation guard ignores pruned packs), but the trigger is narrower than full unmount, which IS guarded.

**Fix.** Make prune-sync two-phase: require the pack to be absent in two consecutive scans, refuse (or require --yes) when a single scan would prune more than N packs or >X% of a repo, and add `lcsas pack unprune`. Treat any PermissionError during scan as 'scan incomplete — prune-sync disabled for this repo'.

**Tests / gates to add:**
- tests/unit/test_cli_scan.py::test_partial_scan_does_not_mark_pruned — chmod 000 one data/XY subdir (or monkeypatch os.scandir to raise PermissionError for it); run cmd_scan; assert zero packs marked pruned.
- tests/unit/test_cli_scan.py::test_mass_prune_requires_confirmation — scan returning 1 of 100 packs must not prune 99 without an explicit flag.
- tests/unit/test_consolidate.py::test_consolidation_warns_on_pruned_packs_left_behind — plan over a volume with pruned packs must surface them, not silently exclude.

#### 10. [MEDIUM] Volume status is never reconciled with its physical copies: a VERIFIED volume with every copy DEPRECATED/DESTROYED still counts as a safe replica

**CONFIRMED** (verifier confidence: high) — tracked: Adjacent: recovery/docs/READINESS_CHECKLIST.txt:78-84 notes discs 'can be lost... without being marked DESTROYED in the catalog' (operator-side); the code-side inverse — marked-destroyed copies not affecting volume-level safety math — is untracked.

**Gap.** deprecate_copy/destroy_copy update only the volume_copies row; nothing demotes the volume when its LAST copy is gone. check_deprecation_safe (the guard preventing deprecating the last replica of a pack) and get_redundancy_report both count volumes by volume.status (BURNED/VERIFIED), not by surviving ACTIVE copies. So after the user records 'the Home_Shelf disc of VOL-7 was destroyed' (its only copy), the catalog still treats VOL-7 as a valid replica: redundancy reports show packs as covered, and deprecating another volume holding the same packs is permitted — leaving packs with zero physical copies while every report says they are safe. The monthly 'VOLUME COUNT CHECK' in READINESS relies on these same numbers.

**Evidence.** src/lcsas/db/volume_copies.py:220-245 (deprecate_copy/destroy_copy touch only the copy row); src/lcsas/db/volumes.py:241-265 (check_deprecation_safe: `v2.status IN ('BURNED','VERIFIED')`, no join to volume_copies); db/queries.py:397-417 (redundancy report joins volumes by status only); no caller anywhere syncs volume.status from copy statuses (grep for deprecate_copy/destroy_copy callers shows only CLI marking).

**Verifier.** Code confirmed: deprecate_copy/destroy_copy (volume_copies.py:220-245) touch only the copy row; check_deprecation_safe (volumes.py:250-265) and get_redundancy_report (queries.py:405-417) count by volume.status with no volume_copies join; grep finds NO production caller syncing volume status from copies. One nuance: deprecate_copy/destroy_copy have no CLI wiring at all (only `location move` and consolidate --deprecate exist), so the trigger today is API/direct-DB use — which also means there is no supported way to record a destroyed disc, reinforcing rather than refuting the gap. Medium fits the bar.

**Fix.** Define replica truth as 'volume has >=1 ACTIVE copy' and use it in check_deprecation_safe and get_redundancy_report; auto-transition a volume to DEPRECATED (with an event) when its last ACTIVE copy is deprecated/destroyed, prompting re-burn via stage --for-location.

**Tests / gates to add:**
- tests/unit/test_db_volumes.py::test_deprecation_guard_ignores_volumes_with_no_active_copies — pack on VOL-A and VOL-B; destroy VOL-B's only copy; assert deprecating VOL-A raises.
- tests/unit/test_db_queries.py::test_redundancy_report_counts_active_copies_not_volume_status — same fixture; pack must appear in get_redundancy_report(min_copies=2).
- tests/unit/test_db_volume_copies.py::test_destroy_last_copy_demotes_volume.

---

## The non-technical heir's restore journey  `ux-journey`

> The Linux single-drive journey is genuinely strong (relocation-to-RAM, framed swap prompts, pre-flight binary checks, exit-64 before password entry, blind-restore e2e validation), but the journey collapses on every other advertised path. The Windows path — the one most heirs will actually face — dead-ends at step 1: restore.bat cannot find a repo on a real holographic meta-volume, and its fallback manual (RECOVER_WINDOWS.txt) gives binary paths and commands that don't exist. The split-key (Shamir) instructions burned onto every disc reference a restore.sh flag interface (--target/--key) that the canonical driver does not implement, and the "boot the disc directly" option promised in START_HERE.txt cannot be produced by `lcsas meta build` at all. The only journey-level gate (cdemu blind restore) is Linux-only, local-only, and cost-gated opt-in, so none of these doc/script contract breaks can be caught in CI today.

#### 1. [CRITICAL] restore.bat cannot discover the repository on a real meta-volume — the entire advertised Windows journey dead-ends at step 1

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** START_HERE.txt's first instruction for Windows heirs is 'Double-click restore.bat in this folder.' restore.bat discovers the repo by probing ONLY %RECOVERY%\repo\keys+index and %RECOVERY%\keys+index. But the meta-builder writes per-tenant repos to <disc-root>/metadata/<tenant>/ (the holographic layout) — there is no repo/ dir anywhere on a built meta-volume. Worse, the single-drive relocation copies only bin\ and catalog.db to %TEMP%, so after relocation RECOVERY points at the RAM dir, which can never contain a repo. Every double-click on a current-layout disc therefore terminates with 'ERROR: no restic repo (keys\ + index\) found' and exit 1 — before the password prompt, with no actionable next step. The POSIX restore.sh handles this layout (probes $RECOVERY/metadata/* and every mounted disc); restore.bat never got the equivalent. The only tests of restore.bat are static string assertions (target-triple naming, ARM64 message) — nothing executes it against a built meta-volume layout, which is exactly how this contract break shipped.

**Evidence.** recovery/scripts/restore.bat:126-135 ('if exist "%RECOVERY%\repo\keys" ... ERROR: no restic repo (keys\ + index\) found under %RECOVERY%'); relocation copies only bin + catalog.db at restore.bat:59-66; src/lcsas/meta/builder.py:2264-2278 (_bundle_metadata writes output/metadata/<repo_id>/{config,keys,index,snapshots}); contrast src/lcsas/recovery... recovery/scripts/restore.sh:488-547 (probes $RECOVERY/metadata/* plus all mounted discs); src/lcsas/meta/builder.py:2442-2444 (START_HERE: 'Windows 10 or 11: Double-click restore.bat'); tests/unit/test_restore_bat_dispatcher.py:1-8 ('Windows .bat scripts can't be executed on Linux, so we settle for static-content assertions'); docs/workflows/restore-windows.md:106-107 documents the keys/+index/ probe without flagging the layout mismatch.

**Verifier.** Verified restore.bat:126-135 probes only %RECOVERY%\repo and %RECOVERY% direct layouts; builder.py:2264-2278 writes metadata/<repo_id>/ at volume root, and restore.bat is surfaced at disc root (builder.py:1993-1998) so RECOVERY resolves to <disc>\recovery — never probed for metadata\. Relocation (lines 47-66) copies only bin+catalog.db. Repo probe precedes the password prompt (line 158). restore.sh:487-547 got the holographic-layout port; the .bat did not. Only static string tests exist (tests/unit/test_restore_bat_dispatcher.py); test_e2e_windows.sh drives the .exe, not the .bat. Both documented Windows routes broken (with finding 4) → unrecoverable-for-heir plausible; critical stands.

**Fix.** Port restore.sh's repo discovery to restore.bat: probe <disc-root>\metadata\<tenant>\ (with a tenant-selection prompt for multi-repo archives), scan drive letters D-Z for metadata\<tenant>\ on mounted data discs, and copy metadata\ during the %TEMP% relocation so post-relocation discovery still works after the meta disc is ejected.

**Tests / gates to add:**
- recovery/tests/test_restore_bat_e2e.sh: run restore.bat under `wine cmd /c` against a tree produced by MetaVolumeBuilder (metadata/<tenant>/ layout, no repo/ dir) with LCSAS_PWFILE set; assert it reaches tier-1 dispatch and exits 0 restoring a fixture repo — wire into `make -C recovery test` when wine is present (mirror of existing test_e2e_windows.sh which only execs the .exe)
- tests/unit/test_restore_bat_dispatcher.py::test_restore_bat_probes_holographic_metadata_layout: assert the .bat contains a metadata\ probe (string-level guard until the wine test lands)
- Add the manual real-Windows checklist item in WINDOWS_RECOVERY_PLAN.txt:320-331 to READINESS_CHECKLIST.txt as a per-meta-disc-build gate ('double-click restore.bat on a stock Win11 VM reaches the Password: prompt')

*Verifier refinements:*
- Caveat on the wine e2e proposal: wine 9.0's cmd is not a faithful .bat interpreter (verified delayed-expansion divergence during this audit), so treat `wine cmd /c restore.bat` as a smoke gate only and keep the real-Windows-VM checklist item (WINDOWS_RECOVERY_PLAN.txt:320-331 → READINESS_CHECKLIST.txt) as the authoritative gate

#### 2. [HIGH] On-disc split-key (Shamir) instructions tell the heir to run restore.sh with flags that do not exist

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** Every split-key archive burns START_HERE.txt and KEY_INFO.txt telling the heir, at the most fragile step of the whole journey (password reconstruction), to run `./restore.sh --target ~/restored` and optionally 'pass the saved file with --key repo.key'. The canonical restore.sh accepts only --help/--repo/--version plus positional args; `--target` falls through to positional parsing, TARGET_DIR becomes the literal string '--target', and `mkdir -p "--target"` aborts under set -e with 'mkdir: invalid option' — a cryptic dead end right after the heir has successfully reconstructed the password. keyshare_combine.py's own header repeats the nonexistent `--key` flag. Additionally `python3 keyshare_combine.py` assumes python3 on PATH (the bundled per-target CPython is never mentioned) and there is no Windows-orchestrated combiner path at all.

**Evidence.** src/lcsas/staging/metadata.py:70-73 ('./restore.sh --target ~/restored' and 'pass the saved file with --key repo.key' in _share_recovery_lines → KEY_INFO.txt) and metadata.py:370 (same command in START_HERE split block); src/lcsas/meta/keyshare_combine.py:23 ('./restore.sh --key repo.key --target ~/restored'); recovery/scripts/restore.sh:267-338 (flag loop handles only -h/--help, --repo, --version; '*) break') and restore.sh:740 ('mkdir -p "$TARGET_DIR"'); recovery/docs/PHYSICAL_DISC_VALIDATION.txt:80 repeats 'restore.sh --target /tmp/restored'.

**Existing partial coverage.** make blind-restore-split-2of5 (opt-in, local-only, agent-driven — does not enforce literal on-disc commands)

**Verifier.** Confirmed: metadata.py:58-73 and :361-370 burn './restore.sh --target ~/restored' and '--key repo.key'; keyshare_combine.py:23 repeats --key/--target. restore.sh flag loop (267-338) handles only -h/--help, --repo, --version; '--target' falls to positionals, TARGET_DIR becomes literal '--target' (line 392), and `mkdir -p "--target"` at line 740 aborts under `set -eu` (line 34) — after the password prompt (727-737). Partial coverage: make blind-restore-split-2of5 (Makefile:180) exercises the split journey, but it's opt-in (~$5, cdemu, Linux) and the agent may deviate from literal commands, which is how this shipped.

**Fix.** Either add --target/--key (alias for LCSAS_PWFILE) flags to restore.sh, or fix all four instruction sites to the real interface ('sh restore.sh ~/restored' + LCSAS_PWFILE=repo.key). Mention the bundled CPython path (recovery/bin/<triple>/python/bin/python3 keyshare_combine.py ...) as the no-system-python fallback, and add a combiner step to restore.bat or RECOVER_WINDOWS.txt.

**Tests / gates to add:**
- tests/recovery_hardening/test_split_key_docs.py: extract every `restore.sh` invocation from _share_recovery_lines, the START_HERE split block, keyshare_combine.py's header, and PHYSICAL_DISC_VALIDATION.txt; assert each uses only flags present in recovery/scripts/restore.sh's case statement (parse the flag loop) — same pattern as the existing test_disc_swap_docs.py doc-contract test
- Extend tests/e2e/cdemu_blind_restore agent_prompt_split.txt scoring to penalize any run where the agent had to deviate from the literal on-disc STEP 1/STEP 2 commands (assert the documented commands themselves succeed in verify.sh)

*Verifier refinements:*
- Place the doc-contract test under tests/unit/ (CI runs only `make test-unit` + `make test-integration` per .github/workflows/test.yml) or wire tests/recovery_hardening/ into test.yml — as proposed it would never run in CI

#### 3. [HIGH] START_HERE.txt and RECOVERY_GUIDE promise 'boot directly from the disc', but no buildable boot path exists

**CONFIRMED** (verifier confidence: high) — tracked: Partially: docs/workflows/restore-bare-metal.md:61 admits no boot test exists; recovery/docs/READINESS_CHECKLIST.txt:23-30 lists a manual boot drill. The fact that the standard build CANNOT produce a bootable disc, and that BOOT.txt's build command is fictional, is untracked.

**Gap.** The heir-facing docs offer a third route for 'No working computer at all': boot the meta disc (press F12/F2), citing BOOT.txt. But (a) `lcsas meta build` never sets bootable=True and exposes no flag for it; (b) BOOT.txt's documented build command `lcsas meta build --output meta/ --recovery-boot` references a flag that does not exist in the CLI; (c) recovery/boot/ contains only configs and build scripts — no vmlinuz, no initramfs.cpio.gz, no isolinux.bin/EFI loaders, no FreeBSD kernel — and the alternate Alpine path (BootableISOBuilder) requires a pre-built alpine_dir that no Makefile target produces. An heir with a dead machine who follows the on-disc instruction gets a non-bootable disc and no explanation. The READINESS_CHECKLIST 'META DISC BOOT TEST' would expose this to the archive owner, but only if they run it — the burned promise to the heir is unconditional.

**Evidence.** src/lcsas/meta/builder.py:2451-2454 ('>>> No working computer at all <<< Boot directly from the disc... See recovery/docs/BOOT.txt'); src/lcsas/cli/main.py:1951-1956 (MetaVolumeBuilder constructed without bootable/alpine_dir; no such CLI arg at main.py:391-398); recovery/docs/BOOT.txt:66-67 ('lcsas meta build --output meta/ --recovery-boot' — flag absent from cli/main.py per grep); recovery/boot/linux/ holds only cmdline.txt + kernel_config.*.txt and boot/freebsd/ only kernel_config.txt + loader.conf (no binaries); src/lcsas/meta/builder.py:1743-1747 (bootable=True raises without alpine_dir); docs/RECOVERY_GUIDE.md row 'No working OS → restore-bare-metal.md (boot the META disc directly)'; docs/workflows/restore-bare-metal.md:61 ('No automated test boots the actual vmlinuz + initramfs.cpio.gz').

**Verifier.** Confirmed: `lcsas meta build` argparse has only --output/--project-root (cli/main.py:391-398); MetaVolumeBuilder constructed without bootable/alpine_dir (main.py:1951-1956); grep finds no --recovery-boot anywhere in cli/ — BOOT.txt:67's build command is fictional. recovery/boot/ holds only configs + build_initramfs.sh (no vmlinuz/initramfs/loader binaries); bootable=True raises without pre-built alpine artifacts (builder.py:1743-1753); no Makefile target produces them. The no-config START_HERE (builder.py:2451-2454) advertises boot unconditionally. Partial acknowledgement only in restore-bare-metal.md:61 and the manual READINESS_CHECKLIST drill. High stands: unconditional burned promise that the standard build cannot fulfil.

**Fix.** Either wire a real `--bootable` path into `lcsas meta build` (with prebuilt kernel/initramfs artifacts or a documented fetch step) and gate the build on the artifacts existing, or make START_HERE.txt/RECOVERY_GUIDE conditionally omit the boot option when the volume is not bootable (the builder knows).

**Tests / gates to add:**
- tests/unit/test_meta_builder.py::test_start_here_boot_claim_matches_bootability: build a meta-volume with bootable=False and assert START_HERE.txt does NOT instruct booting from the disc (or that /boot/vmlinuz exists when it does)
- tests/unit/test_boot_doc_contract.py: assert every `lcsas meta build` flag mentioned in recovery/docs/BOOT.txt exists in the argparse definition (parse cli/main.py)
- Opt-in Makefile target `boot-smoke`: QEMU-boot the built bootable ISO headless and assert lcsas-init reaches the restore.sh handoff banner within 120s (closes the restore-bare-metal.md:61 gap)

#### 4. [HIGH] RECOVER_WINDOWS.txt — the burned Windows manual — gives wrong binary paths and a wrong standalone-restorer command in every fallback section

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** The Windows heir who hits any snag is routed to RECOVER_WINDOWS.txt, where: (a) all six binary-path references use the legacy `recovery\bin\x86_64-windows\` while the disc actually ships `recovery\bin\x86_64-pc-windows-gnu\` — so the recommended integrity check (`certutil` + `findstr` against MANIFEST.sha256) fails to find the file, which the doc itself says means 'the disc has been tampered with ... do not run'; (b) the last-resort Python instructions (three sections) say `python standalone_restorer.py D:\repo C:\Users\me\restored` with positional args and claim 'the script prompts for the password on stdin' — but standalone_restorer requires --repo/--password-file/--target (argparse errors on positionals, never prompts for a password); (c) `D:\repo` does not exist on any current-layout disc (repos live under metadata\<tenant>\ and a pack cache must be assembled first). Combined with the restore.bat failure (finding 1), every documented Windows route fails for an heir following the text literally.

**Evidence.** recovery/docs/RECOVER_WINDOWS.txt:78,112,186,194-195,274 (x86_64-windows paths) vs src/lcsas/meta/builder.py:2152 ('x86_64-pc-windows-gnu': ... 'lcsas-restore.exe') and tests/unit/test_restore_bat_dispatcher.py which bans the legacy name from the .bat but not the doc; RECOVER_WINDOWS.txt:207,306,346 (positional invocations) and :349-350 ('prompts for the password on stdin') vs src/lcsas/restore/standalone_builder.py:141-152 (--repo/--password-file/--target all required=True); RECOVER_WINDOWS.txt:85-86 ('If they do not [match], the disc has been tampered with').

**Verifier.** Confirmed: RECOVER_WINDOWS.txt lines 78/112/186/194-195/274 use recovery\bin\x86_64-windows\ while the meta-builder writes on-disc bins under the rust triple x86_64-pc-windows-gnu (builder.py:2146-2165 — x86_64-windows is the SOURCE-tree dir, dst is the triple). Lines 207/306/346 give positional standalone_restorer invocations and :349-350 claim a stdin password prompt; the generated CLI requires --repo/--password-file/--target (standalone_builder.py:141-152) and argparse errors on positionals. test_restore_bat_dispatcher.py bans the legacy name from the .bat only, not the doc. Tampering false-alarm framing at :85-86 confirmed. High stands.

**Fix.** Sweep RECOVER_WINDOWS.txt to the current target-triple paths and the real standalone_restorer flag set (including the cache-assembly pre-step), and state that the password must be supplied via --password-file.

**Tests / gates to add:**
- tests/recovery_hardening/test_windows_doc_paths.py: assert 'x86_64-windows\' (the legacy dir) appears nowhere in RECOVER_WINDOWS.txt / UX_CONCERNS mitigation text that names on-disc paths, and that the triple in the doc matches builder.py's tier1_map key
- Same file: regex-extract every `standalone_restorer.py` invocation across docs/ and recovery/docs/ and assert each contains --repo, --password-file, and --target (mirrors the doc-contract style of test_env_var_docs.py)

*Verifier refinements:*
- Place the proposed doc-contract tests in tests/unit/ rather than tests/recovery_hardening/ — the hardening tier is not executed by CI (test.yml runs make test-unit/test-integration only), so as proposed they would not be an always-on gate

#### 5. [HIGH] Production (config-built) META discs get the weakest START_HERE.txt: no run command, no Windows route, 'you need Linux', and a reference to a file that is not on the disc

**CONFIRMED** (verifier confidence: high) — tracked: Adjacent to recovery/docs/UX_CONCERNS.txt ID 002 ('START_HERE.txt ... already partially exists — needs improvement', OPEN) and ID 008, but the config-variant regression (no OS dispatch, missing-file reference, 'need Linux' claim) and the missing-path command are not recorded anywhere.

**Gap.** MetaVolumeBuilder writes two very different START_HERE.txt variants. With a config (the production case), it uses HolographicInjector.write_start_here — text designed for DATA discs: it says 'You need a computer running Linux, or someone who can help you use one' (contradicting the Windows/macOS support), gives no actual command at all (only 'a script called restore.sh that automates everything'), never mentions restore.bat, and directs the reader to 'the file RESTORE_INSTRUCTIONS.txt on this disc' — which the meta-builder never writes to the meta volume. Only the no-config fallback variant carries the useful per-OS dispatch (Windows: double-click restore.bat / macOS-Linux: sh restore.sh / boot). Even that variant's command, 'Open a Terminal, then run: sh restore.sh ~/restored', omits the disc path — a terminal opens in $HOME, so the literal command fails with 'sh: restore.sh: No such file'. The first document the heir reads is the worst one on exactly the archives that matter.

**Evidence.** src/lcsas/meta/builder.py:2421-2426 (config path → injector.write_start_here, and no write_restore_instructions call in _write_start_here or build()); src/lcsas/staging/metadata.py:403-404 ('You need a computer running Linux'), :424-426 ('the file RESTORE_INSTRUCTIONS.txt on this disc has step-by-step manual recovery instructions' — file absent from meta volume layout per builder.py build() steps 1710-1724); builder.py:2447-2449 (no-config variant: 'Open a Terminal, then run: sh restore.sh ~/restored' with no cd/mount path).

**Verifier.** Confirmed: builder.py:2421-2427 routes the config case to HolographicInjector.write_start_here — data-disc text saying 'a computer running Linux' (metadata.py:404), no runnable command, no restore.bat mention, and 'RESTORE_INSTRUCTIONS.txt on this disc' (metadata.py:424-426). write_restore_instructions is called only by burn/orchestrator.py:425 for data discs — never by the meta builder. The no-config variant (builder.py:2447-2448) does carry per-OS dispatch but 'sh restore.sh ~/restored' lacks any disc path. tests/unit/test_meta_builder.py:347-356 asserts only file existence, not content. High stands — first-touch doc on production meta discs misdirects non-Linux heirs.

**Fix.** Make the meta-volume always use a META-specific START_HERE that merges the config survivability fields (owner, key hints, split block) with the per-OS dispatch block, includes the mount path in commands (e.g. 'sh /Volumes/LCSAS_META/restore.sh ~/restored' / 'sh /media/$USER/LCSAS_META/restore.sh'), and either write RESTORE_INSTRUCTIONS.txt to the meta volume or drop the reference.

**Tests / gates to add:**
- tests/unit/test_meta_builder.py::test_start_here_with_config_has_os_dispatch: build with a config and assert START_HERE.txt mentions restore.bat (Windows), a runnable restore.sh command containing a path separator before 'restore.sh', and does NOT claim Linux is required
- tests/unit/test_meta_builder.py::test_start_here_references_only_files_present: parse START_HERE.txt for referenced filenames (RESTORE_INSTRUCTIONS.txt, KEY_INFO.txt, restore.bat, ...) and assert each exists in the built output tree

#### 6. [MEDIUM] restore.sh --help QUICK START tells the operator to start from 'ANY data disc', but restore.sh only exists on the META disc

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** The first help text a confused operator (or a phone-a-friend helper) reads says: '1. Insert ANY data disc into your drive. 2. Mount it. 3. Run: sh /mnt/restore.sh ~/restored/ latest'. Data discs do not carry restore.sh (staging injects only standalone_restorer.py, START_HERE, KEY_INFO, RESTORE_INSTRUCTIONS, catalog.db, metadata/, data/), and RECOVER.txt explicitly states the META disc 'is the only disc that contains the recovery binaries ... and restore.sh'. Following the QUICK START yields 'sh: can't open /mnt/restore.sh' — a contradiction inside the primary driver's own documentation, likely a leftover from the legacy bash driver's bootstrap-from-data-disc flow.

**Evidence.** recovery/scripts/restore.sh:273-277 ('QUICK START: 1. Insert ANY data disc ... 3. Run: sh /mnt/restore.sh ~/restored/ latest'); recovery/docs/RECOVER.txt:252-254 ('Always start with the META disc ... It is the only disc that contains the recovery binaries (bin/<arch>/), the catalog, and restore.sh'); src/lcsas/staging/metadata.py:189-310 (data-disc payload: standalone_restorer.py + txt docs, no restore.sh).

**Existing partial coverage.** tests/recovery_hardening/test_restore_sh_ux.py asserts QUICK START presence only (and is not CI-run)

**Verifier.** Confirmed: restore.sh:273-277 ('Insert ANY data disc ... sh /mnt/restore.sh') vs RECOVER.txt:252-254 ('Always start with the META disc ... only disc that contains ... restore.sh'). HolographicInjector writers (metadata.py) stage standalone_restorer.py + txt docs onto data discs — no restore.sh; staging/builder.py copies none either. test_restore_sh_ux.py:505-513 asserts only that the QUICK START heading exists, not that its content is correct. Recoverable friction (clear 'can't open' error, RECOVER.txt corrects it) — medium is right.

**Fix.** Change the QUICK START to 'Insert the disc labelled LCSAS_META' and keep the rest; optionally also stage restore.sh onto every data disc (it is ~40 KB and would make the help true).

**Tests / gates to add:**
- Extend tests/recovery_hardening/test_disc_swap_docs.py with an assertion that restore.sh's --help heredoc instructs starting from the META disc (contains 'META') and does not contain 'ANY data disc'

*Verifier refinements:*
- Note the proposed extension targets tests/recovery_hardening/test_disc_swap_docs.py, which is not run in CI; mirror the assertion in tests/unit/ or wire the hardening tier into test.yml for it to be a real gate

#### 7. [MEDIUM] Restore overwrites an existing non-empty target without warning — the one step where a wrong action by the heir destroys data

**CONFIRMED** (verifier confidence: high) — tracked: recovery/docs/UX_CONCERNS.txt ID 009 (OPEN, P2)

**Gap.** lcsas-restore writes directly into --target; restore.sh does `mkdir -p "$TARGET_DIR"` and proceeds, and restore.bat creates the chosen folder and runs. An heir who restores into Documents\restored twice with different snapshots, or into an existing folder of their own files with colliding names, silently overwrites current data with decades-old archive content. This is the only point in the journey where a confused user causes irreversible loss of *live* data rather than just a failed restore. Tracked as UX_CONCERNS ID 009 but rated LOW and still OPEN; against the fool-proof-for-heirs bar it is a real destructive-action gap with no confirmation prompt anywhere in any tier.

**Evidence.** recovery/docs/UX_CONCERNS.txt:171-183 (ID 009 OPEN, 'files are overwritten without prompt'; mitigation --verify-only / refuse non-empty without --force not implemented); recovery/scripts/restore.sh:740 (mkdir -p "$TARGET_DIR" with no emptiness check); recovery/scripts/restore.bat:148-156 (creates target, no emptiness check); RECOVER.txt:135-148 documents idempotent re-runs into the same dir, which makes a hard refusal wrong but a 'target is non-empty and does not look like a previous LCSAS restore' warning right.

**Existing partial coverage.** Tracked (not covered): recovery/docs/UX_CONCERNS.txt ID 009, OPEN

**Verifier.** Confirmed: restore.sh:740 `mkdir -p "$TARGET_DIR"` with no emptiness check; restore.bat:151 creates the target and proceeds; UX_CONCERNS ID 009 OPEN ('files are overwritten without prompt', mitigations unimplemented). RECOVER.txt 'RETRY SAFETY' section documents idempotent re-runs into the same dir, so the finding's nuance (warn on foreign non-empty, keep silent resume) is correct. Tracked but unfixed; medium matches the bar (destructive risk worth fixing, not a likely journey failure).

**Fix.** In restore.sh/restore.bat: if TARGET_DIR exists and is non-empty and lacks the marker of a previous LCSAS restore (e.g. a .lcsas-restore-state file the tier-1 binary writes), print a framed warning naming the directory and require typed confirmation (default abort). Keep silent resume for re-runs.

**Tests / gates to add:**
- tests/recovery_hardening/test_nonempty_target_guard.py: run restore.sh with a pre-populated TARGET_DIR (foreign files, no resume marker) and assert it prompts/aborts; with a marker present assert it resumes silently
- Add the guard's prompt text to the cdemu blind-restore verify.sh scoring so the agent journey exercises it

#### 8. [MEDIUM] The only end-to-end journey gate (cdemu blind restore) is Linux-only, local-only, and cost-gated opt-in; Windows and macOS journeys have zero functional gates

**CONFIRMED** (verifier confidence: high) — tracked: Partially: .github/workflows/test.yml:68-73 documents the cdemu CI exclusion; docs/workflows/restore-windows.md 'Test coverage and gaps' documents the .bat gap. No ledger tracks 'no functional gate for any non-Linux journey' as a risk item.

**Gap.** Everything the heir journey depends on across OSes is validated, when at all, by `make blind-restore` — which refuses to run without LCSAS_BLIND_ACK_COST=1, needs sudo + cdemu/vhba, and is explicitly NOT run in CI ('cdemu is NOT installed in CI'). There is no Windows equivalent at any level: restore.bat is never executed by any test (static string checks only), the wine e2e drives the .exe directly and bypasses the .bat, and macOS has nothing. This is precisely why findings 1, 2, 4 and 5 (script/doc contract breaks on the non-Linux and split-key paths) shipped and persisted: the journey-level safety net only covers the one path that already works.

**Evidence.** Makefile:80-98 (blind-restore requires LCSAS_BLIND_ACK_COST=1 and sudo tests/e2e/cdemu_blind_restore/setup.py); .github/workflows/test.yml:68-73 ('cdemu is NOT installed in CI ... e2e blind-restore suite is [run on a self-hosted/local box]'); tests/unit/test_restore_bat_dispatcher.py:3-8 ('we settle for static-content assertions'); docs/workflows/restore-windows.md:159-166 ('restore.bat is not driven through a CMD interpreter ... Real-Windows verification is a manual checklist').

**Verifier.** Confirmed and slightly understated: Makefile:80-98 gates blind-restore on LCSAS_BLIND_ACK_COST=1 + sudo/cdemu; test.yml:68-73 documents cdemu exclusion; restore.bat is never executed by any test (static assertions only; recovery/Makefile test-windows drives the .exe via test_e2e_windows.sh). Additionally, CI runs only `make test-unit` + `make test-integration` — the entire tests/recovery_hardening tier (where most journey doc-contract tests live) is also not CI-run, strengthening the finding. Medium is right: process gap that enabled findings 1/2/4/5.

**Fix.** Add an always-run CI doc/script contract layer (the unit-level tests proposed in findings 1-6 are all CI-runnable), plus a wine-cmd restore.bat e2e in the recovery test suite, and record the blind-restore cadence (which paths, last run, score) in READINESS_CHECKLIST.txt so opt-in coverage is at least auditable.

**Tests / gates to add:**
- CI job 'journey-contracts' in .github/workflows/test.yml running tests/recovery_hardening/test_*_docs.py (split-key flags, Windows doc paths, START_HERE file references, --help disc claim) on every push — pure-Python, no cdemu needed
- recovery/tests/test_restore_bat_e2e.sh under wine cmd (see finding 1), wired into audit-gate.yml's recovery-path filter
- Makefile `blind-restore-windows` (wine-based agent variant) added to the blind-restore-variants family, opt-in like the others but existing

#### 9. [LOW] The 'printed sheet' leg of the journey is unbacked by tooling — the promised paper Recovery Card template was never created

**CONFIRMED** (verifier confidence: high) — tracked: recovery/docs/UX_CONCERNS.txt ID 006 (WONTFIX-CRYPTOGRAPHIC overall, but the RECOVERY_CARD.txt mitigation bullet is unimplemented and untracked as its own item)

**Gap.** The heir scenario assumes a printed sheet stored with the discs (password location, disc inventory, start-here pointer). UX_CONCERNS ID 006's mitigation promises 'a paper-printable Recovery Card template under docs/RECOVERY_CARD.txt' — the file does not exist, no CLI command generates a printable sheet, and ESTATE_PLANNING.md only offers a manual checklist + letter template the owner must hand-assemble. Since password loss is acknowledged as 'the actual #1 failure mode in real-world archive inheritance' (ID 006, IMPACT: HIGHEST), the absence of any generated, fill-in-and-print artifact (with the archive's actual repo names, disc count, K-of-N share scheme, and the literal start command) leaves the highest-impact mitigation entirely to owner diligence.

**Evidence.** recovery/docs/UX_CONCERNS.txt:129-139 (ID 006 mitigation: 'Add a paper-printable Recovery Card template under docs/RECOVERY_CARD.txt'); docs/ directory listing contains no RECOVERY_CARD.txt; docs/ESTATE_PLANNING.md:33-37 ('Maintain a paper manifest — Print a list of all disc labels' — manual, no generator); no `lcsas` subcommand produces a printable artifact (cli/main.py subparser list).

**Verifier.** Confirmed: docs/RECOVERY_CARD.txt absent; UX_CONCERNS ID 006 mitigation bullet promises it; ESTATE_PLANNING.md is a manual checklist. One overstatement: `lcsas key split` DOES emit printable per-share cards (cli/main.py:3183-3279), so 'no CLI command generates a printable sheet' is wrong for the split-key case — but the full Recovery Card (disc inventory, password location, literal start command) has no generator, and the share card's 'lcsas key combine' command itself assumes an installed LCSAS rather than the on-disc keyshare_combine.py. Low stands.

**Fix.** Add `lcsas estate card` (or extend `lcsas key split`) to emit a one-page plain-text/printable RECOVERY_CARD.txt populated from config: owner, repos, disc-label prefix and count, K-of-N share scheme, where the password/cards live (key_storage_hints), and the literal first command per OS. Reference it from ESTATE_PLANNING.md's checklist.

**Tests / gates to add:**
- tests/unit/test_recovery_card.py: generate the card from a fixture config and assert it contains owner, K/N values, key_storage_hints, and a runnable start command consistent with restore.sh's real interface
- Doc-contract assertion that UX_CONCERNS ID 006's mitigation bullets either exist as artifacts or are marked deferred with an issue number

*Verifier refinements:*
- Extend the existing `lcsas key split` card output (cli/main.py:_share_card_text) rather than building a new generator, and fix the card's 'lcsas key combine' instruction to also name the on-disc keyshare_combine.py path

**Refuted in verification (recorded for honesty):**
- ~~[medium] restore.bat silently corrupts passwords containing '!' (delayed expansion) — correct password is rejected as wrong~~ — Refuted for real Windows. `set /p` stores input unparsed, and `> "%PWFILE%" echo !LCSAS_PW!` (restore.bat:172) uses delayed-expansion read — the canonical bang-safe form: real cmd does not rescan substituted values (the standard EnableDelayedExpansion 'toggle' technique depends on exactly this). The %LCSAS_PW% empty-check at :164 mangles only that line's comparison, never the stored value. I reproduced the mangle ('my!pass^word!end'→'myend') ONLY under wine 9.0 cmd — a known-unfaithful reimplementation; heirs run real cmd. Residual risks (embedded quotes breaking :164, CRLF in pwfile) are different mechanisms, not this claim.

---

## Key & secret availability (escrow, Shamir shares, passphrase flow)  `keys-escrow`

> Audited key & secret availability end-to-end: docs (KEY_SHARE_FORMAT, ESTATE_PLANNING, CRYPTO.txt, RECOVERY_GUIDE, RECOVER/RECOVER_WINDOWS), the SLIP-0039 stack (src/lcsas/keyshare, cli key split/combine, meta/keyshare_combine.py, recovery/src/lcsas-keyshare + all 6 target bins), restore.sh key handling, staging/metadata.py heir-doc generation, config flow, and the blind-restore split-key harness. The cryptographic core is solid (SLIP-0039 with 45 official vectors, fail-closed, 2-of-5 default, C combiner on all 6 targets and on the meta-volume), and the password correctly lives off-disc. The fool-proof layer around it is where it breaks: the production share-card artifact is rejected by every combiner even though all heir docs say to pass cards (empirically reproduced); the on-disc STEP-2 instructions use restore.sh flags (--target, --key) that don't exist; `lcsas key split` never verifies the escrowed secret recombines or unlocks the repo, and there is no rotation/verify story; and the 15/15 blind split-key runs prove none of this because the prompt spoon-feeds the exact winning commands and stages bare mnemonics instead of cards. Secondary gaps: heir docs name only the python3 combiner (nothing points at the C binary; Windows/RECOVERY_GUIDE have zero share coverage), the C combiner is outside coverage-c/fuzz/audit-gate, typo feedback is generic with no prefix entry, key_split/K/N disc text is unreconciled config, the promised RECOVERY_CARD.txt never shipped, and KEY_SHARE_FORMAT.md falsely claims MANIFEST pinning. 10 findings: 4 high, 5 medium, 1 low; most untracked in the known-issues ledgers.

#### 1. [HIGH] Real share-card files are rejected by every combiner, yet all heir docs say 'pass the card files'

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** ESTATE_PLANNING.md, on-disc START_HERE.txt and KEY_INFO.txt all tell the heir to run `python3 keyshare_combine.py <card1> <card2>` with their card files. But the card artifact `lcsas key split` actually produces ({repo}-share-N-card.txt, the one handed to holders and the only thing a holder will possess after printing) contains header lines ('================ LCSAS KEY SHARE ================', 'Repository : alpha', 'WHAT THIS IS', ...). The Python combiner treats every non-blank non-# line as a mnemonic and dies on 'Unknown word in mnemonic: ================'; the C combiner reads the whole file as one mnemonic and dies with a generic 'insufficient, corrupt, or mismatched shares'. Empirically verified: feeding two genuine card files to both combiners fails (python rc=1, C rc=1). Only the bare -share-N.txt mnemonic file works — and the unit tests deliberately exclude -card.txt files, so the gap is invisible to the test suite.

**Evidence.** src/lcsas/cli/main.py:3183-3211 (_share_card_text card format with header lines); src/lcsas/staging/metadata.py:61-63 ('python3 keyshare_combine.py <card1> <card2>' / 'pass any {k} card files'), 361-365 (same in START_HERE); docs/ESTATE_PLANNING.md:84-91; src/lcsas/meta/keyshare_combine.py:97-103 (_read_mnemonics: every line is a mnemonic); recovery/src/lcsas-keyshare/main.c:33-72 (whole file = one mnemonic), 179-181 (generic error); tests/unit/test_cli_key.py:40-43 (tests filter out -card.txt); empirical repro in this audit: both combiners exit 1 on genuine card files.

**Verifier.** Reproduced live: ran cmd_key_split, fed both -card.txt files to keyshare_combine.py (rc=1, 'Unknown word in mnemonic: ================') and recovery/bin/x86_64/lcsas-keyshare (rc=1, generic error); bare -share-N.txt works (recovered byte-exact). Verified metadata.py:61-66/363-365 and ESTATE_PLANNING.md:84-91 say 'pass any K card files'. tests/unit/test_cli_key.py:40-43 explicitly excludes -card.txt; TestHeirDocGating only asserts text presence, never command validity. No always-on coverage anywhere. cmd_key_combine (main.py:3311) reads whole file as one mnemonic — also card-intolerant.

**Fix.** Make all three combiners card-tolerant: when parsing input, keep only lines whose every token is in the SLIP-0039 wordlist (or parse the 'THE SHARE WORDS' section explicitly). Same fix in keyshare_combine.py, cli `lcsas key combine`, and lcsas-keyshare main.c (rebuild all 6 target bins). Alternatively (weaker) rewrite every heir doc to name the -share-N.txt file — but heirs will hold printed cards, so card-tolerance is the right fix.

**Tests / gates to add:**
- tests/unit/test_keyshare_combine.py::test_combine_accepts_real_card_files — generate cards via cmd_key_split, feed the -card.txt files (not the share files) to keyshare_combine.main and cli key combine; assert password recovered byte-exact
- recovery/tests/test_keyshare.c — add a case feeding a full card text (with header lines) to lcsas_keyshare_recover_password; assert success
- Makefile gate: extend `make gate` so the card round-trip test runs in test-unit (no external tools needed)

#### 2. [HIGH] On-disc split-key instructions tell the heir to run restore.sh with flags that do not exist (--target, --key)

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** The split-key block burned into every disc's START_HERE.txt and KEY_INFO.txt instructs STEP 2 as `./restore.sh --target ~/restored` and '(or pass the saved file with --key repo.key)'. restore.sh's flag parser accepts only -h/--help, --repo, --version; anything else falls through to positional parsing, so `--target` becomes the TARGET_DIR (a directory literally named ./--target) and `~/restored` becomes the SNAPSHOT_ID — the restore fails with a snapshot-not-found error the heir cannot interpret. `--key` similarly does not exist (the real mechanisms are LCSAS_PWFILE / the Password: prompt). keyshare_combine.py's own usage text repeats the bogus `./restore.sh --key repo.key --target ~/restored`. A non-technical heir following the on-disc docs verbatim fails at this step even after correctly reconstructing the password.

**Evidence.** src/lcsas/staging/metadata.py:70 ('./restore.sh --target ~/restored'), 73 ('--key repo.key'), 367-370 (START_HERE split block, same command); src/lcsas/meta/keyshare_combine.py:22-23 ('./restore.sh --key repo.key --target ~/restored'); recovery/scripts/restore.sh:267-339 (flag case: only -h|--help, --repo, --version; '*) break' makes unknown flags positional), restore.sh:288-290 (LCSAS_PASSWORD/LCSAS_PWFILE are the only key-file mechanisms).

**Verifier.** Verified restore.sh:267-338: case accepts only -h/--help, --repo, --version; '*) break' makes '--target' positional → TARGET_DIR='--target', '~/restored' becomes SNAPSHOT_ID → undecipherable failure. No --key flag; pw mechanisms are LCSAS_PASSWORD/LCSAS_PWFILE (restore.sh:288-292). Broken text confirmed at metadata.py:70/73/370 and keyshare_combine.py:23. Existing tests (test_restore_sh_repo_flag.py, test_restore_sh_version_flag.py) cover only real flags; nothing checks doc/flag consistency or unknown-flag rejection. Heir typing the password at the prompt still fails because SNAP is a bogus path.

**Fix.** Fix the generated text to the real invocation (`sh /mnt/restore.sh ~/restored` + type the password at the Password: prompt, or LCSAS_PWFILE=repo.key) — or add genuine --target/--key flags to restore.sh so the simpler wording becomes true (safer for heirs; --key maps to LCSAS_PWFILE). Then add a doc/CLI consistency gate so generated heir docs can never reference unsupported flags again.

**Tests / gates to add:**
- tests/unit/test_heir_doc_commands.py — render START_HERE.txt/KEY_INFO.txt with key_split=True, extract every `restore.sh` flag mentioned, and assert each appears in restore.sh's flag-parsing case block (parse the script text)
- tests/recovery_hardening/test_restore_sh_flags.py — run `sh restore.sh --target /tmp/x` and assert it either succeeds as documented or rejects the unknown flag loudly (no silent positional misparse)
- If --key/--target are added: shell-coverage case in make shell-coverage exercising `restore.sh --key pwfile --target dir`

#### 3. [HIGH] lcsas key split never verifies the escrowed secret — shares of a stale/wrong password would be discovered decades later

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** cmd_key_split reads config/--password-file bytes, splits, writes cards, done. There is (a) no recombine round-trip of the freshly written share files, and (b) no check that the password actually unlocks the repo's key file — even though a pure-Python scrypt+Poly1305 key-unlock path already exists in the codebase (restore/restic_fallback.py) and the repo keys/ dir is in the mirror. The burn pipeline never decrypts anything (scanner/stage/ISO/ECC are byte-copies), so a wrong or rotated password_file is never exercised anywhere in the burn lifecycle. If the owner splits a stale password, destroys the single copies as ESTATE_PLANNING implies, the heir reconstructs a perfectly checksummed wrong password — unrecoverable. There is also no `lcsas key verify` for the READINESS checklist's 're-confirm annually' drill, and no key-rotation story anywhere (zero 'rotation' mentions outside Windows cert notes): re-keying the repo silently invalidates all distributed cards with nothing to detect or document it.

**Evidence.** src/lcsas/cli/main.py:3260-3294 (split → write → print, no verification); src/lcsas/burn/orchestrator.py:233-292 (preflight checks binaries/capacity only, never the password); recovery/docs/READINESS_CHECKLIST.txt:32-42 (manual annual key-redundancy check, no tool support); grep 'rotat' across docs/ and recovery/docs/ → only WINDOWS_RECOVERY_PLAN.txt:382 (cert rotation).

**Verifier.** Verified cmd_key_split (main.py:3214-3294): read → split → write → print; no recombine round-trip, no repo-unlock check. split_secret has no internal verify. Burn orchestrator preflight (233-292) checks binaries/capacity only; burn path never touches the password (rustic wrapper used only by analyze/restore commands). No 'key verify' anywhere. READINESS_CHECKLIST:32-42 is manual. Minor evidence overstatement: 'rotat' also appears in docs/workflows (multi-tenant.md:322 'key rotation does not update...', meta-volume.md:606, restore-disc-only.md:654) — but none address escrow-share invalidation, so the gap stands. Existing tests prove algorithm round-trip only, not runtime verification.

**Fix.** 1) In cmd_key_split, always recombine the written share files in-memory and assert byte-equality with the input password before printing success. 2) Add a repo-unlock verification (default on, --no-verify-repo to skip): derive scrypt from the password and authenticate against a key file under the repo mirror's keys/ using the existing restic_fallback primitives. 3) Add `lcsas key verify --repo R --share-file ...` so the owner/executor can drill annually that K cards still open the live repo. 4) Document rotation: re-split + recall/destroy superseded cards; print the SLIP-0039 identifier and split date on each card so stale sets are identifiable.

**Tests / gates to add:**
- tests/unit/test_cli_key.py::test_split_roundtrip_verifies — corrupt the written share post-split via monkeypatched writer and assert split fails loudly
- tests/unit/test_cli_key.py::test_split_rejects_wrong_repo_password — point --password-file at a wrong password with a real keys/ fixture; assert non-zero exit and clear error
- tests/unit/test_cli_key.py::test_key_verify_detects_stale_shares — split, then re-key the fixture repo; `lcsas key verify` must fail
- READINESS_CHECKLIST.txt: change the key-redundancy item to name the `lcsas key verify` command, pinned by a static doc test like tests/recovery_hardening/test_env_var_docs.py

#### 4. [HIGH] Blind split-key 15/15 proves the tools, not the heir journey: the prompt spoon-feeds exact commands and stages non-production share artifacts

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** The split-key blind variant's prompt contains a QUICK REFERENCE giving the agent the precise winning command sequence — including the arch-specific combiner path (/mnt/recovery/bin/x86_64/lcsas-keyshare), the exact `sh /mnt/restore.sh ~/restored/ latest` invocation, and 'DO NOT deviate from these steps'. setup.py stages bare mnemonic files (mnemonic + newline), not the production -card.txt artifacts holders actually receive. So the gate never exercises: the on-disc START_HERE/KEY_INFO instructions (which are broken — findings 1 and 2 sailed through four 15/15 runs), card-file parsing, or the typed-from-print path. The PLAN's acceptance claim ('production meta-disc output unmodified') is true of the disc but not of the heir's information environment.

**Evidence.** tests/e2e/cdemu_blind_restore/agent_prompt_split.txt:11-35 (full command sequence incl. combiner path and restore.sh args), 105 ('DO NOT deviate from these steps'); tests/e2e/cdemu_blind_restore/setup.py:581-586 (writes bare `mnemonic + "\n"` as 'cards'); contrast with the broken on-disc instructions in src/lcsas/staging/metadata.py:61-73.

**Verifier.** Verified agent_prompt_split.txt:11-35 gives the full winning sequence (combiner path, restore.sh args, 'DO NOT deviate' at line 105); setup.py:581-585 writes bare mnemonic+newline as 'cards', never the production -card.txt artifacts. So the only end-to-end split-key gate cannot detect findings 1-2 — and demonstrably didn't. The prompt even forbids reading on-disc scripts, but never requires following on-disc docs. Severity high: the project's acceptance gate gives false assurance on exactly the heir-journey layer that is broken.

**Fix.** Add a docs-driven blind variant: the prompt states only the scenario ('you inherited these discs and hold these 2 printed share cards; insert the disc labelled LCSAS_META and follow the instructions you find') plus the test-rig mechanics (disc-loader/restore-shell), with the share artifacts staged as real -card.txt card text. Score it on the same 15-point rubric. Keep the current scripted variant as a tooling smoke test, but make the docs-driven variant the acceptance gate for any heir-doc change.

**Tests / gates to add:**
- Makefile target blind-restore-split-docs-driven (haiku only, LCSAS_BLIND_ACK_COST guard) — prompt contains no LCSAS commands; setup stages production card files via cmd_key_split; gate = 15/15 twice consecutively
- tests/e2e/cdemu_blind_restore/no_bypass_check.py extension: assert the docs-driven prompt file contains no occurrence of 'restore.sh', 'keyshare', or '/mnt/recovery/bin'
- Cheap pre-gate (no LLM cost): tests/unit/test_heir_doc_commands.py from finding 2, executed in `make gate`, so doc command-rot is caught without a $5 blind run

#### 5. [MEDIUM] Heir-facing share guidance names only the python3 combiner; C lcsas-keyshare, Windows path, and the printed RECOVERY_GUIDE have zero key-share coverage

**CONFIRMED** (verifier confidence: high) — tracked: recovery/docs/UX_CONCERNS.txt ID 001 tracks the general Windows-recovery gap, but not the key-share omission; the python-only doc gap is untracked

**Gap.** Phase 5 shipped a tier-1-grade C combiner on all 6 targets precisely so reconstruction needs no python — and the split-key blind run proved that path. But every heir-facing doc (START_HERE split block, KEY_INFO share lines, ESTATE_PLANNING letter) says only `python3 keyshare_combine.py`; nothing on-disc points the heir at recovery/bin/<arch>/lcsas-keyshare. If python3 won't run (the very scenario the tier-1 design exists for), the heir has no documented fallback. Worse on Windows: restore.bat and RECOVER_WINDOWS.txt contain zero key-share mentions although lcsas-keyshare.exe is shipped, and bare Windows has no python3 at all. docs/RECOVERY_GUIDE.md — the document ESTATE_PLANNING tells the owner to print for the binder — also never mentions shares or the reconstruct-first pre-step.

**Evidence.** src/lcsas/staging/metadata.py:61, 363 (python3-only instruction); grep 'keyshare|share card|key share|SLIP' over docs/RECOVERY_GUIDE.md and recovery/docs/RECOVER.txt → no matches; recovery/docs/RECOVER_WINDOWS.txt and recovery/scripts/restore.bat → no keyshare mentions (restore.bat:179 'split' is about multi-volume packs); recovery/bin/x86_64-windows/lcsas-keyshare.exe and all 6 target dirs contain the binary (ls verified); .claude/skills/key-escrow/PLAN.md:87-88 (C5.4/C5.5: C combiner is the proven python-free path, but only agent_prompt_split.txt was updated).

**Existing partial coverage.** recovery/docs/UX_CONCERNS.txt ID 001 (general Windows gap only — keyshare omission untracked)

**Verifier.** Verified: metadata.py:61/363 name python3 keyshare_combine.py only; grep for keyshare/share card/key share/SLIP returns 0 matches in docs/RECOVERY_GUIDE.md, recovery/docs/RECOVER.txt, RECOVER_WINDOWS.txt, restore.bat (restore.bat:179 'split' is multi-volume packs). lcsas-keyshare(.exe) confirmed present in all 6 recovery/bin target dirs including x86_64-windows. UX_CONCERNS ID 001 (OPEN) tracks the general Windows gap, not key shares. Severity medium correct: fallback exists physically, just undocumented for the heir.

**Fix.** Update _share_recovery_lines and the START_HERE split block to present lcsas-keyshare (recovery/bin/<arch>/lcsas-keyshare, .exe on Windows) as the primary combiner with python3 keyshare_combine.py as fallback — mirroring the tier ordering. Add a key-share pre-step section to RECOVER.txt, RECOVER_WINDOWS.txt, restore.bat comments, and RECOVERY_GUIDE.md (gated on the same split-archive wording since RECOVERY_GUIDE is generic).

**Tests / gates to add:**
- tests/unit/test_staging_metadata.py::test_split_block_names_c_combiner — rendered KEY_INFO/START_HERE with key_split=True must mention recovery/bin and lcsas-keyshare, python3 only as fallback
- tests/recovery_hardening/test_keyshare_docs.py — static doc test (pattern of test_disc_swap_docs.py) asserting RECOVER.txt and RECOVER_WINDOWS.txt each contain a KEY SHARES section naming lcsas-keyshare(.exe)
- Extend the docs-driven blind variant (finding 4) to a variant where python3 is removed from the bundled tools, forcing the C-combiner path from on-disc docs alone

#### 6. [MEDIUM] lcsas-keyshare C combiner sits outside every tier-1 audit gate: no coverage-c, no fuzzing, silent in EXEMPTIONS

**CONFIRMED** (verifier confidence: high) — tracked: .claude/skills/key-escrow/PLAN.md C5.3 ([~] in-progress follow-up note) — not in any of the six known-issues ledgers

**Gap.** The C combiner parses untrusted, heir-typed text (mnemonics) on the 50-year critical path, but coverage-c filters to SRCDIR=src/lcsas-restore only, all five fuzz harnesses target lcsas-restore parsers (json/b64/zstd/path/repo — no slip39 mnemonic fuzzer), and EXEMPTIONS.md/AUDIT_FINDINGS.md never mention keyshare. The audit-gate that the project treats as the merge bar for tier-1 C code therefore does not see this binary at all; only the 45-vector unit test and a one-off manual ASan run cover it.

**Evidence.** recovery/Makefile:26 (SRCDIR = src/lcsas-restore), :544 (gcovr --filter '$(SRCDIR)/.*'); recovery/fuzz/ contains only fuzz_json_parse.c, fuzz_b64.c, fuzz_zstd_decode.c, fuzz_path_safe.c, fuzz_repo_strip_v2.c; grep 'keyshare' over recovery/docs/EXEMPTIONS.md and AUDIT_FINDINGS.md → no matches; .claude/skills/key-escrow/PLAN.md:86 (C5.3 marked [~]: 'Full coverage-c/EXEMPTIONS/fuzz integration of the new dir = documented follow-up').

**Existing partial coverage.** recovery/tests/test_keyshare.c runs in `make -C recovery test` (partial; CI not triggered by keyshare source paths)

**Verifier.** Verified recovery/Makefile:26 SRCDIR=src/lcsas-restore, gcovr --filter '$(SRCDIR)/.*' (~line 543); recovery/fuzz/ has exactly 5 harnesses, none for slip39; zero 'keyshare' in EXEMPTIONS.md/AUDIT_FINDINGS.md; audit-gate.yml paths list lcsas-restore/** only — changes to recovery/src/lcsas-keyshare/** do not trigger CI. Partial coverage exists: test_keyshare (45 vectors) is in the `make -C recovery test` TESTS list (Makefile:84), so it runs whenever recovery/tests/** or Makefile changes trigger CI — but never on keyshare source changes, and never under coverage/sanitize/fuzz.

**Fix.** Close C5.3: widen the coverage-c filter to include src/lcsas-keyshare (or add a parallel coverage target), add a libFuzzer harness over lcsas_keyshare_recover_password fed raw mnemonic text, list keyshare explicitly in EXEMPTIONS.md (even if only to state full coverage), and add the keyshare dir to the audit-gate path filter in .github/workflows/audit-gate.yml so PRs touching it trigger the gate.

**Tests / gates to add:**
- recovery/fuzz/fuzz_slip39_mnemonic.c + Makefile target fuzz-keyshare-smoke (60 s), included in fuzz-smoke and audit-gate
- recovery/Makefile coverage-c: --filter extended to src/lcsas-keyshare/.* with the same THRESHOLD; test_keyshare already runs under `make test` so instrumentation is free
- .github/workflows/audit-gate.yml paths: add recovery/src/lcsas-keyshare/**

#### 7. [MEDIUM] C combiner gives no actionable feedback on a typo'd share — and the advertised 4-letter-prefix property is unused

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** A holder's card carries 20 or 33 words the heir must retype perfectly. The Python path at least names the unrecognized word; the C binary — the python-free primary path — collapses every failure (under-threshold, typo, checksum, foreign share) into one generic message: 'failed to recover the password (insufficient, corrupt, or mismatched shares)'. There is no per-share validation mode ('share 2, word 7 "buidling" is not a word'), no indication WHICH card is bad when one of K is mistyped, and although KEY_SHARE_FORMAT.md advertises that every wordlist word is uniquely identified by its first 4 letters, no combiner accepts prefixes — exact full words only. For a non-technical heir, a single typo across 40+ typed words yields an undiagnosable dead end.

**Evidence.** recovery/src/lcsas-keyshare/main.c:179-181 (single generic error for all failures); src/lcsas/keyshare/slip39.py:180-184 (exact dict lookup; KeyShareError does name the unknown word — python only); docs/KEY_SHARE_FORMAT.md:36-38 ('1024 words, each uniquely identified by its first 4 letters').

**Verifier.** Verified main.c:177-181: single generic error for all failure modes. slip39.c word_to_index is an exact binary search (no strncmp/prefix logic); python _mnemonic_to_indices (slip39.py:181-184) is exact dict lookup but at least names the unknown word. KEY_SHARE_FORMAT.md:36-38 advertises unique 4-letter prefixes; no combiner accepts them. Reproduced the generic C error in finding-1 repro. Medium is right, borderline high given typing 40+ words from print is the real heir input mode; combined with finding 5 (C path is the python-free primary), worth prioritizing.

**Fix.** Add to the C combiner: (1) a pre-pass validating each share independently, reporting share-file + word position of the first unknown word and a per-share RS1024 checksum verdict ('share 1: OK / share 2: checksum FAILED — recheck your typing'); (2) 4-letter-prefix expansion at word lookup (the wordlist guarantees uniqueness); mirror both in slip39.py/keyshare_combine.py. Print the same hint text the Python combiner already has.

**Tests / gates to add:**
- recovery/tests/test_keyshare.c — cases: one mistyped word in share 2 of 2 → stderr names share 2 and the word index; valid 4-letter prefixes for every word of a valid set → recovery succeeds
- tests/unit/test_keyshare.py::test_prefix_words_accepted and ::test_checksum_error_names_share — same behavior in the Python implementation
- Cross-check gate: extend the existing C-vs-Python byte-match script to run prefix-typed inputs through both and assert identical outputs

#### 8. [MEDIUM] key_split/K/N on-disc instructions are self-reported config, never reconciled with the actual split

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** Whether discs print share instructions — and the K/N they claim — comes solely from hand-edited lcsas.toml fields (key_split, key_threshold, key_shares). `lcsas key split` accepts --threshold/--shares overrides but writes nothing back and prints no reminder to set key_split=true; the burn pipeline never checks consistency. Failure modes: owner splits but forgets key_split=true → every disc tells the heir to find a single key that may no longer exist anywhere; owner splits 3-of-5 via flags while config says 2 → discs tell the heir 'any 2 cards' and the combine fails after they've gathered exactly 2 (the card itself shows the true K, but the heir has to notice the contradiction); key_split=true with no split ever performed → heir hunts for nonexistent cards.

**Evidence.** src/lcsas/config/settings.py:47-52, 246-248 (free-floating defaults, no linkage); src/lcsas/cli/main.py:3219-3230 (flags override config; nothing persisted), 3281-3293 (success output has no 'set key_split=true' reminder); src/lcsas/staging/metadata.py:48-49, 350-351 (disc text rendered from config values only); docs/ESTATE_PLANNING.md:73-77 (setting key_split is a separate manual checklist item).

**Verifier.** Verified settings.py:246-251 (free defaults: key_threshold=2, key_shares=5, key_split=False, no linkage); cmd_key_split flags override config (main.py:3219-3230) with nothing persisted and no key_split reminder in success output (3281-3293); grep shows key_split/key_threshold appear ONLY in staging/metadata.py (render-time) — nothing in burn/ or db/, so no consistency check or recorded state exists. ESTATE_PLANNING.md:73-77 confirms it's a separate manual checklist step. All three failure modes are plausible. Medium correct.

**Fix.** Record split state durably at split time: cmd_key_split writes the actual K/N + SLIP-0039 identifier + date into the catalog (new key_escrow table or repositories columns) and prints a loud next-step reminder. At burn/stage time derive the split block from the recorded state, and fail (or warn loudly) when config.key_split disagrees with recorded state in either direction.

**Tests / gates to add:**
- tests/unit/test_cli_key.py::test_split_records_state — after split with --threshold 3, catalog/recorded K=3; KEY_INFO rendered from it says 'any 3'
- tests/unit/test_burn_orchestrator.py::test_burn_fails_on_escrow_drift — key_split=true with no recorded split (and the inverse) → burn aborts with actionable message
- tests/unit/test_staging_metadata.py::test_kn_comes_from_recorded_split_not_config

#### 9. [MEDIUM] UX_CONCERNS ID 006's promised Recovery Card template still doesn't exist; single-key printed artifacts remain manual homework

**CONFIRMED** (verifier confidence: high) — tracked: recovery/docs/UX_CONCERNS.txt ID 006 (mitigation bullet, unimplemented; item marked WONTFIX-CRYPTOGRAPHIC overall)

**Gap.** For the (default) single-key archive, the entire key-availability story is the owner manually transcribing the password per the ESTATE_PLANNING checklist. UX_CONCERNS ID 006 (rated 'IMPACT: HIGHEST ... the actual #1 failure mode') lists a concrete mitigation: 'a paper-printable Recovery Card template under docs/RECOVERY_CARD.txt' — the file does not exist and no generator produces a printable single-key sheet (location hints, key label, no QR/checksum on the transcribed password). The split-key path got generated cards; the more common single-key path got nothing machine-generated, so a hand-copied password has no transcription check at all.

**Evidence.** recovery/docs/UX_CONCERNS.txt:121-139 (ID 006 + RECOVERY_CARD.txt mitigation bullet); ls docs/RECOVERY_CARD.txt → No such file or directory; grep RECOVERY_CARD across repo → only the UX_CONCERNS mention; docs/ESTATE_PLANNING.md:41-53 (manual checklist, no generator).

**Existing partial coverage.** recovery/docs/UX_CONCERNS.txt ID 006 (mitigation bullet, unimplemented)

**Verifier.** Verified UX_CONCERNS.txt ID 006 (IMPACT: HIGHEST, '#1 failure mode', STATUS WONTFIX-CRYPTOGRAPHIC) lists 'paper-printable Recovery Card template under docs/RECOVERY_CARD.txt' as mitigation; repo-wide grep finds RECOVERY_CARD only in that one UX_CONCERNS line; docs/RECOVERY_CARD.txt does not exist; no generator for a single-key printable artifact anywhere. Split path got generated cards (cmd_key_split), single-key path got nothing. Medium correct: it's a missing promised mitigation, not a regression.

**Fix.** Ship the template plus a generator: `lcsas key card --repo R` renders a printable single-key recovery card (key-storage hints, repo name, KEY file name, a SHA-256-derived 4-char check code the heir can verify after typing the password — checkable offline via the bundled tools). Reference it from ESTATE_PLANNING.md and START_HERE.txt. Keep ID 006 open until done rather than buried under WONTFIX.

**Tests / gates to add:**
- tests/unit/test_cli_key.py::test_key_card_renders — card includes hints, check code; check code verifies against the password and fails against a typo
- tests/recovery_hardening/test_recovery_card_docs.py — static test pinning docs/RECOVERY_CARD.txt existence and ESTATE_PLANNING reference (pattern of test_env_var_docs.py)

#### 10. [LOW] KEY_SHARE_FORMAT.md falsely claims the wordlist and combiner are pinned in recovery/MANIFEST.sha256

**CONFIRMED** (verifier confidence: high) — tracked: .claude/skills/key-escrow/PLAN.md K2.1 documents the non-pinning decision, but not the contradicting doc claim

**Gap.** The 50-year re-implementation spec states the bundled wordlist.txt 'is pinned in recovery/MANIFEST.sha256 alongside the combiner'. Neither the wordlist nor any combiner artifact appears in MANIFEST.sha256 (grep finds nothing); PLAN.md K2.1 records the opposite decision ('the recovery/-rooted MANIFEST intentionally doesn't pin them'). An engineer decades out, told by the spec to verify provenance via the manifest, finds no entry — undermining trust in the one document meant to be authoritative, for an artifact (the 1024-word list) whose exact bytes are load-bearing for reconstruction.

**Evidence.** docs/KEY_SHARE_FORMAT.md:108-110 (pinning claim); grep -i 'keyshare|wordlist|combine' recovery/MANIFEST.sha256 → exit 1 (no matches); .claude/skills/key-escrow/PLAN.md:55 (K2.1: 'the recovery/-rooted MANIFEST intentionally doesn't pin them').

**Existing partial coverage.** .claude/skills/key-escrow/PLAN.md K2.1 (documents non-pinning decision, not the contradicting doc claim)

**Verifier.** Verified KEY_SHARE_FORMAT.md:108-110 claims wordlist.txt 'is pinned in recovery/MANIFEST.sha256 alongside the combiner'; grep -i 'keyshare|wordlist|combine' over recovery/MANIFEST.sha256 → rc=1, no matches; PLAN.md K2.1 explicitly records 'the recovery/-rooted MANIFEST intentionally doesn't pin them (git-pinned + 45-vector guarded)'. Doc contradicts the deliberate decision. Low correct: provenance/trust polish, not a functional break — the 45 official vectors do guard wordlist bytes in tests.

**Fix.** Either actually pin wordlist.txt + keyshare_combine.py (+ the per-target lcsas-keyshare bins, like other shipped artifacts) in a manifest the meta-builder copies, or correct KEY_SHARE_FORMAT.md §5 to state the real provenance (git-pinned, guarded by the 45 official vectors). Pinning is preferable: the wordlist's bytes are part of the cryptographic contract.

**Tests / gates to add:**
- tests/unit/test_keyshare_manifest.py — parse KEY_SHARE_FORMAT.md for 'pinned in' claims and assert each named artifact has a matching, hash-valid line in the named manifest
- If pinning is chosen: extend the existing MANIFEST verification step (READINESS_CHECKLIST 'sha256sum -c') to cover the keyshare artifacts and add wordlist.txt hash equality between src/lcsas/keyshare/wordlist.txt and recovery/src/lcsas-keyshare/wordlist.c (generated-from check)

---

## Python restore path & fallbacks (planner, executor, tier-3, meta-volume)  `restore-python`

> The Python restore path has solid bones (alternates-aware planner, hash-verify-on-ingest, cache completeness checks, a genuinely self-contained pure-Python crypto stack that matches the restic spec for AES-CTR/Poly1305-AES/scrypt) but the failure-path bookkeeping is where it breaks for a non-technical user. The single worst find is reproducible and deterministic: `lcsas restore standalone` aborts every multi-disc restore with 'packs could not be recovered from any disc' AFTER successfully ingesting everything, because successfully-ingested packs are never pruned from the failure list — and the unit tests mask it by mocking the executor. Beyond that, tier-3 (and tier-1) misclassify uncompressed blobs that begin with the zstd magic, one bad blob aborts an entire hours-long tier-3 restore with no partial-result manifest, tier-3 zstd support is silently build-host-specific while default rustic repos are zstd-compressed, and the meta-volume has no bundle-completeness gate (verify is self-referential, missing per-target binaries are silently skipped). A known-dead symlink-escape guard was documented in a skipped test rather than fixed.

#### 1. [HIGH] `lcsas restore standalone` multi-disc restore deterministically aborts with 'packs could not be recovered from any disc' even after every pack was successfully ingested

**CONFIRMED** (verifier confidence: high) *(finder said critical; verifier corrected)* — **untracked** (not in any known-issues ledger)

**Gap.** In cmd_restore_from_disc, the initial-disc ingest is given ALL required pack hashes with collect_failures=True, so every pack that lives on a later disc is appended to all_failed (executor.ingest_volume records 'not found on this volume' as a failure). The per-volume loop then ingests those packs successfully, but nothing ever prunes all_failed. _retry_from_alternates_batch/_interactive only clears packs that have catalog alternates; in the common single-copy layout (each pack on exactly one volume, alternates_map empty) every off-initial-disc pack survives to still_failed and PackCorruptionError is raised — AFTER the cache is complete and verified. main() catches it and prints 'Unexpected error: N packs could not be recovered from any disc' and exits 1. A non-technical heir restoring any snapshot that spans more than one disc via the documented disc-only path will conclude the discs are damaged and the data is gone. Both --volume-dir batch mode and interactive mode are affected. Reproduced end-to-end with the real RestoreExecutor and a real catalog (2 packs, 2 volumes, no alternates): the restore cache was complete, yet the command raised PackCorruptionError and never ran the restore.

**Evidence.** src/lcsas/cli/main.py:2782-2788 (initial disc ingest with plan.required_pack_hashes + all_failed.extend(result.failed)); src/lcsas/cli/main.py:2810-2820 (batch: 'if all_failed: ... raise PackCorruptionError(f"{len(still_failed)} packs could not be recovered from any disc")'); src/lcsas/cli/main.py:2832-2839 and 2886-2896 (interactive path, same pattern); src/lcsas/restore/executor.py:307-315 ('Pack not found on this volume ... failed.append(sha256)'); src/lcsas/cli/main.py:2944-2999 (_retry_from_alternates_batch never consults the cache, only alternates_map). Reproduction script /var/tmp/audit_repro/repro_from_disc.py output: 'RESULT: UNHANDLED PackCorruptionError: 1 packs could not be recovered from any disc' with pack B physically present and verified in the cache. Existing unit tests mask the bug by mocking the executor: tests/unit/test_restore_from_disc.py:280 and :328 return IngestionResult(N, []) so failed is never populated.

**Verifier.** Reproduced live: /var/tmp/audit_repro/repro_from_disc.py raises PackCorruptionError with pack B verified-present under --volume-dir. cli/main.py:2782-2820 seeds all_failed with every off-initial-disc pack; nothing prunes it; alternates retry no-ops when alternates_map is empty. Interactive path (2832-2896) identical. tests/unit/test_restore_from_disc.py:280/328 mock IngestionResult(N,[]) so CI never sees it; blind-restore e2e drives restore.sh, not this CLI. Downgraded critical→high: data intact, restore.sh tier-1/2/3 cascade (the heir's primary START_HERE journey) unaffected; this is Workflow C, requiring installed lcsas+rustic. Critical defensible if Workflow C counts as a primary heir path.

**Fix.** Before the alternates retry (and again before raising), prune all_failed against RestoreExecutor.verify_cache_completeness(cache_dir, all_failed): a pack already present and hash-valid in the cache is recovered, regardless of which volume supplied it. Only raise PackCorruptionError for packs genuinely absent from the cache after all sources are exhausted, and print the volume LABELS (from the pick list) for those packs, not just hashes.

**Tests / gates to add:**
- tests/unit/test_restore_from_disc.py::test_batch_multidisc_single_copy_returns_0 — real RestoreExecutor (no MagicMock), catalog with pack A on VOL_001 (initial disc) and pack B only on VOL_002 under --volume-dir, no alternates; assert exit code 0 and execute_restore called
- tests/unit/test_restore_from_disc.py::test_interactive_multidisc_no_spurious_alternate_prompts — monkeypatched input(); assert no _retry_from_alternates_interactive prompt fires when the cache is complete
- tests/e2e: extend tests/e2e/cdemu_blind_restore or add a non-agent e2e that drives `lcsas restore standalone --volume-dir` against a real 2-volume staged archive (the current blind-restore gates exercise recovery/scripts/restore.sh, not this CLI path)

#### 2. [HIGH] Tier-3 _read_blob misclassifies uncompressed blobs that begin with the zstd magic, raising IntegrityError and aborting the whole restore (tier-1 C shares the flaw)

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** restic/rustic repo-v2 'auto' compression stores incompressible blobs UNCOMPRESSED, and the index marks compression solely via the presence of uncompressed_length. restic_fallback._read_blob instead sniffs plaintext[:4] for the zstd magic and decompresses unconditionally. Any archived file that is itself zstd data (.zst, .tar.zst, zstd-framed assets — its first chunk starts with 0x28B52FFD) is stored uncompressed, gets falsely decompressed, fails the SHA-256 content check, and raises IntegrityError — which aborts the ENTIRE tier-3 restore (finding 3), not just that file. The project's own spec doc encodes the wrong rule ('check if it starts with the zstd magic'). The tier-1 C binary has the identical sniff in repo.c, so tiers 1 and 3 both fail on such archives; only tier-2 (upstream rustic) handles them. Reproduced: a valid uncompressed blob whose content is a zstd frame raises 'Blob content hash mismatch'.

**Evidence.** src/lcsas/restore/restic_fallback.py:961-963 ('if plaintext[:4] == _ZSTD_MAGIC: ... _decompress_zstd') with loc.uncompressed_length used only as a size hint, not as the discriminator; docs/RESTIC_FORMAT_SPEC.md:179-180 ('check if it starts with the zstd magic bytes ...; if so, decompress'); recovery/src/lcsas-restore/repo.c:927-939 (same magic sniff: 'Inline pack blobs in restic v2 are zstd-compressed (no prefix byte)' — decompresses whenever magic matches, uses uncompressed_length only when >0). Live reproduction in this audit: _read_blob on an uncompressed blob whose bytes are a zstd frame → 'FAILS: IntegrityError raised on a perfectly valid blob: Blob content hash mismatch'. tests/unit/test_restic_fallback.py:1480-1500 covers only the compressed case (uncompressed_length set); no fixture for an uncompressed blob starting with the magic.

**Verifier.** Confirmed: restic_fallback.py:961-963 decompresses whenever plaintext[:4]==magic, using loc.uncompressed_length only as a size hint (index parser does record it, line 577, so the correct discriminator is available but unused). repo.c:927-939 has the identical sniff; RESTIC_FORMAT_SPEC.md:178-180 encodes the wrong rule. No uncompressed-with-magic fixture in tests/unit/test_restic_fallback.py (1107-1150 cover compressed only) or recovery/tests. Trigger requires an archived zstd-framed file stored uncompressed (restic auto-compression or compression-off repos) — format-legal, so tiers 1+3 both false-reject and (per finding 3) abort. High stands.

**Fix.** Gate decompression on loc.uncompressed_length is not None (the restic index contract), falling back to magic-sniff only when the index entry is silent AND the SHA-256 of the raw plaintext does NOT already match blob_id (try raw hash first — it is authoritative). Apply the same fix to repo.c read_blob and correct docs/RESTIC_FORMAT_SPEC.md §4.5.

**Tests / gates to add:**
- tests/unit/test_restic_fallback.py::test_uncompressed_blob_with_zstd_magic_roundtrips — store a blob whose content is a real zstd frame with uncompressed_length=None; assert _read_blob returns it verbatim
- recovery/tests: add a fixture repo containing a .zst file stored uncompressed; assert tier-1 lcsas-restore restores it byte-identical (wire into make -C recovery test)
- tests/unit/test_restic_fallback.py::test_compressed_blob_without_index_hint — compressed blob with uncompressed_length present must still decompress (regression guard for the fix)

#### 3. [HIGH] One corrupt/missing blob aborts the entire tier-3 restore — no skip-and-continue, no per-file failure summary

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** PurePythonRestorer._restore_tree/_restore_file have no error tolerance: IntegrityError (corrupt blob), KeyError (blob missing from index), or FileNotFoundError (pack not found after the swap-prompt retries are exhausted) propagates up and kills the whole restore, possibly hours in at ~1 MB/s, leaving a partially written file and no record of what was already restored or what remains. cmd_restore_from_disc catches it and prints 'Pure-Python restore failed: <exc>' (exit 1); the standalone_restorer.py CLI block doesn't catch it at all, so the heir sees a raw traceback. For the fail-proof goal, a last-resort restorer should restore everything restorable and end with an explicit manifest of failed paths — 99.9% of a family archive is vastly better than 0%.

**Evidence.** src/lcsas/restore/restic_fallback.py:977-1060 (_restore_tree: no try/except around self._restore_file(node, node_path) or self._read_blob); :1062-1074 (_restore_file writes chunks directly to the final path — an exception mid-file leaves a truncated file with no marker); :936-973 (_read_blob raises KeyError/IntegrityError); src/lcsas/cli/main.py:2657-2664 ('except Exception as exc: logger.error("Pure-Python restore failed: %s", exc); return 1'); src/lcsas/restore/standalone_builder.py:128-263 (_CLI_BLOCK has no exception handling around restorer.restore()).

**Verifier.** Confirmed: _restore_tree/_restore_file (restic_fallback.py:977-1074) have no per-node error handling; restore() (line 429-476) calls _restore_tree bare; _restore_file writes to the final path so an exception mid-file leaves a truncated file. cli/main.py:2657-2664 catches and exits 1; standalone_builder.py _CLI_BLOCK line 259 calls restorer.restore() with no try/except — raw traceback for the heir. Missing-pack has partial tolerance via the interactive disc-swap prompt, but corrupt-blob (IntegrityError) and KeyError have none. No existing test of skip-and-continue. High fits the bar (one bad blob → 0% restored on the last-resort tier).

**Fix.** Add a tolerant mode (default ON for the CLI): catch IntegrityError/KeyError/OSError per file node, record (path, blob_id, reason), write to <target>/RESTORE_FAILURES.txt, continue traversal, and exit non-zero with a clear count ('Restored 9,742 of 9,745 files; 3 failures listed in RESTORE_FAILURES.txt'). Write files to a temp name and rename on success so partial files are never mistaken for complete ones.

**Tests / gates to add:**
- tests/unit/test_restic_fallback.py::test_restore_continues_past_corrupt_blob — repo fixture with 3 files, middle file's data blob corrupted in the pack; assert other 2 files restored byte-identical, exit/raise carries a failure list naming the bad path
- tests/unit/test_restic_fallback.py::test_partial_file_not_left_behind — corrupt second chunk of a 2-chunk file; assert no truncated file remains at the final path
- tests/recovery_hardening: static test asserting standalone_restorer.py's _CLI_BLOCK wraps restore() and prints the failure-manifest path (same style as test_disc_swap_docs.py)

#### 4. [HIGH] Tier-3 zstd support only works on the build host's arch + CPython minor version; silently absent when zstandard isn't installed at meta-build time — default rustic v2 repos then unrestorable by tier 3

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** rustic v2 repos are zstd-compressed by default, and tier-3 has no pure-Python zstd path: _decompress_zstd raises 'pip install zstandard' — useless advice on an air-gapped machine decades from now. The meta builder copies the BUILD HOST's zstandard package (with its cpXY-arch-specific backend_c .so) into tools/lib/pythonX.Y/, and restore.sh only re-exposes that same directory. The six per-target python-build-standalone CPython 3.12.13 trees bundled under recovery/bin/<target>/python ship no zstandard at all, and PBS 3.12 has no stdlib zstd (compression.zstd landed in 3.14). So tier-3 zstd works only when the recovery host matches the build host's arch AND CPython minor (today: x86_64-linux + 3.12 — which is exactly why the blind-restore e2e passes and masks this). On the other five approved targets, or after the build host upgrades to 3.13+, tier-3 cannot decrypt-then-decompress any default-format repo. Additionally, bundle_python_package returns None silently when zstandard isn't installed on the build host and _bundle_tools ignores the return value — the meta-volume builds 'successfully' with no zstd support and no warning.

**Evidence.** src/lcsas/restore/restic_fallback.py:101-110 (ImportError → RuntimeError 'Install it with: pip install zstandard'); src/lcsas/meta/builder.py:1799-1801 (bundler.bundle_python_package("zstandard") — return value discarded); src/lcsas/meta/bundler.py:272-274 ('if pkg_dir is None: return None' — silent) and :283-296 (copies host site-packages incl. C-extension .so verbatim); recovery/scripts/restore.sh:177-191 (issue #284 fix copies only tools/lib/python*/zstandard — the host-arch copy); recovery/UPSTREAM.sha256:29-35 (pinned CPython 3.12.13 per target, no third-party packages); build host check this audit: Python 3.12.3 with zstandard at ~/.local/lib/python3.12 — coincidentally ABI-compatible with the pinned 3.12 PBS trees.

**Existing partial coverage.** tests/recovery_hardening/test_tier3_pythonpath.py + test_standalone_zstandard_guard.py (local-only, host-arch/graceful-degrade only — not cross-target)

**Verifier.** All evidence confirmed: restic_fallback.py:101-110 ('pip install zstandard'), builder.py:1801 discards bundle_python_package return, bundler.py:272-274 silent None, restore.sh:177-191 copies only the host-arch tools/lib copy, UPSTREAM.sha256 pins bare PBS CPython 3.12.13 (no zstd; stdlib zstd is 3.14+). restore.sh's tier-3 comment block itself admits the '3.12 ↔ 3.12' build-host coincidence. Build host today: 3.12.3 + x86_64 zstandard — exactly the masking configuration. Partial local-only coverage: test_tier3_pythonpath.py (host target only) and test_standalone_zstandard_guard.py (degrade-gracefully only); neither covers the other five targets.

**Fix.** Two-part fix: (1) make the gap loud — _bundle_tools must fail (or require --allow-no-zstd) when bundle_python_package returns None, and record in volume_info.json which targets have working zstd; (2) close it — vendor a pure-Python zstd decompressor (the project already maintains a from-scratch C zstd_dec at 100% coverage; port it) as a fallback backend in restic_fallback, or bundle per-target zstandard wheels pinned in UPSTREAM.sha256.

**Tests / gates to add:**
- tests/unit/test_meta_builder.py::test_build_fails_when_zstandard_unbundleable — monkeypatch _find_installed_package to return None; assert MetaVolumeBuilder.build() raises instead of silently succeeding
- tests/recovery_hardening/test_tier3_zstd_portability.py — for each recovery/bin/<target>/python tree on a built meta-volume, assert a zstd backend importable by THAT interpreter exists (static check of cpXY/arch tags in bundled .so names vs target triple)
- Makefile blind-restore variant (qemu aarch64, like the existing cross-arch verify in recovery_c_build_and_audit_gate workflow): run standalone_restorer.py under the bundled aarch64 PBS python against a zstd-compressed fixture repo; assert restore succeeds

#### 5. [HIGH] No meta-volume completeness gate: missing per-target tier-1/tier-2/python binaries are silently skipped at build and `lcsas meta verify` is self-referential, so an incomplete rescue disc passes every check

**CONFIRMED** (verifier confidence: high) — tracked: Partially adjacent: recovery/docs/READINESS_CHECKLIST.txt:23 (manual META DISC BOOT TEST) and UX_CONCERNS ID 001 (Windows path) — but no ledger entry tracks the absence of a bundle-completeness gate itself.

**Gap.** MetaVolumeBuilder treats every per-target artifact as optional: missing upstream cache → return (no warning); each absent lcsas-restore short-arch build → continue; the silent-skip behavior is even codified as expected in unit tests. _regenerate_recovery_manifest then rebuilds MANIFEST.sha256 bin/ rows FROM whatever happened to be bundled, so `lcsas meta verify` (and `sha256sum -c`) validates an incomplete bundle as PASS — it pins what WAS shipped, not what SHOULD ship. Files outside recovery/ (standalone_restorer.py, keyshare_combine.py, tools/, restore.bat at root) are in no manifest at all. Net effect: an operator can burn a 'rescue' meta disc that lacks the Windows/macOS/aarch64 binaries (UX_CONCERNS ID 001's whole mitigation) or lacks tier-1 entirely, with zero failing gate; the heir on that platform discovers it decades later. The only completeness-adjacent check is the manual 'META DISC BOOT TEST' checklist item, which tests boot, not bundle contents.

**Evidence.** src/lcsas/meta/builder.py:1962-1965 ('Missing per-arch binaries are silently skipped'); :2041-2042 ('if not cache_root.is_dir(): return'); :2156-2157 ('if not src_bin.is_file(): continue'); :2172-2233 (_regenerate_recovery_manifest recomputes bin/ rows from disk contents); src/lcsas/cli/main.py:1976-2070 (cmd_meta_verify checks only recovery/MANIFEST.sha256 entries; root-level restore artifacts unverified); tests/unit/test_meta_builder.py:564 (test_no_cache_dir_is_silent_skip pins the silent skip as intended); recovery/docs/READINESS_CHECKLIST.txt:23-30 (boot test is manual, content-blind).

**Existing partial coverage.** tests/recovery_hardening/test_meta_bundling_completeness.py — tier-1 binaries only, source-tree based, local-only (not in always-on CI)

**Verifier.** Core claim verified: builder.py 1962-1965/2041-2042/2161-2162 silently skip; _regenerate_recovery_manifest (2172-2233) rebuilds bin/ rows from disk so cmd_meta_verify (cli/main.py:1976-2070, recovery/ only, --strict catches extras not absences) passes incomplete bundles; test_no_cache_dir_is_silent_skip pins the skip. One inaccuracy: tests/recovery_hardening/test_meta_bundling_completeness.py ALREADY gates all 6 tier-1 binaries — but it checks the source tree (not the built meta output), is local-only (CI test.yml runs unit+integration only; audit-gate runs 3 other files), and covers neither upstream rustic/python trees nor root artifacts. High stands (silent integrity gap on the rescue disc).

**Fix.** Add a required-contents contract: a static REQUIRED list (6 rust-triples × {lcsas-restore[.exe], rustic-static[.exe], python tree} + root artifacts standalone_restorer.py, keyshare_combine.py, restore.sh/restore.bat, START_HERE.txt). `lcsas meta build` gets --require-complete (default ON for release builds) that fails listing every missing artifact; `lcsas meta verify` gains the same list so verification of an old disc also reports gaps, separately from hash mismatches.

**Tests / gates to add:**
- tests/unit/test_meta_builder.py::test_require_complete_fails_listing_missing_targets — build with empty cache + --require-complete; assert raised error enumerates all 6 triples
- tests/unit/test_meta_builder.py::test_meta_verify_reports_absent_required_artifacts — delete recovery/bin/x86_64-pc-windows-gnu from a built tree; assert cmd_meta_verify returns 1 naming it (today it returns 0)
- Makefile target meta-gate (wired into release docs / .github/workflows/test.yml on tags): lcsas meta build --require-complete into a temp dir after make fetch-recovery + build-recovery, then lcsas meta verify --strict

*Verifier refinements:*
- Extend tests/recovery_hardening/test_meta_bundling_completeness.py (don't duplicate it) to assert the BUILT meta-volume output contains, per approved triple: lcsas-restore[.exe], rustic-static[.exe], python tree — plus root artifacts standalone_restorer.py, keyshare_combine.py, restore.sh/restore.bat, START_HERE.txt
- tests/unit/test_meta_builder.py::test_meta_verify_reports_absent_required_artifacts — delete recovery/bin/x86_64-pc-windows-gnu from a built tree; assert cmd_meta_verify returns 1 naming it (today it returns 0)
- Wire the completeness test into always-on CI: add tests/recovery_hardening/test_meta_bundling_completeness.py to .github/workflows/test.yml (currently only run via local make test-recovery-hardening)

#### 6. [MEDIUM] Symlink escape guard in tier-3 restore is dead code — out-of-bounds symlinks are created despite the code claiming to skip them; the test that would catch it is @pytest.mark.skip'd acknowledging the dead branch

**CONFIRMED** (verifier confidence: high) — tracked: Only as the skip-reason text in tests/unit/test_restic_fallback.py:1584-1592 — not in UX_CONCERNS.txt, DEFERRED_WORK.txt, or AUDIT_FINDINGS.md, and the production bug is unfixed.

**Gap.** _restore_tree intends to skip relative symlinks that resolve outside the restore target, but calls resolved.is_relative_to(target_dir.resolve()) and discards the boolean inside a try/except ValueError — is_relative_to never raises ValueError, so the 'continue' branch is unreachable and every escaping relative symlink is created. Reproduced: a node with linktarget '../../../../etc' produced a live symlink resolving to /etc-equivalent outside the target. The corresponding unit test exists but is skipped with a reason that explicitly states the branch is dead code — the dead guard was documented instead of fixed, and the separate test_restic_fallback_path_traversal.py tests a hand-rolled relative_to() re-implementation rather than the production code, giving false assurance. Risk: a tampered or attacker-influenced repo restored as root (the live-boot wizard runs as root) can plant symlinks anywhere; subsequent writes/copies through them escape the restore sandbox. The same generated code ships in standalone_restorer.py on every disc.

**Evidence.** src/lcsas/restore/restic_fallback.py:1040-1049 ('resolved.is_relative_to(target_dir.resolve())' result unused; 'except ValueError: ... continue' unreachable); live repro this audit: symlink 'escape' created pointing to ../../../../etc, resolving outside target; tests/unit/test_restic_fallback.py:1584-1592 (@pytest.mark.skip reason: 'is_relative_to() returns bool (never raises ValueError) ... dead code'); tests/unit/test_restic_fallback_path_traversal.py:44-64 (tests relative_to(), not the production function).

**Verifier.** Confirmed: restic_fallback.py:1040-1049 discards the is_relative_to() bool inside try/except ValueError; Path.is_relative_to returns bool and never raises ValueError, so the continue branch is unreachable and escaping relative symlinks are created. tests/unit/test_restic_fallback.py:1585-1603 is skipped with a reason explicitly stating the branch is dead; test_restic_fallback_path_traversal.py:44-64 tests a hand-rolled relative_to() re-implementation, not production code. Same code ships in generated standalone_restorer.py. Medium is right: exploitation requires a repo writer holding the key (blobs are Poly1305-MAC'd), and restic itself restores symlinks verbatim — the harm is a guard that claims protection it doesn't provide.

**Fix.** Change to `if not resolved.is_relative_to(target_dir.resolve()): _log(...); continue` (drop the try/except), regenerate standalone_restorer.py, and un-skip test_symlink_escaping_target_skipped. Decide and document the policy for legitimate cross-tree symlinks (restic restores them verbatim; if fidelity is preferred, create them but log loudly — either way the guard must do what it claims).

**Tests / gates to add:**
- Un-skip tests/unit/test_restic_fallback.py::TestRestoreTreeSecurityPaths::test_symlink_escaping_target_skipped and make it assert the production behavior (currently skipped)
- tests/unit/test_restic_fallback_path_traversal.py: replace the logic re-implementation tests with ones that call PurePythonRestorer._restore_tree on crafted tree blobs (the audit repro is a ready-made template)
- tests/recovery_hardening: equivalent symlink-escape fixture run against the GENERATED standalone_restorer.py via subprocess (extends tests/unit/test_standalone_subprocess.py)

#### 7. [MEDIUM] Interactive restore: typing 'skip' for a missing disc silently discards the alternates machinery, and final missing-pack errors print SHA-256 hashes instead of disc labels

**CONFIRMED** (verifier confidence: high) — tracked: Adjacent: recovery/docs/UX_CONCERNS.txt ID 005 (CLOSED — but only for the tier-1 lcsas-restore prompt path); CODE_REVIEW_CLEANUP.md:71 unchecked 'Restore from damaged/incomplete discs'. The CLI-path gap itself is untracked.

**Gap.** The planner builds PickListV2 with per-pack alternate volumes precisely for the lost/damaged-disc case, but in cmd_restore_exec's interactive loop, answering 'skip' to 'Mount volume X' just breaks out — the skipped volume's packs are never added to all_failed, so _retry_from_alternates_interactive never offers the alternate discs that the catalog knows about. The restore then dies at the completeness check printing up to 10 bare pack hashes and 'mount the missing volumes and retry' — with no volume label, so a non-technical user who skipped DISC_007 because it is lost is shown 64-char hex strings and no actionable next step, even when DISC_012 holds redundant copies. The same hash-only final error appears in cmd_restore_from_disc:2901-2913. (UX_CONCERNS ID 005 fixed this class of problem for the tier-1 prompt; the LCSAS-CLI journey was not covered.)

**Evidence.** src/lcsas/cli/main.py:2390-2397 (skip → break; packs not queued for alternates), 2425-2433 (alternates retry fed only from all_failed), 2444-2460 (completeness failure prints 'missing: <sha>' and 'mount the missing volumes' with no labels); src/lcsas/cli/main.py:2858-2865 (restore standalone interactive skip, same pattern); planner alternates exist and are populated: src/lcsas/restore/planner.py:95-136, src/lcsas/db/queries.py:198-261. cmd_restore_plan uses generate_pick_list (v1) so the printed plan never shows alternates either (cli/main.py:2120-2140).

**Verifier.** Confirmed: cli/main.py:2395-2397 skip→break with no enqueue into all_failed, so _retry_from_alternates_interactive (fed only from all_failed, 2426-2433) never offers catalog-known alternates; completeness failure (2448-2460) prints bare hashes + 'mount the missing volumes' with no labels; same in cmd_restore_from_disc (2863-2865, 2901-2913). cmd_restore_plan uses v1 generate_pick_list (2121-2122) so plans never show alternates. UX_CONCERNS ID 005 is CLOSED but covered only the tier-1 prompt. No existing test exercises interactive skip. Medium correct (lost-disc journey friction; redundant copies exist but are never offered).

**Fix.** On 'skip', enqueue that volume's pack hashes into all_failed so the alternates flow fires, and have the alternates prompt say which packs/labels are substitutable. Map any finally-missing hashes back to their pick-list volume labels (and deprecated labels) in the closing error: 'still need disc DISC_007 (or its alternate DISC_012) for 14 packs'.

**Tests / gates to add:**
- tests/unit/test_cli_restore.py::test_skip_primary_volume_triggers_alternate_prompt — monkeypatched input() answering 'skip' then the alternate's mount path; real executor; assert restore completes from the alternate
- tests/unit/test_cli_restore.py::test_final_missing_error_names_volume_labels — skip everything; assert stderr contains the volume label(s), not only hashes
- tests/unit/test_restore.py: assert `lcsas restore plan` output includes an 'also on:' alternates column when packs have multiple volumes (switch plan to generate_pick_list_v2)

#### 8. [LOW] Tier-3 hardlink reconstruction is per-directory only — cross-directory hardlinks are silently materialized as full copies, inflating restore size

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** hardlink_map is a local variable created fresh on every _restore_tree call (i.e., per directory), so two hardlinked files in different directories are each fully re-extracted instead of linked. Content is correct, but hardlink-heavy archives (rsnapshot/Time-Machine-style trees, maildirs) can balloon many-fold on restore, risking ENOSPC partway through a multi-hour tier-3 run — which, combined with the no-skip-and-continue behavior (finding 3), aborts the restore. The heir has no warning that the target needs more space than the snapshot's logical size.

**Evidence.** src/lcsas/restore/restic_fallback.py:983 ('hardlink_map: dict[int, Path] = {}' inside _restore_tree) vs :1027 (recursion into subtrees creates a new map per call); :1004-1022 (dedup logic keyed on that per-call map). No test covers cross-directory hardlinks (tests/unit/test_restic_fallback.py hardlink tests are same-directory; TestHardlinkOSErrorFallback only covers the OSError fallback).

**Verifier.** Confirmed: hardlink_map created fresh inside _restore_tree (restic_fallback.py:983); recursion into subtrees (line 1027) gets a new map, so cross-directory hardlinks are re-extracted as full copies. Content correct, size inflated; worst case ENOSPC mid-restore for hardlink-heavy trees. Existing hardlink tests are same-directory only (TestHardlinkOSErrorFallback covers the OSError fallback). Low is fairly calibrated for typical family archives; proposals are appropriate.

**Fix.** Thread one shared dict[int, Path] through the traversal (pass as parameter, key by (device, inode) if available), and keep the existing OSError fall-through to copying. Regenerate standalone_restorer.py.

**Tests / gates to add:**
- tests/unit/test_restic_fallback.py::test_hardlink_across_directories_restored_as_link — fixture with same inode + links=2 in two sibling dirs; assert os.stat st_nlink == 2 and only one copy's bytes on disk
- tests/unit/test_restic_fallback.py::test_hardlink_count_matches_node_links — regression for the shared-map refactor

#### 9. [LOW] standalone_restorer.py claims 'Python ≥ 3.10' but uses `from datetime import UTC` (3.11+) — ImportError at startup on the very systems the claim invites

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** The standalone builder's module docstring and the generated header advertise that the script runs 'with nothing but Python ≥ 3.10 stdlib'. restic_fallback.py (concatenated verbatim into the script) does `from datetime import UTC` at import time; datetime.UTC was added in Python 3.11, so on a 3.10 interpreter the heir's first command dies with ImportError before any help text. Tier-3's whole pitch is running on whatever system Python exists when the bundled interpreters won't; an off-by-one version claim costs a non-technical user the path with a cryptic traceback. The subprocess tests only exec the script under the dev interpreter (3.12), so the claim is never validated against the stated floor.

**Evidence.** src/lcsas/restore/standalone_builder.py:6 ('can run with nothing but Python ≥ 3.10 stdlib'); src/lcsas/restore/restic_fallback.py:49 ('from datetime import UTC' — module level, survives _strip_header into the generated file); tests/unit/test_standalone_subprocess.py:176+ run via sys.executable only.

**Verifier.** Confirmed: standalone_builder.py:6 claims '≥ 3.10 stdlib'; restic_fallback.py:49 has module-level `from datetime import UTC` (3.11+); verified the import survives into build_standalone() output. Worse than cited: the heir-facing docs/workflows/restore-disc-only.md claims 'Python 3.10+' in five places (lines 4, 101, 131, 160, 470). pyproject requires-python >=3.11, and tests exec only under sys.executable (3.12), so the 3.10 floor is never validated. Low correct — trivial fix, narrow trigger window; recommendation to lower the floor via timezone.utc is sound.

**Fix.** Replace `from datetime import UTC` with `from datetime import timezone` + `timezone.utc` (3.2+), or update every claim to 'Python ≥ 3.11' (header, docstrings, restore.sh tier-3 messaging) — preferably the former: lower floor = more survivable. Add a startup version check that prints a plain-English message instead of a traceback when the interpreter is too old.

**Tests / gates to add:**
- tests/unit/test_write_standalone.py::test_generated_script_has_no_post_310_syntax_or_apis — ast-walk the generated script for known >3.10 APIs (datetime.UTC, etc.) or, where available, `python3.10 -m py_compile` the generated file (skip if 3.10 absent)
- tests/unit/test_standalone_subprocess.py::test_old_python_gets_friendly_version_error — monkeypatch sys.version_info guard and assert the plain-English message

**Refuted in verification (recorded for honesty):**
- ~~[medium] `consolidate --deprecate` marks source volumes DEPRECATED with no verification that their packs exist anywhere else~~ — Refuted. deprecate_sources (merger.py:95-97) calls update_status with force=False, and update_status (db/volumes.py:105-145) enforces both VALID_TRANSITIONS (BURNED/BURNING volumes cannot be deprecated at all) and the Phase 19.4 guard: check_deprecation_safe (volumes.py:241-265) raises ValueError when any non-pruned pack on the volume lacks a copy on another BURNED/VERIFIED volume — exactly the stranding check the finding claims is absent. Always-on CI unit tests cover it (test-unit runs in test.yml). Residual truths — no 'type yes' prompt, ValueError surfaces unhandled — are polish, not stranding.

---

## Systems-level FMEA (catalog state machine, long-horizon controls)  `failure-modes`

> The restore-side engineering (tier-1 binary, freshest-catalog selection, ECC layer, blind-restore drills) is notably hardened, but the burn-side catalog state machine has several FMEA holes where the catalog can confidently lie: packs count as 'archived' the moment they touch a never-burned STAGING volume (a stage-then-clean or failed-burn path silently strands data on the NAS), post-burn 'verification' never compares disc content to the recorded ISO hash, and a verify-FAILED disc is still recorded as an ACTIVE copy that satisfies location redundancy. Most striking, the schema migration machinery is dead code — nothing in production ever calls migrate(), and the project's own live catalog is at v5 with CHECK constraints that crash burn_session exactly when a re-burn verification fails. Long-horizon controls are also thinner than documented: there is no tooled disc-rot re-verification (verify --all checks ISOs that are deleted after burn; last_verified_at is never written), no per-disc blast-radius report, and catalog rebuild from a mixed-generation disc box is order-dependent and resurrects DESTROYED volumes. Each gap has a concrete, unit-testable fix; none require new architecture.

#### 1. [CRITICAL] Packs linked to a never-burned STAGING volume are counted 'archived' forever; cleaning an unburned session silently strands data on the NAS

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** get_unarchived_packs() treats a pack as archived the moment any volume_packs row exists, regardless of volume status or physical copies. clean_session() deletes the ISOs and staging tree but leaves the volume rows and volume_packs links intact, with the volume stuck at STAGING. Failure combo: stage -> burn fails (status reset to STAGING by the except path) or is never run -> operator runs session clean to free the staging SSD -> the packs are never burned, never re-selected by the default 'lcsas stage', and 'lcsas status' reports them archived. If the NAS then dies, the data is gone while the catalog claimed it was on disc; an heir's pick list will even name the ghost volume label (get_pick_list only excludes DEPRECATED/DESTROYED, so STAGING volumes are offered as restore sources) and they will hunt for a disc that was never burned. The only accidental escape hatch is the location-targeted workflow (stage --for-location uses ACTIVE-copy queries), which the default path does not use.

**Evidence.** src/lcsas/db/queries.py:20-48 (archived == EXISTS volume_packs row, no status/copy check); src/lcsas/burn/orchestrator.py:813-830 (clean_session removes ISOs/staging, never delete_volume — contrast abort() at 314-326 which does); orchestrator.py:772-787 (burn failure resets volume to STAGING, links kept); queries.py:163-184 (pick list includes STAGING volumes); cli/main.py:909-933 (status reports these packs as archived).

**Verifier.** Confirmed at every cited line. get_unarchived_packs/get_archive_status_summary (queries.py:20-48, 420-443) test only volume_packs existence — no volume status or copy check. clean_session (orchestrator.py:813-830) deletes ISOs+staging but keeps volume rows and links; CLI `stage --clean` (main.py:1025-1029) has no unburned-session guard; burn-failure except path resets volume to STAGING with links kept (772-787). Pick list excludes only DEPRECATED/DESTROYED, so the ghost volume is offered to an heir. Existing TestCleanSession asserts only dir removal + session status. Catalog lies 'archived' until NAS loss makes it data loss — critical stands.

**Fix.** clean_session() on a session whose volumes have zero ACTIVE volume_copies should delete_volume() those volumes (returning packs to the unarchived pool), or get_unarchived_packs() should require at least one volume in BURNED/VERIFIED status (or one ACTIVE copy). 'lcsas status' should report a distinct 'staged-but-never-burned' bucket instead of folding it into 'archived'. Pick-list queries should exclude volumes with status STAGING/BURNING that have zero volume_copies.

**Tests / gates to add:**
- tests/unit/test_session_pipeline.py::test_clean_unburned_session_returns_packs_to_unarchived — stage(), clean_session(), assert every pack reappears in get_unarchived_packs() and the ghost volume is gone.
- tests/unit/test_db_queries.py::test_pick_list_excludes_never_burned_staging_volumes — STAGING volume with no copies must not appear as a restore source.
- tests/unit/test_cli_handlers.py::test_status_reports_staged_unburned_bucket — status output distinguishes archived vs staged-only packs. All always-run in make test-unit.

#### 2. [HIGH] Schema migrations are never executed by any production code path; the live catalog is already stale and crashes on specific unhappy paths

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** db/schema.py defines migrate() (docstring even claims it is safe to call on disc catalogs), but no CLI handler, connection helper, or script ever calls it — every command calls create_all() only, which is CREATE TABLE IF NOT EXISTS and never alters existing tables. The project's own live catalog (/home/mikmorg/git/lcsas/archive.db) is at schema_version 5 with a volume_events CHECK that lacks BOTH 'VERIFY_FAIL_REBURN' and 'BURN_RECEIPT_IMPORTED'. Consequences verified against the live DB shape: (a) a re-burn whose post-burn verify FAILS calls add_event('VERIFY_FAIL_REBURN') -> sqlite3.IntegrityError -> the whole burn_session aborts with a raw traceback, exactly at the moment a bad disc needs recording; (b) 'lcsas catalog import-receipts' (which writes BURN_RECEIPT_IMPORTED events) crashes on this catalog; (c) v4-era catalogs can never enter CONSOLIDATING. The 'owner dies between schema versions' scenario is therefore the steady state, not an edge case: catalogs silently never upgrade, and on-disc holographic copies inherit the stale schema forever.

**Evidence.** src/lcsas/db/schema.py:201-209 (migrate defined; docstring claims it runs on disc snapshots) and schema.py:139 (current DDL expects VERIFY_FAIL_REBURN + BURN_RECEIPT_IMPORTED); grep over src/ shows zero migrate() callers — cli/main.py:587,618,648,672,769,913,960,1000,1076,1212 all import only create_all; live archive.db query: schema_version=(5,'2026-04-11') and volume_events CHECK = ('VERIFY_PASS','VERIFY_FAIL','ECC_REPAIR','LOCATION_MOVE','CONDITION_CHECK','NOTE'); src/lcsas/burn/orchestrator.py:735-740 writes VERIFY_FAIL_REBURN on re-burn verify failure.

**Existing partial coverage.** tests/unit/test_db_schema.py covers migrate() logic itself (always-on), not its absence from production paths

**Verifier.** Confirmed. grep: migrate() defined in db/schema.py:201 with zero callers in src/; all CLI handlers call create_all only (IF NOT EXISTS, never alters). Queried live archive.db: schema_version=(5,'2026-04-11'); volume_events CHECK has only the 6 v4-era types — lacks VERIFY_FAIL_REBURN and BURN_RECEIPT_IMPORTED. orchestrator.py:735-740 writes VERIFY_FAIL_REBURN on re-burn verify fail and cli import-receipts writes BURN_RECEIPT_IMPORTED → both IntegrityError on this catalog. tests/unit/test_db_schema.py exercises migrate() directly in always-on CI but never the production wiring, so it doesn't cover the gap.

**Fix.** Call migrate(conn) inside get_connection()/locked_connection() (or immediately after create_all in every CLI handler) so any catalog opened by the tooling is brought to CURRENT_SCHEMA_VERSION; refuse writes if the version is newer than the code understands. Run it once now on the live archive.db.

**Tests / gates to add:**
- tests/unit/test_db_schema.py::test_cli_auto_migrates_old_catalog — build a v5-shaped catalog fixture (replay the historical DDL), invoke cmd_status/cmd_verify via the CLI dispatcher, assert get_schema_version()==CURRENT_SCHEMA_VERSION afterwards.
- tests/unit/test_burn_orchestrator.py::test_reburn_verify_fail_on_v5_catalog — run burn_session with verify_disc=False against the v5 fixture; today it must reproduce the IntegrityError, after the fix it must record VERIFY_FAIL_REBURN cleanly. Always-run in make test-unit / .github/workflows/test.yml.
- tests/unit/test_db_schema.py::test_refuses_future_schema — opening a catalog with version > CURRENT_SCHEMA_VERSION fails loud with guidance instead of writing.

#### 3. [HIGH] Post-burn 'verification' never compares disc content to the ISO hash — VERIFIED means only 'a readable disc was in the drive'

**CONFIRMED** (verifier confidence: high) — tracked: Partially: docs/PHASE_12_20_PLAN.md Phase 14 [S1] 'Verification Pipeline' is marked implemented via --mark-verified/--mark-failed (remote workflow) — the content-compare gap itself is untracked; PHYSICAL_DISC_VALIDATION.txt covers it only as a manual annual drill.

**Gap.** verify_disc() runs 'xorriso -indev <dev> -check_media' and returns returncode==0. It never compares the burned bytes to the staged ISO's SHA-256 (which IS computed and stored in session_volumes at stage time) and never checks that the disc in the drive is the volume being verified. A truncated or mis-burned disc that is structurally readable passes; so does the wrong disc entirely. Worse for fool-proofing: 'lcsas verify <LABEL> --disc' performs the same readability-only check on whatever disc happens to be in the drive and then promotes that volume BURNED->VERIFIED and writes a VERIFY_PASS event — a non-technical operator verifying a 50-disc set can mark every volume VERIFIED while never inserting the right discs. The byte-for-byte sha256 compare exists only in the manual hardware drill doc, not in the pipeline.

**Evidence.** src/lcsas/iso/xorriso.py:307-325 (verify_disc = -check_media returncode only); src/lcsas/burn/orchestrator.py:289-293 and 704-721 (verify_ok gates VERIFIED, detail says 'Post-burn read-back' but no content compare); orchestrator.py:623-626 (iso_sha256 computed, then unused for disc verification); cli/main.py:1713-1720 (--disc path, no volume-identity or hash check), 1766-1769 (promotes BURNED->VERIFIED on it); recovery/docs/PHYSICAL_DISC_VALIDATION.txt:83 (sha256sum -c is manual-drill-only).

**Existing partial coverage.** Partial: recovery/docs/PHYSICAL_DISC_VALIDATION.txt manual drill does the sha256 compare (hardware-only, annual, not catalog-recorded)

**Verifier.** Confirmed. xorriso.py:307-325: verify_disc = `-check_media` returncode only (full-media readability scan — slightly stronger than the title implies — but no content compare, no disc-identity check). iso_sha256 computed and stored in session_volumes at stage (orchestrator.py:623-637) and never used against the device. cli verify --disc (main.py:1713-1720) checks whatever disc is in the drive and promotes BURNED→VERIFIED (1766-1769) with VERIFY_PASS. Mitigation: tier-1 restore-time blob auth prevents silently restoring corrupt data, but wrong/mis-burned discs sit marked VERIFIED until restore time when re-burning is impossible. High stands.

**Fix.** After burn (and in verify --disc), read the device back up to the ISO byte length and compare SHA-256 against session_volumes/volume_copies.iso_sha256 (or use xorriso -compare_r); refuse to mark VERIFIED on mismatch and refuse to verify a volume when the inserted disc's volume label/UUID (readable from volume_info.json on the disc) does not match the requested label.

**Tests / gates to add:**
- tests/unit/test_burn_orchestrator.py::test_burn_marks_verified_only_after_content_compare — fake xorriso runner records that a hash-compare against sv.iso_sha256 occurred; mismatch must leave status BURNED and emit VERIFY_FAIL.
- tests/integration/test_disc_content_verify.py (opt-in LCSAS_DISC_VERIFY=1, CDEmu) — burn ISO A, load ISO B in the virtual drive, run 'lcsas verify <A> --disc', assert FAIL and no promotion; wire into the existing cdemu e2e harness.
- tests/unit/test_cli_handlers.py::test_verify_disc_rejects_wrong_volume — verify --disc with a mounted disc claiming a different label must not promote or record VERIFY_PASS.

#### 4. [HIGH] A failed post-burn verify still records an ACTIVE volume copy at the location; re-burn failures also blank the stored ISO hash

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** In burn_session(), add_volume_copy() executes regardless of verify_passed: a disc that just FAILED read-back verification is recorded as an ACTIVE copy at the location (volume_copies has no verify-state column; only a volume_events row distinguishes it). All location-completeness queries count ACTIVE copies, so 'stage --for-location Offsite' will skip those packs — the only Offsite copy is a known-bad disc and the system reports the location complete. On a verify-failed re-burn the UPSERT additionally resets the prior good copy's row: status forced back to 'ACTIVE', burn_date overwritten, and iso_sha256 overwritten with NULL because the orchestrator never passes it — destroying the hash needed for the portable SHA-256 verify fallback. Years later there is no machine-readable record that this physical copy was bad.

**Evidence.** src/lcsas/burn/orchestrator.py:723-749 (verify_passed False -> status BURNED, then unconditional add_volume_copy(...) with no iso_sha256/verify state); src/lcsas/db/volume_copies.py:57-67 (UPSERT sets status='ACTIVE', iso_sha256=excluded.iso_sha256 i.e. NULL); src/lcsas/db/queries.py:446-488 (get_unarchived_or_missing_at_location / get_packs_at_location trust vc.status='ACTIVE'); tests/unit/test_burn_orchestrator.py:693-724 only asserts the VERIFY_FAIL_REBURN event, not copy state.

**Verifier.** Confirmed. orchestrator.py:742-749: add_volume_copy executes unconditionally after verify_passed=False, never passing iso_sha256. volume_copies.py:57-67 UPSERT forces status='ACTIVE', overwrites burn_date, and sets iso_sha256=excluded (NULL) — clobbering a prior good copy's hash. Location-completeness queries (queries.py:446-488) trust vc.status='ACTIVE', so a known-bad disc satisfies 'stage --for-location'. Existing test (test_burn_orchestrator.py:694-727) asserts only the VERIFY_FAIL_REBURN event, not copy state. Note import-receipts DOES pass iso_sha256, so only the local orchestrator path nulls it.

**Fix.** Record verify-failed burns as a copy with a new 'SUSPECT' (or 'FAILED') status excluded from ACTIVE-copy queries, or skip add_volume_copy until verification passes; always pass iso_sha256=sv.iso_sha256 and never let the UPSERT overwrite a non-null hash with NULL.

**Tests / gates to add:**
- tests/unit/test_burn_orchestrator.py::test_verify_fail_does_not_record_active_copy — verify_disc=False; assert get_copies_for_volume(active_only=True) is empty for that location and get_unarchived_or_missing_at_location still returns the packs.
- tests/unit/test_burn_orchestrator.py::test_reburn_verify_fail_preserves_prior_copy_hash_and_date — prior good copy's iso_sha256/burn_date survive a failed re-burn.
- tests/unit/test_db_volume_copies.py::test_upsert_never_nulls_existing_iso_sha256.

#### 5. [MEDIUM] No tooled disc-rot re-verification: 'verify --all' checks staging ISOs that are deleted after burn, and volume_copies.last_verified_at is never written by any code

**CONFIRMED** (verifier confidence: high) — tracked: Partially: recovery/docs/READINESS_CHECKLIST.txt 'DISC READABILITY SCAN' documents the manual cadence; the tooling gap (verify --all useless post-burn, dead last_verified_at column) is untracked.

**Gap.** The shelf-storage threat (decades of slow media decay) has no catalog-integrated detection loop. burn_session() deletes each ISO after a successful burn, so the batch re-verification command 'lcsas verify --all' — which only knows how to verify ISO files — skips essentially every burned volume ('no ISO path — skipped' / 'ISO not found — skipped'). The per-disc mode is readability-only (see separate finding) and nothing anywhere writes volume_copies.last_verified_at, so the schema's per-copy freshness field is a dead column: neither the owner nor an heir can ask 'when was each physical copy last confirmed good, and which copies are overdue'. The READINESS_CHECKLIST prescribes a monthly manual dd/dvdisaster scan but its results are never recorded in the catalog, so the holographic copies burned onto later discs carry no verification history.

**Evidence.** src/lcsas/burn/orchestrator.py:789-800 (ISO unlinked after verified burn); src/lcsas/cli/main.py:1818-1833 (_verify_all skips volumes without an ISO on disk; only ISO/dvdisaster/sha256-of-ISO paths exist); grep over src/ shows last_verified_at written only by schema DDL/migration (db/schema.py:103,249) — no UPDATE anywhere; recovery/docs/READINESS_CHECKLIST.txt:86-102 (manual scan, no catalog recording).

**Existing partial coverage.** Partial: recovery/docs/READINESS_CHECKLIST.txt manual disc-readability scan (cadence documented, results not recorded)

**Verifier.** Confirmed. orchestrator.py:792-794 unlinks ISO after verified burn; _verify_all (main.py:1818-1833) skips any volume without an on-disk ISO, so post-burn batches verify nothing. grep over src/: last_verified_at appears only in DDL, migration, model, and rebuild row-copy — no UPDATE writer anywhere, dead column confirmed. READINESS_CHECKLIST manual dd/dvdisaster scan exists but results are never catalog-recorded, exactly as the finding states.

**Fix.** Make 'lcsas verify --disc' the batch citizen: 'verify --all --location X' iterates copies at a location, prompts per disc, hash-compares against the recorded iso_sha256, stamps volume_copies.last_verified_at, and a 'lcsas status --stale-copies [--older-than 12m]' report lists copies overdue for re-verification.

**Tests / gates to add:**
- tests/unit/test_cli_handlers.py::test_verify_disc_stamps_last_verified_at — after a passing disc verify, the matching volume_copies row has last_verified_at set.
- tests/unit/test_cli_handlers.py::test_status_stale_copies_report — copies with NULL/old last_verified_at are listed with age.
- tests/recovery_hardening/test_last_verified_writer_exists.py — static test (same pattern as test_env_var_docs.py) asserting a production writer for last_verified_at exists, so the column can never regress to dead.

#### 6. [MEDIUM] Catalog rebuild resurrects DEPRECATED/DESTROYED volumes to VERIFIED from stale disc catalogs, and merge results depend on disc insertion order

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** Mixed-generation box scenario: discs burned before a volume was deprecated/destroyed carry catalogs recording it as VERIFIED. _merge_one_disc resolves status conflicts by 'most alive wins' (VERIFIED ranks above DEPRECATED/DESTROYED), so rebuilding a master catalog from a pile of mixed-age discs upgrades destroyed volumes back to VERIFIED. get_missing_packs then reports packs on those volumes as restorable, the pick list sends the heir hunting for a shredded disc, and the deprecated_disc_labels warning channel in the restore planner never fires. Separately, packs merge with INSERT OR IGNORE keyed on sha256, so is_pruned/size come from whichever disc was merged FIRST — rebuild output differs depending on the order the heir feeds discs in, with no warning. There is no per-row timestamp to prefer the newest catalog's view.

**Evidence.** src/lcsas/db/rebuild.py:109-151 (rank table: VERIFIED=6 > ... > DESTROYED=0, upgrade when source ranks higher; comment 'prefer the less-destroyed state'); rebuild.py:84-93 (packs INSERT OR IGNORE — first disc wins is_pruned); src/lcsas/db/queries.py:301-316 (get_missing_packs trusts non-DESTROYED status); src/lcsas/restore/planner.py:80,92 (deprecated_disc_labels only populated when status survived as DEPRECATED/DESTROYED).

**Existing partial coverage.** tests/unit/test_db_rebuild.py pins the current 'prefer most alive' behavior (it asserts the hazard, not the fix)

**Verifier.** Confirmed. rebuild.py:117-150 rank table upgrades DESTROYED(0)→VERIFIED(6) whenever any source ranks higher; packs merge INSERT OR IGNORE on sha256 (84-93) so first-fed disc wins is_pruned/size. One correction: volume-status resolution is order-INDEPENDENT (converges to max rank — resurrection happens in both orders); order dependence applies only to pack fields. planner.py:80,92 confirms deprecated warnings rely on status surviving. Existing tests (test_db_rebuild.py::test_merge_status_conflict_prefers_higher_quality / _keeps_better_status) pin the resurrect behavior as intended — a fix must invert them.

**Fix.** Make merge order-independent and recency-aware: pick status from the disc whose catalog is freshest (the restore.sh/disc_locator mtime heuristic already exists for catalog selection — apply the same rule per-volume during rebuild), or at minimum log a loud per-volume warning when a merge changes DEPRECATED/DESTROYED back to VERIFIED, and document 'feed the newest disc first' in RECOVER.txt's rebuild section.

**Tests / gates to add:**
- tests/unit/test_db_rebuild.py::test_merge_is_order_independent — merge {stale,fresh} catalogs in both orders; assert identical resulting volumes/packs rows.
- tests/unit/test_db_rebuild.py::test_destroyed_volume_not_silently_resurrected — stale catalog says VERIFIED, fresh says DESTROYED; assert DESTROYED survives (or a RebuildResult warning is emitted and asserted).
- Doc artifact: rebuild caveats section in recovery/docs/RECOVER.txt + assert presence via a static test alongside tests/recovery_hardening/test_disc_swap_docs.py.

*Verifier refinements:*
- test_merge_is_order_independent should target pack fields (is_pruned/size_bytes) — volume status already converges order-independently
- test_destroyed_volume_not_silently_resurrected must also update/invert the existing test_merge_status_conflict_* tests that pin the current behavior

#### 7. [MEDIUM] Table-recreating migrations (v4->v5, v5->v6) are not crash-atomic; a crash mid-migration leaves a catalog that create_all then silently masks as EMPTY

**CONFIRMED** (verifier confidence: high) — tracked: CODE_REVIEW_CLEANUP.md §14 has a generic unchecked item 'Database schema versions (migration strategy if needed)' — the crash-atomicity hazard is not tracked.

**Gap.** The v5 and v6 migrations do RENAME -> CREATE -> INSERT...SELECT -> DROP. Under Python sqlite3's legacy isolation, the DDL statements run in autocommit (verified: conn.in_transaction is False after the RENAME), so a crash between RENAME and the final commit leaves the catalog with volumes_old and NO volumes table; schema_version still reads the old version. Reproduced: re-running migrate() fails with 'no such table: volumes'. Worse, every CLI command runs create_all() first, which would recreate an EMPTY volumes table — 'lcsas status' then shows zero volumes and restore plans report everything missing, while all real volume data sits invisibly in volumes_old. This is exactly the 'owner dies mid-migration' combo: the NAS catalog needs manual SQL surgery and nothing tells the heir that (the on-disc holographic copies remain intact, which is the real mitigation but is documented nowhere in this context).

**Evidence.** src/lcsas/db/schema.py:264-298 (v5) and 303-326 (v6) RENAME/CREATE/INSERT/DROP sequence with commit only at the end; reproduction run in this audit: after simulated crash post-RENAME, tables=['volumes_old',...], version=4, migrate() -> OperationalError 'no such table: volumes'; db/schema.py:171-198 (create_all would recreate empty volumes via IF NOT EXISTS).

**Verifier.** Reproduced independently: on a v4-shaped catalog, conn.in_transaction is False after the RENAME (DDL autocommits under legacy isolation), simulated crash leaves tables=['schema_version','volumes_old',...], version=4; migrate() then fails OperationalError 'no such table: volumes' and create_all recreates an empty volumes table (0 rows) while volumes_old still holds the data. Caveat: latent today because migrate() has no production callers (finding 1); becomes live the moment auto-migration is wired in — fix both together.

**Fix.** Run each migration step inside an explicit transaction (set isolation_level=None and BEGIN IMMEDIATE ... COMMIT — SQLite DDL is transactional); before migrating, detect a leftover *_old table and either resume or fail loud with recovery instructions; create_all() should refuse to run when volumes_old exists.

**Tests / gates to add:**
- tests/unit/test_db_schema.py::test_migration_crash_window_is_atomic — execute the RENAME, close the connection (simulated crash), reopen: assert the catalog still has an intact volumes table (atomic txn) or migrate() resumes cleanly.
- tests/unit/test_db_schema.py::test_create_all_refuses_when_volumes_old_present — create_all on a wedged catalog must fail loud, never present an empty catalog.

#### 8. [MEDIUM] No blast-radius reporting: nothing answers 'what is lost if all copies of disc X fail', and the existing under-replication query has no CLI surface

**CONFIRMED** (verifier confidence: high) — tracked: Closest is recovery/docs/READINESS_CHECKLIST.txt:76-84 'VOLUME COUNT CHECK' (inventory gap only); per-disc blast radius is untracked. recovery/docs/DEFERRED_WORK.txt item 4 (snapshot-aware pick list) is the restore-side inverse, also deferred.

**Gap.** The dimension question 'all copies of ONE disc fail — can the user know in advance which data is lost?' has no answer in the tooling. get_redundancy_report() (packs below N volume copies) exists in db/queries.py but is called only by tests — no CLI command exposes it. There is no command mapping a volume to the snapshots/paths that become unrestorable if it is lost (the data exists: volume_packs + the rustic index inside per-repo metadata), and 'lcsas status' prints only aggregate counts plus a volume list. An owner cannot run a pre-mortem ('which discs are single-points for which memories?') and an heir cannot triage which damaged disc matters most. READINESS_CHECKLIST's 'volume count check' detects missing discs but not concentration of risk.

**Evidence.** src/lcsas/db/queries.py:397-417 (get_redundancy_report defined); grep over src/lcsas/cli/main.py: zero references (only tests/unit/test_edge_cases.py, test_cross_location_restore.py, test_multi_copy_restore.py use it); cli/main.py:909-933 (cmd_status output is counts + per-volume status lines only).

**Verifier.** Confirmed. get_redundancy_report (queries.py:397-417) referenced only by 8 test files, zero CLI references. cmd_status prints aggregate counts + per-volume status lines. Closest existing surface is `lcsas location list/status` (main.py:1223-1269) which reports location completeness ('N packs behind') but nothing per-volume/per-disc and no min-copies view. No volume-impact command exists anywhere in the 15+ subcommands.

**Fix.** Add 'lcsas status --redundancy [--min-copies N]' (surface get_redundancy_report grouped by volume/location) and 'lcsas volume impact <LABEL>' listing repos, pack counts/bytes, and affected snapshots that would lose their only copy if the volume's copies all fail; print the worst offenders in the default status output.

**Tests / gates to add:**
- tests/unit/test_cli_handlers.py::test_status_redundancy_lists_single_copy_packs — populated catalog with one single-copy volume; assert it is flagged with pack/byte counts.
- tests/unit/test_cli_handlers.py::test_volume_impact_lists_snapshots_at_risk — volume holding the only copy of packs referenced by snapshot S; assert S appears in the impact report.
- Always-run in make test-unit; add a one-line 'blast radius review' item to READINESS_CHECKLIST.txt referencing the new command.

#### 9. [MEDIUM] Heir restore has no disk-space preflight; ENOSPC mid-restore is the failure mode and the tier-1 fs-full drain branches are untested

**CONFIRMED** (verifier confidence: high) — tracked: Partially: recovery/docs/AUDIT_FINDINGS.md 'Remaining gaps' tracks the fs-full drain branches as a coverage gap; the missing user-facing preflight is untracked (not in UX_CONCERNS.txt).

**Gap.** The burn side checks staging free space (orchestrator.py stage() preflight) but the restore side — the path the non-technical heir walks — has none: restore.sh, the live restore wizard, and RestoreExecutor never check free space at the target or the pack cache, even though the catalog knows total restore bytes (PickList.total_bytes) and RECOVER.txt simply tells the reader to have 'enough free space'. An heir restoring a multi-hundred-GB archive onto a laptop smaller than the archive will fail mid-restore after long disc-swapping effort, with whatever message the underlying write error produces. AUDIT_FINDINGS.md itself lists the disc_locator drain 'fs-full' branches as uncovered code, so the behavior at ENOSPC in tier-1 is not pinned by any test.

**Evidence.** grep for disk_usage/statvfs/space across src/lcsas/restore/executor.py, restic_fallback.py, meta/live/restore_wizard.py, recovery/scripts/restore.sh: no preflight (restore.sh:310 only mentions the cache 'trades disk space'); recovery/docs/RECOVER.txt:16 ('enough free space' with no check); contrast src/lcsas/burn/orchestrator.py:567-582 (staging-side shutil.disk_usage preflight exists); recovery/docs/AUDIT_FINDINGS.md:111 ('Drain edge cases (fs-full, missing source)' uncovered in disc_locator.c).

**Existing partial coverage.** tests/recovery_hardening/test_tier1_target_full.py pins tier-1 target ENOSPC diagnostic (environment-gated, partial)

**Verifier.** Core gap confirmed: no statvfs/disk_usage in restore.sh, executor, restic_fallback, or restore wizard (the df at restore.sh:85 is mount-point detection); RECOVER.txt just says 'enough free space'; burn-side preflight exists (orchestrator.py:567-582). One sub-claim wrong: tier-1 target-full ENOSPC IS pinned — tests/recovery_hardening/test_tier1_target_full.py (issue #221) asserts an explicit 'out of space' diagnostic naming the target (partial: skips without rustic+built binary+passwordless sudo). The uncovered fs-full branch is the disc_locator drain path (AUDIT_FINDINGS.md), not the target write. The user-facing preflight gap is fully untracked and real.

**Fix.** restore.sh and the restore wizard should compute required bytes (from the catalog / snapshot size) and compare against statvfs of the target (and LCSAS_PACK_CACHE_DIR) before the first disc prompt, failing with 'need ~X GB free at <target>, have Y GB'; tier-1 should print an explicit disk-full message on short write.

**Tests / gates to add:**
- tests/recovery_hardening/test_restore_space_preflight.py — run restore.sh with --target on a tiny tmpfs; assert it exits before any disc/password prompt with a message naming required vs available bytes.
- recovery/tests: unit test driving the disc_locator drain path against a full filesystem fixture (closes the AUDIT_FINDINGS fs-full coverage gap); wire into make -C recovery test.
- tests/unit/test_cli_restore.py::test_restore_exec_warns_insufficient_target_space — planner total_bytes > target free -> hard warning/confirm.

*Verifier refinements:*
- Drop the 'tier-1 should print an explicit disk-full message on short write' recommendation/test for the target path — already implemented and pinned by test_tier1_target_full.py; keep the disc_locator drain fs-full test (that branch is the real uncovered one per AUDIT_FINDINGS.md)

#### 10. [LOW] Burn provenance for the newest session exists only on the NAS catalog and the WARM staging disk — the holographic property never covers it, and rebuild drops the audit trail

**CONFIRMED** (verifier confidence: high) — tracked: Adjacent only: recovery/docs/DEFERRED_WORK.txt item 1 (disc_index.txt sidecar) covers catalog redundancy, not burn-provenance persistence; not tracked elsewhere.

**Gap.** By construction the catalog burned onto each disc is copied at staging time, before any burn: every volume of the final session appears as STAGING with zero volume_copies in every surviving on-disc catalog (a disc cannot contain its own burn record, but session siblings and the meta-volume don't carry it either — restore.sh notes the meta-disc deliberately ships no catalog). Burn receipts (location, verify result, iso_sha256) are written only to the staging SSD's session directory, which clean_session deletes; rebuild_catalog merges 7 tables but not volume_events, burn_sessions, or session_volumes, so a catalog rebuilt from discs after NAS loss has no location, no verification history, and no ISO hashes (volume_copies.iso_sha256 is NULL anyway — orchestrator never passes it) for the newest discs. Restore still works (pick lists tolerate STAGING; the freshest-catalog-by-mtime selection in restore.sh/disc_locator.c is implemented), but resuming OPERATIONS from the holographic copy loses where copies live, what was verified, and the portable hash-verify capability — and none of this is documented as a known limitation.

**Evidence.** src/lcsas/burn/orchestrator.py:403-420 (inject_catalog at stage time) vs 629-637 (session row added after) and 742-748 (add_volume_copy at burn time, without iso_sha256); orchestrator.py:965-1001 (receipts JSON in session_dir) + 813-830 (clean_session deletes it); src/lcsas/db/rebuild.py:62-196 (merges repositories/locations/packs/volumes/snapshots/volume_packs/volume_copies only); recovery/scripts/restore.sh:827-829 (meta-disc carries no catalog; freshest data-disc catalog wins).

**Verifier.** Confirmed. inject_catalog runs inside _stage_single_volume (orchestrator.py:407-409) before add_session_volume and before any burn, so every disc's catalog shows its own session as STAGING with no copies. Receipts (with iso_sha256/verify_passed) go to session_dir/receipts (965-1001) which clean_session deletes. rebuild.py merges exactly 7 tables — volume_events, burn_sessions, session_volumes excluded — and orchestrator's add_volume_copy omits iso_sha256, so a disc-rebuilt catalog has no hashes/verify history for any session. restore.sh:827-829 confirms meta-disc ships no catalog. Restore still works; low is correct (operations-resumption polish).

**Fix.** Pass iso_sha256/media_serial into add_volume_copy at burn time; print receipts to the ESTATE_PLANNING paper manifest workflow and/or copy receipts of session N onto the next session's discs and each meta-volume rebuild; document in RECOVER.txt that a disc-rebuilt catalog will show the newest session as STAGING with no locations, and that this is expected.

**Tests / gates to add:**
- tests/unit/test_burn_orchestrator.py::test_burn_records_iso_sha256_on_copy — after burn_session, volume_copies.iso_sha256 equals session_volumes.iso_sha256.
- tests/unit/test_db_rebuild.py::test_rebuild_from_disc_catalogs_documents_missing_provenance — rebuild from staged catalogs; assert RebuildResult reports volumes lacking copies/locations instead of staying silent.
- Static doc test (tests/recovery_hardening) pinning the new RECOVER.txt 'rebuilt catalog limitations' section.

---

## Format & dependency durability bets  `format-durability`

> The format bets split cleanly into well-hedged and unhedged. Well-hedged: the restic v1/v2 format (complete on-disc spec plus two independent re-implementations — C89 tier-1 and pure-Python tier-3), zstd (full vendored decoder, magic-based detection), SQLite-as-file-format (vendored amalgamation, statically linked), and ISO9660 Level 3 + Rock Ridge + Joliet as the filesystem. Unhedged and largely untracked: the entire RS03 ECC repair story — the only tooling that can spend the 15% parity budget is an abandoned-upstream binary that is optionally bundled for one arch, never wired into restore.sh/restore.bat, never pinned, and backed by an on-disc "spec" that admits it is not sufficient for re-implementation — meaning a damaged disc is repaired-or-rejected only if a 2026-era x86_64 Linux dvdisaster still runs, which fails the non-technical-heir bar outright. Second-order gaps: no burn-time gate stops the unpinned live rustic writer from drifting past the pinned readers (masked by routine test-restores using the same live rustic), and no guard or doc covers the >4 GiB multi-extent ISO9660 limitation on Windows' native mount path. Verdict per bet: keep ISO9660 (add the 4 GiB guard), keep restic v1/v2 (add the drift gate), keep SQLite (add the schema-skew contract), keep zstd as-is, keep RS03 for the burned back-catalog but bring repair in-house (own C89 decoder + pinned dvdisaster source/binaries), and temper the M-DISC/BDXL media guidance.

#### 1. [CRITICAL] RS03 ECC repair is expert-only: the repair half of the disc-integrity layer is outside the bare-minimum path and depends on an abandoned, unpinned, host-arch-only dvdisaster binary

**CONFIRMED** (verifier confidence: high) — tracked: Partially: docs/SURVIVABILITY.md §2.5 'dvdisaster is abandoned' (P2) — marked ✅ Resolved, but the resolution was documentation-only (writing DVDISASTER_RS03_FORMAT.md). The runtime gap — no shipped tool can repair, on any heir platform, without network — is untracked in UX_CONCERNS.txt, DEFERRED_WORK.txt, and READINESS_CHECKLIST.txt.

**Gap.** Every disc pays 15% capacity for RS03 parity, but no shipped LCSAS tooling can USE that parity at restore time. The tier-1 C binary contains zero RS03 code (grep for dvdisaster/rs03 across recovery/src, recovery/scripts returns nothing); restore.sh and restore.bat never invoke or even mention ECC repair. The documented damaged-disc path (RECOVER.txt) requires the heir to (a) run ddrescue — never bundled anywhere — and (b) 'have dvdisaster installed'. dvdisaster is bundled only opportunistically (_OPTIONAL_TOOLS, only if on the build host's PATH), only as a build-host-arch dynamically-linked binary (ToolBundler explicitly excludes glibc-family libs, so it needs a 2026-ABI-compatible host glibc), and is absent from UPSTREAM.sha256 pinning and from the 6-target cross-build matrix that tier 1/2/3 all got. On Windows the official guidance is to download dvdisaster from a fan-maintained mirror URL. So in 2050, a non-technical heir with a scratched disc gets the fail-loud half (Poly1305/SHA-256 rejection) but not the repair half — the parity bytes are unreachable, and the cryptographic layer correctly refuses the data. That is plausible permanent data loss in exactly the scenario ECC was designed for, and on multi-disc archives the affected packs may exist on only one disc.

**Evidence.** src/lcsas/meta/builder.py:31 `_OPTIONAL_TOOLS = ("dvdisaster",)` and :1792-1795 (bundled only `if _shutil.which(tool)`); src/lcsas/meta/bundler.py:23-29 (glibc-family libs deliberately not bundled → dvdisaster ships glibc-dynamic, build-host arch only); recovery/UPSTREAM.sha256 pins only rustic v0.11.2 + CPython (no dvdisaster entry); recovery/scripts/restore.sh and restore.bat: zero matches for 'dvdisaster|ecc|repair'; recovery/docs/RECOVER.txt:154-161 ('Use ddrescue…', 'If you have dvdisaster installed: dvdisaster -i /tmp/disc.iso -f'); recovery/docs/RECOVER_WINDOWS.txt:371-375 ('install dvdisaster from https://dvdisaster.jcea.es'); recovery/docs/TIERS.txt:97-102 claims RS03 'repairs bit-rotted sectors' as one of two guards, but the only validation is tests/integration/test_ecc_repair.py:40-44 — opt-in (LCSAS_ECC_REPAIR=1) and requiring the real dvdisaster binary on PATH.

**Existing partial coverage.** tests/integration/test_ecc_repair.py — PARTIAL only: opt-in (LCSAS_ECC_REPAIR=1), requires real dvdisaster on PATH; never exercises repair without dvdisaster

**Verifier.** Verified: grep of restore.sh/restore.bat finds zero ecc/dvdisaster/repair mentions; recovery/src and recovery/bin/<target>/ contain no RS03 code or ECC tool; UPSTREAM.sha256 pins only rustic+CPython; builder.py _OPTIONAL_TOOLS bundles dvdisaster only if on build-host PATH, glibc-dynamic (bundler.py excludes glibc-family libs); UX_CONCERNS/DEFERRED_WORK have zero ecc entries. Near-refutation checked: restore/executor.py and `lcsas verify` DO wire repair_iso — but via SubprocessDVDisasterRunner shelling to external dvdisaster, so the dependency claim stands. Consolidation (collapsing redundant packs) makes single-copy pack loss plausible. Critical holds.

**Fix.** Bring repair onto the bare path. Best: implement a C89 `lcsas-ecc` (verify + erasure-repair of RS03-augmented images) in recovery/src, built and cross-built exactly like lcsas-restore for all 6 targets, and wire it into restore.sh as a pre-cascade step when the image fails CRC. RS erasure decoding over GF(2^8) is ~500 LOC against the already-vendored style; the hard part (interleaving layout) must be extracted from dvdisaster source once and pinned by conformance tests. Interim mitigations regardless: (1) pin a static dvdisaster build per target in UPSTREAM.sha256 and make it a required (not optional) meta-volume item; (2) bundle GNU ddrescue or document `dd conv=noerror,sync` as the no-tools imaging fallback in RECOVER.txt; (3) make restore.sh print the repair procedure when a disc read fails. Also evaluate emitting PAR2 sidecar files alongside RS03 for new burns (public spec, many independent implementations) — but a self-owned RS03 decoder is strictly better because it also repairs the already-burned back-catalog.

**Tests / gates to add:**
- tests/e2e/test_ecc_selfrepair_no_dvdisaster.py: master a small ISO, augment, corrupt 5% of data sectors, strip dvdisaster from PATH (shim dir exiting 127), repair using ONLY shipped LCSAS tooling, assert byte-identical extraction. Add to `make gate` (always-run, small fixture) — this is the dimension's headline gate: 'our code repairs N% damage with no dvdisaster present'.
- tests/recovery_hardening/test_ecc_tooling_on_meta.py: build a meta-volume and assert an ECC repair tool exists under tools/bin (or recovery/bin/<target>/) for every approved target, pinned in UPSTREAM.sha256/MANIFEST.sha256; fail the build loudly (not silently skip) when the tool is missing.
- tests/recovery_hardening/test_restore_sh_ecc_dispatch.py: static check that restore.sh contains a damaged-image verify/repair step referencing the bundled tool (same pattern as test_disc_swap_docs.py).

*Verifier refinements:*
- Proposal 1 caveat: dvdisaster-compatible RS03 augmented mode pads any image to a full medium (~700 MB, multi-minute passes per test_ecc_repair.py docstring). An always-on `make gate` test is only feasible if the in-house lcsas-ecc supports unpadded/small images; otherwise keep it as a fast-ish CI job, not a per-commit local gate.

#### 2. [HIGH] DVDISASTER_RS03_FORMAT.md is not a re-implementable spec, and the dvdisaster source it defers to is not on the disc

**CONFIRMED** (verifier confidence: high) — tracked: docs/SURVIVABILITY.md §2.5 marks 'dvdisaster RS03 format docs' as ✅ Done; the incompleteness of that resolution is untracked.

**Gap.** The on-disc RS03 document — the designated survivability artifact for when the dvdisaster binary no longer runs — explicitly punts on the two things a re-implementer cannot guess: the exact ECC header struct layout ('may vary between dvdisaster versions; consult the source code's rs03-common.h') and the interleaving order ('requires reading the RS03 source code'). That source lives only at a dormant GitHub fork URL; it is not bundled on the meta-volume (_SOURCE_ITEMS bundles only LCSAS `src`, _DOC_ITEMS bundles docs/README/pyproject). The doc also recommends pip-installable libraries ('reedsolo… pip-installable') — exactly the kind of dependency the rest of the recovery design forbids. Contrast RESTIC_FORMAT_SPEC.md, which was complete enough that tier-1 (C) and tier-3 (Python) readers were independently implemented from it. A 2050 engineer holding a damaged disc and this doc still cannot decode the parity.

**Evidence.** docs/DVDISASTER_RS03_FORMAT.md:109-111 ('The actual struct layout and byte order may vary between dvdisaster versions; consult the source code's rs03-common.h'), :211-213 ('The main challenge is matching dvdisaster's specific interleaving layout, which requires reading the RS03 source code'), :207 ('Python: reedsolo library (pure Python, pip-installable)'); src/lcsas/meta/builder.py:34-35 `_SOURCE_ITEMS = ("src",)` / `_DOC_ITEMS = ("docs", "README.md", "pyproject.toml")` — no dvdisaster source tarball; docs/SURVIVABILITY.md:178-182 claims the doc 'covers RS03 binary layout… and re-implementation guidance', contradicted by the doc's own caveats.

**Verifier.** Verified verbatim: doc lines 109-111 ('struct layout and byte order may vary... consult rs03-common.h') and 211-213 ('requires reading the RS03 source code'), plus pip-installable reedsolo recommendation at 207. builder.py _SOURCE_ITEMS=('src',), _DOC_ITEMS=docs/README/pyproject — no dvdisaster source; recovery/vendored/ holds only sqlite+zstd. SURVIVABILITY.md §2.5 marked '✅ Done' (line 169, 318) on the strength of this doc, so the load-bearing abandonment hedge is broken while tracked as resolved. No conformance test exists. High is correct: the doc is the designated survivability artifact and fails its purpose.

**Fix.** Two-part fix: (1) pin the exact dvdisaster source tarball (the version used at burn time) in UPSTREAM.sha256 and bundle it on every meta-volume (GPLv3 permits and arguably requires source availability if shipping the binary); (2) complete the spec — record the definitive header field offsets, byte order, and the layer/interleaving formula for the pinned version, derived from rs03-common.h, so the doc stands alone. Keep RS03 (changing formats strands the burned back-catalog) but make the spec self-sufficient.

**Tests / gates to add:**
- tests/integration/test_rs03_doc_conformance.py (opt-in with dvdisaster, alongside test_ecc_repair.py): augment a fixture ISO with the real binary, then parse the ECC header using ONLY the offsets/cookie documented in DVDISASTER_RS03_FORMAT.md §3.2 and assert nroots/dataSectors/eccSectors agree with dvdisaster -t output — fails whenever the doc and the binary truth diverge.
- tests/recovery_hardening/test_meta_bundles_dvdisaster_source.py: assert the pinned dvdisaster source archive appears in UPSTREAM.sha256 and lands on a built meta-volume.

#### 3. [HIGH] No burn-time compatibility gate between the live (unpinned) rustic writer and the pinned tier-1/2/3 readers — silent restic-format drift

**CONFIRMED** (verifier confidence: high) — tracked: Partially: docs/SURVIVABILITY.md §5 row 'Rustic project abandoned → format spec on disc' covers the tool disappearing, not the writer silently outrunning the pinned readers. The drift-gate gap is untracked in all ledgers.

**Gap.** The burn pipeline treats pack files as opaque: packs/scanner.py and burn/orchestrator.py never check the repository format version, KDF parameters, or compression mode; the tier-1 reader (repo.c) never parses the repo config version either — it sniffs zstd by magic and assumes restic v1/v2 crypto (AES-256-CTR + Poly1305-AES). The operator's NAS rustic is whatever they install; only the tier-2 fallback is pinned (v0.11.2). If a future rustic migrates the mirror to a v3 format (different MAC, KDF, or compression — restic/rustic have bumped the format before, v1→v2 added zstd), every disc burned afterward is undecodable by all three tiers, while the operator's routine `lcsas restore exec` test-restore (which uses the same live rustic) keeps passing — actively masking the drift. The only end-to-end catch is the blind-restore drill, which is manual, cost-gated, and not in CI (cdemu is not installed in CI per test.yml). An heir could inherit years of discs whose packs no shipped reader can open.

**Evidence.** grep of src/lcsas/packs/scanner.py and src/lcsas/burn/orchestrator.py: zero repo-version/config checks; recovery/src/lcsas-restore/repo.c:23-24 + 927-931 (compression inferred from ZSTD_MAGIC; no config version parse anywhere in repo.c); recovery/docs/FORMAT.txt:43-56 documents only v1/v2 semantics; recovery/UPSTREAM.sha256 pins rustic v0.11.2; docs/RESTIC_FORMAT_SPEC.md:308 ('version: repository format version (1 or 2)'); .github/workflows/test.yml:68-73 ('cdemu is NOT installed in CI… e2e blind-restore suite is' local-only); Makefile:80-118 (blind-restore requires LCSAS_BLIND_ACK_COST=1, manual).

**Existing partial coverage.** tests/recovery_hardening/test_tier1_vs_tier2_differential.py — PARTIAL: real `rustic init`+`backup` writer → tier-1 reader round-trip runs in CI, but with the pinned 0.11.2 writer, so it validates the reader, not live-writer drift

**Verifier.** Verified: zero repo-format/version checks in packs/scanner.py and burn/orchestrator.py (orchestrator preflight only checks xorriso/dvdisaster tool versions); repo.c never parses repo config (grep 'config' empty), sniffs zstd by magic; restic_fallback.py reads config version only into a stats dict (line 1202), no gate. CI pins writer rustic 0.11.2, so drift of the operator's live rustic is untestable there; blind-restore is manual + cost-gated. Drift requires operator action or a new repo created with future rustic — conditional but plausible over decades, and masked by live-rustic test-restores. High holds.

**Fix.** Add a burn-time preflight that proves the bytes about to be burned are readable by the shipped recovery stack: before staging, run the tier-1 binary (new `lcsas-restore --check-repo` mode: decrypt config + one index + one blob) or PurePythonRestorer against the mirror using the configured password_file, and refuse to burn on failure with an explicit 'repository format newer than recovery readers' error. Pin the supported repo version (≤2) as an explicit constant the gate enforces. This converts a decades-later heir-facing failure into an immediate operator-facing one.

**Tests / gates to add:**
- tests/unit/test_burn_preflight_repo_version.py: synthetic repo with config version 3 (and one with an unknown compression byte) — assert the burn orchestrator aborts before ISO creation with the format-drift error; v1 and v2 fixtures pass.
- CI gate: add a cdemu-free blind-ish restore job to .github/workflows/test.yml — build a TEST_TINY archive with the real rustic from UPSTREAM.sha256, then restore it using ONLY the tier-1 binary against the staged tree (no optical emulation needed), asserting byte-identical output on every PR. This makes writer/reader drift fail CI the day it lands instead of at the annual drill.

*Verifier refinements:*
- The proposed CI job (build archive with UPSTREAM.sha256 rustic, restore with tier-1) pins the writer by construction so it cannot detect live-writer drift, and largely duplicates test_tier1_vs_tier2_differential.py. The effective gate is the operator-side burn preflight (lcsas-restore --check-repo or PurePythonRestorer against the mirror, refusing version >2). If a CI canary is wanted, run the round-trip against the LATEST upstream rustic release (unpinned, scheduled job) to get early warning of format bumps.

#### 4. [MEDIUM] ISO9660-only bet: files ≥4 GiB master 'successfully' as multi-extent but are unreadable via Windows' native CDFS mount — no staging guard, no doc caveat

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** xorriso is invoked with `-iso-level 3` commented 'Support files > 4 GB', and SURVIVABILITY.md §4.1 rates ISO 9660 'Low risk' with no caveat. ISO level 3 stores >4 GiB files as multi-extent, which Windows' built-in CDFS driver (the path behind restore.bat / PowerShell Mount-DiskImage per the RFC) does not reassemble — the heir sees a truncated or duplicated file with no error. Nothing in the pipeline prevents a ≥4 GiB single file from being staged: binpack only rejects items larger than whole-media capacity (algorithm.py:40-58), so a large catalog.db at scale, an operator-tuned rustic pack size, or a future bundled artifact slides through silently and the failure surfaces decades later on the most likely heir platform (Windows). Keeping ISO9660 over UDF is the right long-horizon call (UDF revision/driver fragmentation is worse) — the gap is the missing guard, not the format choice.

**Evidence.** src/lcsas/iso/xorriso.py:125 ('-iso-level', '3', # Support files > 4 GB) and :225; src/lcsas/binpack/algorithm.py:40-58 (only whole-media oversize check); docs/SURVIVABILITY.md:260-263 (§4.1 'Low risk', no multi-extent caveat); docs/CROSS_PLATFORM_META_RFC.md:113-114 (Windows restore path mounts via PowerShell Mount-DiskImage, i.e., native CDFS); recovery/docs/RECOVER_WINDOWS.txt: no >4 GiB caveat.

**Verifier.** Verified: xorriso.py builds both data and meta ISOs with '-iso-level 3' commented 'Support files > 4 GB' (lines 125, 225); binpack/algorithm.py:43-58 rejects only items exceeding whole-media usable capacity; SURVIVABILITY §4.1 rates ISO9660 'Low risk' with no multi-extent caveat; grep for 4GiB/multi-extent across RECOVER_WINDOWS.txt, SURVIVABILITY.md, staging/builder.py and tests/ is empty. Windows CDFS not reassembling Level-3 multi-extent files is a well-documented limitation, and the RFC confirms Mount-DiskImage (native CDFS) is the Windows restore path. Likelihood of a ≥4 GiB staged file is modest (catalog.db at scale, tuned pack sizes) — medium is right.

**Fix.** Keep ISO9660 Level 3 + Rock Ridge + Joliet (correct 2050 bet), but enforce the de-facto envelope: add a hard staging-time assertion in staging/builder.py that every staged file is < 4 GiB − 2 KiB, failing the burn with a message naming the file; document the Windows multi-extent limitation in RECOVER_WINDOWS.txt with the workaround (copy the ISO to disk and extract with 7-Zip, which handles multi-extent).

**Tests / gates to add:**
- tests/unit/test_staging_rejects_oversize_file.py: stage a sparse 4 GiB+1 file, assert StagingBuilder raises with the file named; 4 GiB−4 KiB passes.
- tests/recovery_hardening/test_windows_doc_multiextent_caveat.py: static doc pin (same style as test_env_var_docs.py) that RECOVER_WINDOWS.txt documents the >4 GiB/CDFS limitation.

#### 5. [MEDIUM] Tier-1 catalog reader has no schema forward-compat contract — a future schema bump silently degrades multi-disc restores to unhinted 'pack not found'

**CONFIRMED** (verifier confidence: high) — tracked: recovery/docs/DEFERRED_WORK.txt item 1 (disc_index.txt sidecar) covers catalog *corruption*, not version skew — the skew case is untracked.

**Gap.** catalog.c hard-codes schema-v5 SQL (packs.sha256, volume_packs, volumes.label/status). It reads schema_version but only logs it; nothing gates on it. lcsas_catalog_find_pack returns -1 for both 'prepare failed' (schema mismatch) and 'pack not in catalog', so when an heir pairs an older meta-volume binary with newer data discs (archives accumulate across years; discs from different generations coexist in one binder), the disc-swap hint system fails indistinguishably from a missing pack — the heir gets bare 'pack not found' prompts with no volume labels and no explanation, on a multi-disc archive where tier 1 is 'the primary AND the only practical pre-Python fallback' (TIERS.txt). SQLite the file format is a fine 2050 bet (vendored amalgamation, statically linked); the gap is the version-skew contract on top of it. No test exercises a v>5 catalog.

**Evidence.** recovery/src/lcsas-restore/catalog.c:55-62 (version read), :85-98 (find_pack: -1 on prepare error AND on miss), :188-191 (version only logged via fprintf '[catalog] schema v%d'); recovery/src/lcsas-restore/catalog.h:8 ('Schema version 5'); recovery/docs/TIERS.txt:72-74 (tier 1 is the only practical pre-Python path on multi-disc); recovery/tests/test_catalog.c: no future-version fixture (grep v6/future/forward: empty).

**Verifier.** Verified in catalog.c: schema_version read but only fprintf'd in describe() (line 191); lcsas_catalog_find_pack returns -1 for both prepare failure and miss (lines 85-98). Traced the caller: disc_locator.c:723 — on -1 the heir-facing prompt prints '(catalog has no record of this pack hash)', so schema skew is indistinguishable from a genuine miss, exactly as claimed. recovery/tests/test_catalog.c builds only a v5 fixture (line 47-48); no future-version case. Heir can still brute-force disc insertion via the prompt loop, so medium (degraded journey, not dead end) is correctly calibrated.

**Fix.** Define and pin the contract: on open, if schema_version > 5, print one loud diagnostic ('catalog schema v%d is newer than this recovery binary — disc-swap hints disabled; use the restore tooling from the SAME-generation meta disc, or proceed and insert discs when prompted') and degrade gracefully; separate prepare-failure from not-found in find_pack's return convention. Additionally adopt a project policy that future schema bumps remain additive (never rename/drop the v5 columns tier-1 queries), recorded in db/schema.py.

**Tests / gates to add:**
- recovery/tests/test_catalog.c new case: build a synthetic schema_version=6 catalog with renamed columns; assert open succeeds, the newer-schema warning is emitted exactly once, and the locator falls back to directory scanning (runs under existing `make -C recovery test`).
- tests/unit/test_schema_v5_columns_frozen.py: static assertion that the column/table names tier-1 queries (packs.sha256, size_bytes, repo_id; volumes.label/uuid/media_type/status; volume_packs) exist in db/schema.py — fails any future migration that breaks the tier-1 SQL.

*Verifier refinements:*
- Refine the C test: the locator's search-path scanning already continues regardless of catalog state, so don't assert 'falls back to directory scanning' — assert instead that on a v6/renamed-column catalog the misleading '(catalog has no record of this pack hash)' message is replaced by an explicit newer-schema warning, emitted once, and that find_pack's prepare-failure return is distinguishable from not-found.

#### 6. [MEDIUM] ECC capacity claims are internally inconsistent and ~2x inflated ('~30%' documented vs 15% configured)

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** RECOVER.txt tells the recovery operator dvdisaster -f 'restores up to ~30% of unreadable sectors' and READINESS_CHECKLIST.txt repeats 'approximately 30%', but the configured default is 15% redundancy (settings.py), and the project's own format doc correctly states 15% redundancy ≈ ~15% of sectors. RS erasure capacity ≈ nroots/255 ≈ the redundancy fraction, so the real margin is ~13–15%. The inflated figure shapes real decisions: an operator paces re-burn schedules (READINESS readability scan cadence) and an heir triages a failing disc believing they have double the actual margin — delaying re-burn until the disc is past repairable.

**Evidence.** recovery/docs/RECOVER.txt:161 ('This restores up to ~30% of unreadable sectors'); recovery/docs/READINESS_CHECKLIST.txt:95-96 ('can recover approximately 30% of unreadable sectors'); src/lcsas/config/settings.py:32 (`default_ecc_redundancy_pct: int = 15`); docs/DVDISASTER_RS03_FORMAT.md:50 ('15% redundancy ≈ can tolerate ~15% of sectors being unreadable').

**Verifier.** All four cites verified verbatim: RECOVER.txt:161 'restores up to ~30% of unreadable sectors'; READINESS_CHECKLIST.txt:95 'approximately 30%'; settings.py:32 default_ecc_redundancy_pct=15; DVDISASTER_RS03_FORMAT.md:48-50 '15% redundancy ≈ ~15% of sectors'. RS erasure capacity ≈ redundancy fraction, so ~30% is unsupported for a full disc (likely confused with dvdisaster's 33% 'high' preset). Heir-facing triage docs read under stress with a 2x-optimistic margin can delay re-burn past repairability — medium (risk worth fixing), not mere polish. No existing doc-consistency test covers these files.

**Fix.** Correct both docs to derive the figure from the configured redundancy ('with the default 15% redundancy, roughly 13–15% of sectors are repairable; higher redundancy raises this proportionally'), and state it conservatively since heirs will read it under stress.

**Tests / gates to add:**
- tests/recovery_hardening/test_ecc_capacity_claims.py: static doc test (pattern of test_env_var_docs.py) that scans RECOVER.txt and READINESS_CHECKLIST.txt for the repair-capacity percentage and asserts it is consistent with config.settings.default_ecc_redundancy_pct (and that '30%' does not appear), so the docs can never drift from the configured math again.

#### 7. [MEDIUM] Media bets: M-DISC '1000+ years' vendor claim stated as fact, and the 100 GB tier's BDXL drive requirement is never disclosed

**CONFIRMED** (verifier confidence: medium) *(finder said low; verifier corrected)* — tracked: recovery/docs/UX_CONCERNS.txt ID 007 (optical drive rarity, DEFERRED) covers generic drive availability; the BDXL-specific drive class and the M-DISC claim accuracy are untracked.

**Gap.** DISC_CARE.txt (burned onto every disc) states M-DISC is 'Rated for 1000+ years… Best choice for archival' and 'strongly recommended'. Millenniata is defunct, and M-DISC Blu-ray is substantially the same inorganic HTL recording layer as standard BD-R HTL (the same doc rates at 50–100 years) — the 1000-year figure is unverified vendor marketing that may bias archivists toward a price premium instead of toward what actually moves durability (more copies, offsite sets, re-burn cadence). Separately, MDISC100/BDXL100 (100 GB triple-layer) requires a BDXL-capable drive — a strictly rarer class than ordinary BD drives — but the drive-availability advice says only 'Keep at least one USB Blu-ray drive', so an heir (or the archivist stocking the binder) can buy a non-BDXL drive that reads none of the 100 GB discs. Concentrating 4x data per disc also quadruples per-disc blast radius. BD25/M-DISC25 as the recommended default with documented tradeoffs is the better 2050 bet.

**Evidence.** src/lcsas/staging/metadata.py:600-607 ('M-DISC… Rated for 1000+ years… strongly recommended'), :631-637 (drive advice: generic 'USB Blu-ray drive', no BDXL mention); src/lcsas/config/media.py:19-21 (BDXL100/MDISC100 = 100 GB members); docs/ESTATE_PLANNING.md:165 (re-burn every 5–10 years — already the right hedge, but contradicts the 1000-year framing).

**Verifier.** Verified: staging/metadata.py:600-607 states 'Rated for 1000+ years... Best choice' / 'strongly recommended' unqualified; drive section (631-637) says only generic 'USB Blu-ray drive'; grep for 'bdxl' across metadata.py, ESTATE_PLANNING.md, recovery/docs/ is empty while media.py offers BDXL100/MDISC100; UX_CONCERNS 007 is generic drive rarity, WONTFIX. Severity bumped low→medium: the undisclosed BDXL drive-class requirement is a real heir dead-end (replacement non-BDXL drive reads zero 100 GB discs, nothing explains why) — more than polish. M-DISC wording alone would be low.

**Fix.** Reword DISC_CARE.txt: present 1000+ years as 'manufacturer-rated' and note BD-R HTL parity evidence; make redundancy + re-burn cadence the headline durability lever. When the archive's media type is BDXL100/MDISC100, have HolographicInjector add an explicit 'these discs require a BDXL-capable drive — verify the binder drive reads them' line to DISC_CARE.txt and ESTATE_PLANNING checklist.

**Tests / gates to add:**
- tests/unit/test_disc_care_bdxl_caveat.py: build DISC_CARE.txt via HolographicInjector with media_type=BDXL100/MDISC100 and assert the BDXL drive caveat is present (and absent for BD25); assert '1000+ years' is qualified as manufacturer-rated.

*Verifier refinements:*
- Note: DISC_CARE.txt is currently a static string in HolographicInjector — the proposed media-conditional test requires first making generation media-type-aware; scope the fix and test together.

---

## Test & gate coverage map  `tests-gates-map`

> The gate surface is deep locally but the always-run CI slice is thin: test.yml runs only unit+integration+lint+typecheck, so the 58-file recovery-hardening suite ('the final gate that says this build is shippable'), the e2e suite, shell-coverage, and all blind-restore drills are local-only, while audit-gate's path filter leaves vendored sqlite/zstd, the lcsas-keyshare C combiner, and every recovery script (including restore.sh) outside any CI compile or test. Concrete consequences found: the Intel-Mac tier-1 binary is gitignored and absent from the repo (a fresh clone silently produces a 5/6-target meta disc), both macOS binaries are never executed by any test anywhere, the RS03 repair proof is opt-in and never scheduled, the tier-2-fallback blind variant is permanently XFAIL (red score exits 0), and the CI coverage threshold (60) sits 28 points below the documented local floor (88) with a fail-open coverage checker. Each gap admits a cheap closure — mostly new test.yml steps, free macOS runners, a weekly cron, and fail-closed fixes to existing check scripts.

#### 1. [HIGH] Intel-Mac tier-1 binary (recovery/bin/x86_64-macos/lcsas-restore) is gitignored and never committed; meta builder silently skips it and no gate that would catch it runs in CI

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** recovery/.gitignore ignores `bin/*/lcsas-restore`. Five targets were force-added at some point (aarch64, armv7, x86_64, aarch64-macos, x86_64-windows .exe), but x86_64-macos/lcsas-restore exists only on this dev VM's disk — `git ls-files recovery/bin` does not list it. Any fresh clone (including the source tree burned onto the meta disc itself) lacks the Intel-Mac tier-1 binary; `MetaVolumeBuilder._bundle_tier1_binaries` silently `continue`s on missing binaries, so `lcsas meta build` from a clean checkout ships a meta disc with 5/6 tier-1 targets and no error. An heir on an Intel Mac then has no tier-1 path. The test designed to catch exactly this (tests/recovery_hardening/test_meta_bundling_completeness.py::test_tier1_source_binary_present) would fail on a fresh clone — but its docstring's claim that 'CI runs unset and therefore requires all six' is false: no CI workflow runs tests/recovery_hardening/ at all.

**Evidence.** recovery/.gitignore:1 (`bin/*/lcsas-restore`); `git ls-files recovery/bin` lists recovery/bin/x86_64-macos/lcsas-keyshare but NOT recovery/bin/x86_64-macos/lcsas-restore; src/lcsas/meta/builder.py:2161-2162 (`if not src_bin.is_file(): continue`); tests/recovery_hardening/test_meta_bundling_completeness.py:60 ('CI runs unset and therefore requires all six') and :103-104 (checks disk presence only); .github/workflows/test.yml:78-88 runs only test-unit/test-integration/typecheck/lint

**Existing partial coverage.** tests/recovery_hardening/test_meta_bundling_completeness.py::test_tier1_source_binary_present (local-only, disk-presence only — fails on fresh clone, never runs in CI)

**Verifier.** Reproduced: recovery/.gitignore:1 ignores bin/*/lcsas-restore; `git ls-files recovery/bin` lists 5 tier-1 restore bins but NOT x86_64-macos/lcsas-restore (present on disk, May 29). builder.py `if not src_bin.is_file(): continue` confirmed — silent 5/6 meta disc from fresh clone. Docstring claim 'CI runs unset and therefore requires all six' is false: test.yml runs only unit/integration/lint/typecheck; audit-gate's coverage-c runs just 3 tier-1 files with `|| true`, never test_meta_bundling_completeness. Partial local-only coverage: that test fails on a fresh clone but never in CI.

**Fix.** Commit the binary (`git add -f recovery/bin/x86_64-macos/lcsas-restore`) and replace the blanket ignore with explicit negations (`!bin/x86_64-macos/lcsas-restore` etc.) so a missing approved-target binary shows as a dirty/missing tracked file. Make the completeness test assert git-tracked status, not just disk presence.

**Tests / gates to add:**
- Add to tests/recovery_hardening/test_meta_bundling_completeness.py a test_tier1_binary_git_tracked that runs `git ls-files --error-unmatch recovery/bin/<short>/<exe>` for every APPROVED_TIER1_TARGETS entry — fails on this repo today
- Add a CI step to .github/workflows/test.yml: `pytest tests/recovery_hardening/test_meta_bundling_completeness.py -v` (runs in seconds, no external tools) so the six-target contract is enforced on every push

#### 2. [HIGH] The 'shippable build' gate (tests/recovery_hardening, 58 files) and test-e2e never run in CI — restore.sh behavior is effectively ungated on merge

**CONFIRMED** (verifier confidence: high) — tracked: CODE_REVIEW_CLEANUP.md:172-181 has generic unchecked '[ ] Tests run in CI' boxes, but the recovery-hardening/CI split is not called out anywhere

**Gap.** .github/workflows/test.yml runs only make test-unit, test-integration, typecheck, lint. `make gate` (lint+typecheck+test-all incl. e2e and recovery-hardening) is the documented shippability bar but is enforced only by local discipline — there is no pre-push git hook and no CI job for it. All behavioral tests of recovery/scripts/restore.sh (test_restore_sh_ux, test_tier_fallback, test_restore_discovery, test_tier3_*, test_restore_skips_tier2_on_multi_disc, doc-parity tests, etc.) live exclusively in tests/recovery_hardening/. The only CI-run restore.sh test is tests/unit/test_restore_sh_dispatcher.py, which re-runs a hand-copied MIRROR of the dispatcher case block ('MUST stay in lockstep' comment) plus a string-pinning drift guard — it does not execute restore.sh. Combined with the audit-gate path filter excluding restore.sh (finding below), a regression to the heir's single entry-point script merges with zero CI execution of the script.

**Evidence.** .github/workflows/test.yml:78-88 (four steps only); Makefile:36-45 ('the final gate that says this build is shippable' — gate target); .git/hooks contains only samples; tests/unit/test_restore_sh_dispatcher.py:35-37 ('the case block here MUST stay in lockstep with the one in recovery/scripts/restore.sh'); .github/workflows/audit-gate.yml:5-19 path filter omits recovery/scripts/restore.sh

**Existing partial coverage.** Partial: tests/unit/test_restore_sh_dispatcher.py (mirrored case block + drift guard), tests/unit/test_meta_builder.py bash -n syntax check, audit-gate coverage-c Step 3 (3 hardening files, path-triggered, failures swallowed by || true)

**Verifier.** Confirmed: test.yml has exactly 4 gate steps; Makefile gate comment verbatim; .git/hooks has only samples. Suite is 55 test files (not 58) — immaterial. Checked the refutation angle hard: tests/integration/test_meta_volume_restore.py executes restore_legacy.sh (NOT the active 3-tier driver, per its own comment); test_interactive_restore.py drives the production restore.sh but is root-gated (geteuid!=0 skip) so it skips in CI. Only CI touches: dispatcher-mirror unit test and a `bash -n` syntax check (test_meta_builder.py:294). audit-gate conditionally runs 3 tier-1 hardening files via coverage-c with `|| true` (non-fatal). Core claim stands.

**Fix.** Add a `recovery-hardening` job to test.yml that builds the host tier-1 binary (`make -C recovery all test`, ~2 min) and runs `make test-recovery-hardening`. Tests needing qemu/wine/cdemu/live-ISO already skip honestly; assert a minimum passed-count so skip-rot is visible.

**Tests / gates to add:**
- test.yml new step: `make -C recovery && make test-recovery-hardening` with a follow-up `pytest tests/recovery_hardening --co -q | wc -l` style floor (e.g. fail if <40 tests collected/passed) to detect silent mass-skips
- test.yml new step: `make test-e2e` (its tests skip without /mnt/lcsas-data — see separate finding — so also fix that suite's portability)
- Add tests/recovery_hardening/test_ci_workflow_parity.py asserting .github/workflows/test.yml invokes every suite that `make gate` invokes (parses the YAML; same pattern as the existing README-parity tests)

#### 3. [HIGH] macOS tier-1 binaries (both arches) are built but never executed by any test, gate, or CI runner anywhere

**CONFIRMED** (verifier confidence: high) — tracked: recovery/docs/UX_CONCERNS.txt:73-91 (ID 004) tracks macOS Gatekeeper refusal of unsigned binaries — a different problem; the never-executed gap itself is untracked

**Gap.** Cross-arch verification exists for Linux aarch64/armv7 (qemu-user, local-only) and Windows (wine, local-only), but there is no execution path for Mach-O binaries: no macOS CI runner, no test that runs recovery/bin/{aarch64-macos,x86_64-macos}/lcsas-restore or lcsas-keyshare. The only 'macos' references in the test tree are presence checks (meta bundling) and dispatcher string matching (platform detect). A zig-cc regression (wrong -target, broken libSystem stub linkage, Mach-O loader incompatibility) ships green through every gate including the blind restore (which runs on Linux). For an heir restoring on a Mac — arguably the most likely consumer platform — tier 1 is an entirely unproven artifact.

**Evidence.** recovery/Makefile:376-396 (macos targets build-only); grep for macos/darwin across tests/recovery_hardening/ and recovery/tests/ matches only test_meta_bundling_completeness.py and test_restore_platform_detect.py (neither executes a binary); .github/workflows/ contains only test.yml (ubuntu-latest) and audit-gate.yml (ubuntu-latest); compare tests/recovery_hardening/test_tier1_aarch64_qemu.py:45-54 and test_tier1_windows_wine.py:52-60 which exist for the other arches

**Verifier.** Confirmed: recovery/Makefile macos targets (lines 376-397) build+copy only. Grepped all of tests/ and recovery/tests/ for macos/darwin: only presence checks (meta bundling), dispatcher string tests, SUPPORTED_ARCHES asserts, and one cross_build (build, not run) in test_recovery_orchestration.py:179. Both workflows run ubuntu-latest only. qemu/wine execution tests exist for the other 3 cross targets, nothing for Mach-O. Blind restore is Linux/cdemu. Tier-1 on the most likely heir platform has never executed once.

**Fix.** GitHub-hosted macOS runners are free for public/private quota and cover both arches: macos-13 is x86_64, macos-14/15 is arm64. Run the committed binaries against the committed encrypted fixture (recovery/tests/fixtures/repo) — a full list-snapshots + single-file restore takes seconds.

**Tests / gates to add:**
- New .github/workflows/macos-tier1.yml: matrix {macos-13, macos-14}; steps: checkout, `xattr -c recovery/bin/*-macos/*`, run `recovery/bin/<arch>-macos/lcsas-restore --repo recovery/tests/fixtures/repo --password-file <(echo test) --list-snapshots`, then restore one file and sha256-compare; also run lcsas-keyshare with a vector from recovery/tests/gen_keyshare_vectors.py. Runtime ~3 min per arch
- Mirror the qemu/wine pattern with tests/recovery_hardening/test_tier1_macos_native.py (skipif sys.platform != 'darwin') so the same checks run on any future local Mac

*Verifier refinements:*
- In the proposed macos-tier1.yml, use macos-15-intel (not macos-13) for the x86_64 leg — GitHub retired macos-13 hosted runners in late 2025; macos-15-intel is the supported Intel label (available through ~Aug 2027), with macos-14/15 for arm64

#### 4. [HIGH] audit-gate workflow path filter has holes: vendored sqlite/zstd, lcsas-keyshare, lcsas-iso9660, lcsas-init, and all recovery scripts are excluded — and it references a nonexistent sanitize.sh; test.yml never compiles C at all

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** audit-gate.yml triggers only on recovery/src/lcsas-restore/**, recovery/tests/**, recovery/fuzz/**, coverage_check.py, sanitize.sh, and recovery/Makefile. Therefore: (a) a change to recovery/vendored/sqlite/sqlite3.c or vendored zstd — source compiled directly INTO the tier-1 binary — triggers no CI build or test; (b) a change to recovery/src/lcsas-keyshare/slip39.c (the heir-facing SLIP-0039 share combiner; the CI-run unit tests test_keyshare*.py exercise only the Python combiner) triggers nothing — its C test (recovery/tests/test_keyshare.c) only runs inside audit-gate, which the source change does not trigger; (c) lcsas-iso9660/lcsas-init likewise; (d) recovery/scripts/restore.sh, restore.bat, restore_auto.sh, detect_arch.sh, fetch_upstream.sh, exemptions_check.py are unfiltered; (e) the listed recovery/scripts/sanitize.sh does not exist (ls recovery/scripts shows no such file), so that filter entry is dead. Since test.yml contains no C compilation step, a C-breaking change outside lcsas-restore/ produces a fully green CI.

**Evidence.** .github/workflows/audit-gate.yml:5-19 (path lists); `ls recovery/scripts/` has no sanitize.sh; recovery/Makefile:120-135 (keyshare built from recovery/src/lcsas-keyshare/, outside the filter); tests/unit/test_keyshare_combine.py:1-17 (tests lcsas.meta.keyshare_combine Python path, not the C binary); .github/workflows/test.yml has no make -C recovery step

**Existing partial coverage.** Partial: recovery C unit tests exist (recovery/tests/test_*.c) but only execute inside audit-gate, which the excluded paths never trigger

**Verifier.** Confirmed every claim: audit-gate.yml paths are exactly lcsas-restore/**, recovery/tests/**, recovery/fuzz/**, coverage_check.py, sanitize.sh, recovery/Makefile. `ls recovery/scripts/` has no sanitize.sh (dead entry). keyshare builds from recovery/src/lcsas-keyshare/ (Makefile:117-135) — outside filter; its C test test_keyshare.c only runs inside audit-gate. recovery/src has lcsas-init, lcsas-iso9660, lcsas-keyshare also unfiltered; recovery/vendored/{sqlite,zstd} unfiltered. test.yml has zero C compilation. A behavior-breaking edit to heir-facing C (keyshare combiner, vendored decompressor) merges fully green. Minor: there are 17 C test files, not 15.

**Fix.** Broaden the filter to recovery/src/**, recovery/vendored/**, recovery/scripts/**, recovery/MANIFEST.sha256; delete the dead sanitize.sh entry. Independently, add a cheap always-on C smoke step to test.yml so no C change can merge unbuilt.

**Tests / gates to add:**
- audit-gate.yml: replace path list with recovery/src/**, recovery/tests/**, recovery/fuzz/**, recovery/vendored/**, recovery/scripts/**, recovery/Makefile
- test.yml new step 'C smoke': `make -C recovery all test` (builds lcsas-restore + keyshare + iso9660 + init and runs all 15 C unit test binaries; ~2 min, no clang/gcovr needed)
- tests/recovery_hardening/test_workflow_path_filter.py: parse audit-gate.yml and assert every path entry exists in the repo (kills ghost entries like sanitize.sh) and that every directory under recovery/src/ is matched by some filter

#### 5. [HIGH] RS03 ECC repair validation is opt-in (LCSAS_ECC_REPAIR=1) and never runs in CI even though CI installs dvdisaster

**CONFIRMED** (verifier confidence: high) — tracked: Opt-in status is acknowledged in recovery/docs/READINESS_CHECKLIST.txt:100 and root CLAUDE.md ('opt-in LCSAS_ECC_REPAIR=1'), but no ledger flags the absence of any scheduled/CI execution as a gap

**Gap.** The only test proving the disc-integrity layer's core claim — below-threshold damage repairs byte-identical, above-threshold fails loud — is tests/integration/test_ecc_repair.py, skipped unless LCSAS_ECC_REPAIR=1. CI installs dvdisaster (test.yml:61-66) but never sets the variable, so the repair path is validated only when someone remembers locally (and project memory notes it is slow and rarely run). A regression in ecc/dvdisaster.py (argument drift, wrong image targeted, redundancy level change) would silently degrade every disc burned thereafter; the failure surfaces decades later when the heir's damaged disc cannot be repaired — plausible data loss with no tripwire between now and then.

**Evidence.** tests/integration/test_ecc_repair.py:41-43 (`pytest.mark.skipif(not os.environ.get("LCSAS_ECC_REPAIR")`); .github/workflows/test.yml:61-66 installs dvdisaster but no step sets LCSAS_ECC_REPAIR; CLAUDE.md and recovery/docs/READINESS_CHECKLIST.txt:95-101 describe the test as opt-in

**Existing partial coverage.** Partial: tests/unit/test_dvdisaster.py::test_augment_args (CI-run golden-args on the wrapper); tests/integration/test_ecc_repair.py (opt-in local only)

**Verifier.** Confirmed: skipif on LCSAS_ECC_REPAIR at test_ecc_repair.py:41-46; no LCSAS_ECC_REPAIR anywhere in .github/ or Makefile; test.yml installs dvdisaster (lines 61-66) but every other test using DVDisaster injects _NoOpDVDisaster fakes, so the real binary's repair path executes nowhere in CI. One nuance: the finding's second proposal already exists — tests/unit/test_dvdisaster.py::test_augment_args pins -mRS03/-n/redundancy args in CI, covering pure argument drift. The real-repair-byte-identical proof remains opt-in-only; severity high stands (silent integrity gap surfacing decades later).

**Fix.** Run it on a schedule rather than per-push: the repair sweep is slow but a weekly cadence bounds the regression window to days instead of years.

**Tests / gates to add:**
- New .github/workflows/ecc-weekly.yml: `on: schedule: cron '0 4 * * 1'` + workflow_dispatch; installs xorriso+dvdisaster; runs `LCSAS_ECC_REPAIR=1 pytest tests/integration/test_ecc_repair.py -v`; also trigger on pull_request paths src/lcsas/ecc/**
- Cheaper per-push complement in test.yml: a fast assertion that `dvdisaster -c` is invoked with the pinned RS03 flags (unit-level golden-args test against the wrapper, tests/unit/test_dvdisaster.py extension) so argument drift is caught instantly even without the slow repair sweep

*Verifier refinements:*
- Drop the proposed per-push golden-args test — tests/unit/test_dvdisaster.py::test_augment_args already pins -mRS03/-n/redundancy in CI; extend it only if specific flags are unpinned. Keep the weekly scheduled real-repair workflow as proposed

#### 6. [HIGH] Tier-2 fallback under a missing/broken tier-1 has never passed a live blind restore: 'tier1-missing' variant is permanently XFAIL (red score exits 0), and all blind variants are local-only and cost-gated

**CONFIRMED** (verifier confidence: medium) — tracked: Issue #227 plus the in-file comments in run_variant.sh:99-102 and Makefile:129-135; not listed in UX_CONCERNS.txt/DEFERRED_WORK.txt

**Gap.** run_variant.sh defaults LCSAS_VARIANT_XFAIL to 'tier1-missing': the variant simulating the heir's most plausible cascade failure (tier-1 binary won't run on their machine) reports XFAIL and exits 0 on a red score. Comments confirm tier 2 (rustic) cannot drive multi-disc archives (#227 'partial fix: falls to tier-3, but tier-3 disc-swap protocol still needs verification'). So the documented hedge tiers — the whole reason tiers 2/3 exist — are unproven end-to-end, and even when the suite IS run (locally, sudo+cdemu, ~$5/variant, never CI), this path cannot fail the run. Deterministic hardening tests (test_tier_fallback.py, test_restore_skips_tier2_on_multi_disc.py) pin script-level fall-through but not the live multi-disc tier-3 disc-swap journey.

**Evidence.** tests/e2e/cdemu_blind_restore/run_variant.sh:99-113 (XFAIL default + #227 rationale) and :126-128 (XFAIL exits 0); Makefile:148-160 (variants local-only, LCSAS_BLIND_ACK_COST guard); .github/workflows/test.yml:68-73 (cdemu impossible in CI); tests/recovery_hardening/test_tier_fallback.py:1-25 (fallback itself is opt-in LCSAS_TIER_FALLBACK=1)

**Existing partial coverage.** Partial: tests/recovery_hardening/test_tier3_disc_swap.py (deterministic tier-3 swap protocol, rustic-gated local), test_restore_skips_tier2_on_multi_disc.py, and the live-promoted tier1-tier2-missing variant (15/15, cycle 9)

**Verifier.** Core facts verified: run_variant.sh defaults LCSAS_VARIANT_XFAIL=tier1-missing, red score prints XFAIL and exits 0 with no expiry; variants are sudo/cdemu/cost-gated local-only; LCSAS_TIER_FALLBACK is opt-in. One overstatement: 'tiers 2/3 unproven end-to-end' — tier1-tier2-missing (tier-3 takeover incl. disc swaps) was promoted out of XFAIL at 15/15 on 2026-05-28 (cycle 9, PRs #285/#286), and test_tier3_disc_swap.py + test_restore_skips_tier2_on_multi_disc.py pin the protocol deterministically. Residual gap is the tier1-missing live journey, which structurally can never fail any run — keeping high for that permanent fail-open.

**Fix.** Until tier1-missing scores 15/15 live, the TIERS.txt promise that tiers 2/3 are working hedges is unvalidated. Either fix #227's remaining tier-3 disc-swap gap and promote the variant out of XFAIL, or document in TIERS.txt that the fallback path is unverified for multi-disc archives so operators burn accordingly.

**Tests / gates to add:**
- Add a deterministic (non-LLM) e2e: tests/e2e/cdemu_blind_restore/test_scripted_tier3_multidisc.sh — script (not agent) drives restore.sh with tier-1 removed across a 2-disc cdemu set, asserting tier-3 completes the swap protocol and restores byte-identical content; cost $0, runnable in the local gate on every cycle
- Makefile: add an XFAIL-ledger check — a hardening test that fails if LCSAS_VARIANT_XFAIL default is non-empty for more than a pinned issue list (forces each XFAIL to carry an open issue number, mirroring the promotion comments)

*Verifier refinements:*
- Proposed scripted tier-3 multidisc e2e partially duplicates tests/recovery_hardening/test_tier3_disc_swap.py (restorer-level swap protocol already pinned); scope the new test to drive recovery/scripts/restore.sh end-to-end with tier-1 removed across 2 discs, i.e. the dispatcher->tier2-skip->tier3 chain, which no deterministic test covers

#### 7. [MEDIUM] CI audit-gate runs THRESHOLD=60 while the documented/local default is 88, and coverage_check.py fails open when the report matches no files

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** Local `make audit-gate` defaults to THRESHOLD=88 (the 'measured floor... prevents regressions' per AUDIT.md), but audit-gate.yml explicitly passes THRESHOLD=60 — a tier-1 coverage collapse of up to ~28 points per file merges green, and AUDIT.md:320 falsely states CI runs 'the default threshold'. Separately, coverage_check.py returns exit 0 with only a WARNING when zero src/lcsas-restore/*.c entries appear in coverage.json (lines 75-77) — so any gcovr filter/path drift that empties the report silently disables the entire threshold gate instead of failing it (project memory already records gcovr flag drift breaking coverage-c once).

**Evidence.** .github/workflows/audit-gate.yml:46 (`THRESHOLD=60`); Makefile:211 and recovery/Makefile:827 (`THRESHOLD ?= 88`); recovery/docs/AUDIT.md:35 ('88% (default)... Prevents regressions') vs AUDIT.md:320 ('runs ... with the default threshold'); recovery/scripts/coverage_check.py:75-77 (`if not rows: ... return 0`)

**Verifier.** All four evidence points verified verbatim: audit-gate.yml:46 `THRESHOLD=60`; Makefile:211 and recovery/Makefile:827 `THRESHOLD ?= 88`; AUDIT.md coverage table '88% (default)... Prevents regressions' vs AUDIT.md ~320 'runs ... with the default threshold' (false); coverage_check.py `if not rows: WARNING ... return 0` fail-open — exactly the gcovr-drift failure mode project memory already recorded once. Medium is correctly calibrated: local gate keeps the 88 floor, but CI silently tolerates 28-point collapse and a fully empty report.

**Fix.** Raise the CI threshold to the highest value that passes reliably on the runner (the audit-gate.yml:36-42 comment says CI lands ~5 pts below local, suggesting ~83); make an empty-report a hard failure; fix the AUDIT.md sentence.

**Tests / gates to add:**
- audit-gate.yml: THRESHOLD=83 (or measured CI floor) with a comment tying it to the local 88 contract
- coverage_check.py: change the no-rows branch to `return 1`; add tests/unit/test_coverage_check_fail_closed.py feeding it a coverage.json with no lcsas-restore entries and asserting exit 1
- tests/recovery_hardening/test_audit_gate_threshold_parity.py: parse audit-gate.yml and recovery/Makefile, assert CI threshold >= documented floor minus a pinned tolerance

#### 8. [MEDIUM] No gate verifies that the committed recovery/bin artifacts were built from current source — stale tier-1/keyshare binaries can ship on every meta disc

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** The binaries the meta disc actually bundles are the committed files in recovery/bin/<arch>/, regenerated manually (e.g. commit 9dd1b8a 'regenerate lcsas-keyshare bins'). Timestamps already diverge (x86_64 lcsas-restore May 28; aarch64/armv7 May 21; aarch64-macos May 20 — same source tree, four build dates). Most tests build fresh binaries from source (recovery/build/), so a fix landing in C source with stale committed bins passes every source-level gate while the heir receives the unfixed binary. Only the local-only qemu/wine tests touch committed bins, and nothing compares them to the source they claim to embody. repro-check (recovery/Makefile:460-475) only compares two fresh local builds, not bin/ vs source.

**Evidence.** ls -la recovery/bin/*/ shows divergent mtimes per arch (May 20/21/28/29, Jun 4); git log 9dd1b8a (manual regeneration commit); recovery/Makefile:460-475 (repro-check scope); tests/recovery_hardening/test_tier1_unit.py et al. point at build/lcsas-restore via LCSAS_RESTORE_BIN (recovery/Makefile:507)

**Existing partial coverage.** Partial: test_tier1_aarch64_qemu.py/test_tier1_armv7_qemu.py/test_tier1_windows_wine.py run committed bins (local, toolchain-gated); blind-restore runs committed x86_64 bin (local, cost-gated)

**Verifier.** Confirmed: bin mtimes diverge per arch (May 20/21/28/29, Jun 4); commit 9dd1b8a is a manual regen; repro-check (recovery/Makefile:459-475) compares two fresh builds only, never bin/ vs source; coverage-c points hardening tests at build/lcsas-restore via LCSAS_RESTORE_BIN. Partial mitigations exist but are local-only: qemu/wine tests default to committed bin/aarch64|armv7 binaries, and blind restores exercise the committed x86_64 binary via the meta disc — none compare against current source. Medium correct.

**Fix.** Exploit the reproducible-build property: zig cross-builds with pinned SOURCE_DATE_EPOCH are deterministic, so committed bins can be byte-compared against a clean rebuild in CI. Failing that, at minimum compare each committed binary's --version output to recovery/VERSION.

**Tests / gates to add:**
- New CI job (audit-gate.yml or weekly): `pip install ziglang; SOURCE_DATE_EPOCH=<pinned> make -C recovery keyshare-arches bin/x86_64/lcsas-restore ...` then `git diff --exit-code recovery/bin/` — any drift between source and committed artifacts fails the build (~5-8 min)
- Cheaper per-push fallback: tests/recovery_hardening/test_bin_version_parity.py — run each runnable committed binary (host x86_64 directly, aarch64/armv7 via qemu if present) with --version and assert it equals recovery/VERSION

*Verifier refinements:*
- For the rebuild-and-diff CI job, pin the exact ziglang/zig version (and SOURCE_DATE_EPOCH) used for the committed bins — zig version drift alone breaks byte-identity and would make the gate flaky rather than meaningful

#### 9. [MEDIUM] shell-coverage gate for restore.sh: documented 90% threshold actually enforced at 60%, pytest failures swallowed with `|| true`, and the target is wired into neither `make gate` nor CI

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** Makefile comment states 'Threshold: 90%' but the invocation passes `--threshold 60`. The pytest run that generates the trace ends in `|| true`, so the gate cannot distinguish 'tests passed, low coverage' from 'tests crashed at collection, near-zero trace' until coverage happens to dip under 60. And since shell-coverage is absent from both the `gate` target and CI, restore.sh — the heir's entry point — has no enforced line-coverage floor anywhere. A large untested branch (e.g. a new tier-dispatch path) can land with the trace silently shrinking.

**Evidence.** Makefile:57 ('Threshold: 90%') vs Makefile:72-73 ('--threshold 60'); Makefile:68-70 (`pytest ... || true`); Makefile:44 (gate: lint typecheck test-all — no shell-coverage); .github/workflows/test.yml contains no shell-coverage step

**Verifier.** All confirmed directly in the root Makefile: comment 'Threshold: 90%' (line 57) vs `--threshold 60` (line 72); pytest trace run ends `|| true` (line 70) so a collection crash yields a near-empty trace that only fails if it dips under 60; `gate: lint typecheck test-all` (line 44) excludes shell-coverage; no shell-coverage step in test.yml. restore.sh has no enforced coverage floor anywhere always-on. Medium correctly calibrated.

**Fix.** Reconcile the threshold (raise flag to 90 or fix the comment to the real measured floor), remove `|| true` (the trace-generation run should fail loud), and add shell-coverage to the recovery-hardening CI job once that exists (it needs only bash + the hardening subset, ~1-2 min).

**Tests / gates to add:**
- Makefile shell-coverage: drop `|| true`, set --threshold to the documented value, and add `shell-coverage` as a dependency of `gate`
- test.yml: append `make shell-coverage` to the recovery-hardening job proposed in finding 2

#### 10. [LOW] tests/e2e/test_scripts.py hard-skips off-host (/mnt/lcsas-data LV), making `make test-e2e` a silent no-op everywhere except the author's machine

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** The only test in tests/e2e/ outside the blind-restore directory skips unless the machine-specific /mnt/lcsas-data LV exists. `make gate` therefore reports test-e2e green on any other machine (or CI, if ever added) while executing zero e2e assertions — the suite's name implies pipeline-level protection it does not deliver portably. Anyone reading `gate: ... test-all` (which includes test-e2e) gets false confidence.

**Evidence.** tests/e2e/test_scripts.py:38-41 (`skipif not Path("/mnt/lcsas-data").exists()`); Makefile:26-27 and 39 (test-e2e inside test-all/gate); scripts/e2e_test.py uses the hardcoded base per the docstring at tests/e2e/test_scripts.py:4-8

**Verifier.** Confirmed: skipif `not Path("/mnt/lcsas-data").exists()` at tests/e2e/test_scripts.py:38-41; scripts/e2e_test.py hardcodes BASE = Path("/mnt/lcsas-data") (line 39) with no --base flag; test_scripts.py is the only pytest-collected file in tests/e2e/ (cdemu_blind_restore is shell-driven, not collected); test-e2e is inside test-all/gate so the gate reports green-by-skip off-host. Low correctly calibrated — portability friction, no integrity exposure.

**Fix.** Parameterize the base directory (env var defaulting to a tmpdir on any filesystem large enough for TEST_TINY media) so the e2e pipeline test can run in CI with real rustic+xorriso, which test.yml already installs.

**Tests / gates to add:**
- Refactor scripts/e2e_test.py to honour LCSAS_E2E_BASE (default /var/tmp/lcsas-e2e) and change the skipif to a disk-space check; then add `make test-e2e` to test.yml — with TEST_TINY media it should run in ~1-2 min with the binaries CI already installs

---

## Tier-1 C restore binary  `tier1-c`

> The tier-1 crypto primitives (AES, Poly1305, SHA-256, scrypt, CTR) and the auth ordering are sound: every index/snapshot/tree/pack blob is Poly1305-MAC-verified during decrypt and SHA-256-verified against its declared id BEFORE any plaintext reaches the target file, so corrupt or forged data is rejected rather than silently restored. The most serious correctness gap is not in the crypto but in the hand-rolled JSON layer: fixed token-buffer caps (16384/32768/65536) turn ordinary large index files, wide directories, or large files into a hard restore failure -- and one of those modes is a SILENT skip that drops files -- while the headline petabyte test is constructed in a way that sidesteps exactly this limit. Secondary gaps: unchecked signed-integer overflow (UB) in the int decoder that no fuzz target exercises, a FORMAT.txt-vs-code divergence on path-safety guarantees, an entirely unfuzzed tree-walk with unbounded recursion, and a 256 MiB decompress cap that silently skips oversized index files. None break the auth boundary, but several silently fail or abort the heir's restore with a generic message.

#### 1. [HIGH] Fixed JSON token caps make ordinary large directories, large files, and dense index files un-restorable on tier-1 -- and one path drops files silently; the petabyte test sidesteps the limit

**CONFIRMED** (verifier confidence: high) — tracked: Partially: recovery/docs/AUDIT_FINDINGS.md:113 notes tree.c corrupted-blob paths are uncovered and 'would need ... a fuzz target on lcsas_tree_restore directly' (which does not exist). The token-cap scaling cliff itself, and the silent index-skip data-loss mode, are untracked.

**Gap.** The parser allocates a FIXED token array and bails the moment it fills: index pass-1 = 16384 tokens, pass-2 = 32768 (repo.c:481-482), tree blobs = 65536 (tree.c:781). alloc_tok returns -2 at the cap (json_q.c:27-29) and lcsas_json_parse propagates that negative (json_q.c:228-229). Two divergent failure modes result. (a) TREE blobs: a single restic tree blob holds ALL nodes of one directory, and a file node's `content` array (one token per ~1 MB chunk) lives in the SAME 65536 budget. A file node costs ~21-24 tokens (name/type/mode/mtime/uid/gid/size/inode/links/device_id + content array), so a directory of >~2700 entries, OR a single multi-GB file (e.g. a 60 GB VM image = tens of thousands of 1-8 MB chunks = tens of thousands of content tokens), overflows the parse. tree.c:785 then `goto out` with rc=-1, bubbling to main.c:504 'ERROR: tree restore failed' -- the WHOLE restore aborts, with no hint that the cause is a big folder/file or that tier-2 would succeed. (b) INDEX files: each blob entry costs ~9-11 tokens (parse_blob_entry id/offset/length/type/uncompressed_length, repo.c:422-450), so an index file describing >~2978 blobs overflows the 32768 pass-2 buffer; repo.c:575-576 then `if (ntoks <= 0) { free(plain); continue; }` SILENTLY SKIPS the entire index file, dropping every blob it referenced. The restore then appears to proceed but later dies with 'blob not in index' (tree.c:637) for random files -- or, worse, drops files whose only index entry was in the skipped shard. Real restic index files routinely aggregate far more than ~3000 blobs. This is exactly the cold-storage / petabyte scale LCSAS targets, and tier-1 is the DURABLE path the heir is told to trust.

**Evidence.** repo.c:481-482 (calloc 16384 / 32768 token buffers); repo.c:521-522 and repo.c:575-576 (`if (ntoks <= 0) { free(plain); continue; }` silently skips a whole index file on overflow); tree.c:781,784-785 (malloc 65536, `if (ntoks <= 0 ...) goto out;` rc stays -1); main.c:504-507 ('ERROR: tree restore failed'); json_q.c:27-29,228-229 (cap -> -2 -> negative return); parse_blob_entry repo.c:422-450 (per-blob token cost); the petabyte test creates 3000 *undecryptable* index files (tests/recovery_hardening/test_tier1_petabyte_fixture.py header: BUG-3 regression, fake index JSON that fails decrypt) and spreads only ~1k files, so it never feeds ONE valid dense index or a wide directory; recovery/tests/fixtures/gen_fixture.py writes a single tiny index/tree; grep across tests/ and recovery/tests/ finds no wide-directory or many-content-chunk fixture.

**Existing partial coverage.** None. test_tier1_petabyte_fixture.py uses undecryptable stubs / split indexes; test_tier1_vs_tier2_differential deep/wide profiles are tiny (50 files, 6 levels).

**Verifier.** All citations verified: repo.c:481-482/521-522/575-576 (silent continue on ntoks<=0), tree.c:781-785, json_q.c:27-29. gen_fixture.py ITSELF documents the cliff ('~3500 blob entries max per file', 'no more than ~3000 files' per subtree) and splits fixtures to dodge it, so no test feeds a dense index/wide dir. restic/rustic flush index files at ~50k blobs, so real repos overflow routinely. restore.sh defaults LCSAS_TIER_FALLBACK=0 → tier-1 failure aborts. One correction: end state is a loud abort ('blob not in index' → 'tree restore failed', non-zero exit), never exit-0 file drop — 'drops files' is overstated. High stands.

**Fix.** Make the JSON parse size-adaptive: on a cap hit, re-parse with a buffer sized from the plaintext length (tokens are bounded by input bytes) rather than aborting; at minimum, treat an index-file parse overflow as a FATAL loud error (matching BUG-4's supersedes fail-loud), never a silent `continue`, and emit a specific 'directory/file too large for tier-1; use tier-2 (rustic)' message instead of generic 'tree restore failed'. Document the real per-tree/per-index limits in FORMAT.txt.

**Tests / gates to add:**
- Add tests/recovery_hardening/test_tier1_wide_directory.py: generate a valid encrypted fixture with one directory of 5000 file nodes and assert lcsas-restore either restores all 5000 or exits non-zero with a message naming the size limit (never a partial/silent success).
- Add tests/recovery_hardening/test_tier1_large_file.py: a single file whose content array has 40000 chunk ids; assert full byte-identical restore or explicit size-limit error.
- Add tests/recovery_hardening/test_tier1_dense_index.py: ONE valid index file referencing >5000 blobs; assert no blob is silently dropped (restore byte-identical) -- this is the case the current petabyte test does not cover.
- Add a tree fuzz target recovery/fuzz/fuzz_tree_restore.c driving lcsas_tree_restore on a decrypted-tree corpus and wire it into make fuzz-smoke / audit-gate.

*Verifier refinements:*
- Dense-index test: build ONE valid index file with >5000 blob entries via gen_fixture (remove the deliberate ORPHANS_PER_FILE=3000 split); assert non-zero exit with a message naming the index file or full restore — current behavior is a generic downstream abort, not exit-0 partial restore, so word the assertion as 'no generic/cryptic failure', not 'no silent success'.

#### 2. [MEDIUM] Unchecked signed-integer overflow (UB) in lcsas_json_decode_int on offset/length/size/uncompressed_length, and no fuzz target ever exercises it

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** lcsas_json_decode_int accumulates `v = v * 10 + (c - '0')` with NO overflow guard (json_q.c:362-367). Signed long long overflow is undefined behaviour. This decoder is the sole numeric parser for security-relevant fields read off disc: blob `offset`/`length`/`uncompressed_length` (repo.c:434-435,447), file `size` (tree.c:502,705), `inode`/`mode`/`uid`/`gid` (tree.c). A long digit run produces UB, and on the offset/length path the overflowed values are consumed BEFORE the SHA-256 check: `need_end = (long long)loc->offset + (long long)loc->length` (repo.c:881) can itself overflow and bypass the truncation guard (repo.c:882), and `malloc((size_t)loc->length)` (repo.c:891) can attempt a wild allocation. Downstream the per-blob SHA-256 (repo.c:958-959) still rejects wrong bytes, so this is defense-in-depth-protected against silent corruption, but it is genuine UB that a -fsanitize=undefined build would trap -- and the JSON fuzz harness never triggers it because fuzz_json_parse.c only calls lcsas_json_decode_string on STRING tokens (fuzz_json_parse.c:39-44), never lcsas_json_decode_int on NUMBER tokens.

**Evidence.** json_q.c:352-370 (no overflow check in the multiply-accumulate loop); repo.c:434-435,447 (offset/length/uncompressed_length via decode_int); repo.c:881-882,891-893 (need_end arithmetic + malloc consume these before hash check); fuzz_json_parse.c:39-58 (walks STRING tokens + obj_get only; decode_int absent); `grep -rl decode_int recovery/fuzz/` returns nothing.

**Existing partial coverage.** Partial: .github/workflows/audit-gate.yml runs sanitize + fuzz-json-smoke always-on, but no input ever reaches decode_int overflow.

**Verifier.** Confirmed: json_q.c:362-367 has no overflow guard; repo.c:434-437 feeds offset/length into need_end arithmetic (repo.c:881, itself overflowable) and malloc (repo.c:891) before the SHA check (repo.c:958). fuzz_json_parse.c walks only STRING tokens; grep confirms decode_int absent from recovery/fuzz/; test_json.c's only decode_int case is 32768. Mitigating fact the finding acknowledges: input is Poly1305-MAC-verified plaintext, so triggering requires the repo key or a writer bug — defense-in-depth UB, not an attack surface. Always-on audit-gate CI runs sanitize+fuzz-smoke but neither path reaches decode_int with long digit runs. Medium is right.

**Fix.** Add an overflow guard to lcsas_json_decode_int (reject when v would exceed LLONG_MAX/10 or the final add overflows) and return -1; additionally clamp/validate offset>=0 and length within file size before the malloc/pread in read_blob. Extend the JSON fuzz harness to call decode_int on every NUMBER token so UBSan covers it.

**Tests / gates to add:**
- Extend recovery/fuzz/fuzz_json_parse.c to call lcsas_json_decode_int on every LCSAS_JSON_NUMBER token, and add a seed corpus entry with a 40-digit number; run under -fsanitize=fuzzer,undefined in make fuzz-json-smoke.
- Add recovery/tests/test_json.c case: decode_int on '99999999999999999999999999' returns -1 (rejected) rather than wrapping.
- Add a unit assertion in test_repo.c that an index entry with an astronomically large `length` is rejected before malloc/pread (no crash, clean error).

#### 3. [MEDIUM] FORMAT.txt promises path-safety guarantees the binary does not enforce (absolute symlinks allowed; NUL-in-name not rejected and silently truncates)

**CONFIRMED** (verifier confidence: high) — tracked: Partially: the absolute-symlink behaviour change is referenced in a path.c code comment as issue #187, but FORMAT.txt was not reconciled and the NUL-name divergence is untracked.

**Gap.** FORMAT.txt PATH SAFETY (the durable spec an heir's auditor would trust) states: 'REJECT any name that contains / or .. or NUL bytes' and 'REJECT symlinks whose linktarget is absolute or whose lexically-resolved target escapes the restore root' (FORMAT.txt:162-164). The code does NEITHER for those two cases. (a) lcsas_path_safe_symlink returns 0 (ALLOWED) for any absolute target (path.c:113-122) -- a deliberate #187 change to match rustic, but FORMAT.txt was never updated, so the spec claims a containment property the binary abandoned. A restored tree can therefore contain symlinks pointing anywhere on the filesystem. (b) lcsas_path_safe_name has no NUL check (path.c:14-41); a JSON name containing   is decoded as a literal NUL into name_buf (json_q.c:323-325 emits the byte), and snprintf('%s/%s', dir, name) (tree.c:838) then C-string-truncates at the NUL. So a name 'report .txt.exe' and 'report' map to the SAME on-disk path -- two distinct snapshot entries silently collide and overwrite, a quiet data-loss / wrong-content mode an heir would never notice.

**Evidence.** recovery/docs/FORMAT.txt:159-164 (spec: reject NUL names + absolute symlinks); path.c:113-122 (absolute symlink -> `return 0`); path.c:14-41 (lcsas_path_safe_name: checks '/', '..', empty -- no NUL); json_q.c:312-336,342-345 (  and raw bytes copied verbatim into out buffer); tree.c:838 (snprintf join truncates at embedded NUL).

**Existing partial coverage.** Partial: absolute-symlink change tracked only as the #187 code comment; NUL divergence untracked.

**Verifier.** Confirmed both halves. FORMAT.txt PATH SAFETY ('REJECT any name that contains / or .. or NUL bytes'; 'REJECT symlinks whose linktarget is absolute') vs path.c:114-122 which returns 0 for absolute targets (deliberate #187, comment present, spec never reconciled) and lcsas_path_safe_name (path.c:14-41) which takes a C string so it structurally cannot see an embedded NUL; json_q.c decode emits the NUL byte and tree.c:813-816 discards the returned length, then snprintf at tree.c:838 truncates. Collision/overwrite requires a repo-key-holding writer (trees are MAC-verified), so medium, not high. No NUL cases in test_path.c.

**Fix.** Reconcile the spec and code: either re-reject absolute symlink targets (restoring containment) or update FORMAT.txt to state plainly that absolute symlinks are restored as-is and containment is NOT guaranteed. Add an explicit reject for any decoded name/linktarget containing a NUL byte in lcsas_path_safe_name (return the decoded length and check it equals strlen, or scan for 0).

**Tests / gates to add:**
- Add recovery/tests/test_path.c cases: lcsas_path_safe_name('a\0b') (via a decoded buffer with embedded NUL) returns -1; lcsas_path_safe_symlink with absolute target asserts the documented behaviour and a matching FORMAT.txt line.
- Add a doc-consistency gate (Makefile target or pytest) that greps FORMAT.txt PATH SAFETY claims against path.c behaviour so the two cannot drift again.
- Extend recovery/fuzz/fuzz_path_safe.c corpus with NUL-embedded and absolute-target inputs.

#### 4. [MEDIUM] Tree-restore walk has no fuzz coverage and recurses on the C stack with no depth bound -- a deep directory tree crashes tier-1 with SIGSEGV and no message

**CONFIRMED** (verifier confidence: high) — tracked: Partially tracked as a coverage gap in AUDIT_FINDINGS.md:113; the crash-on-deep-tree behaviour and the absence of any tree fuzz harness are not addressed by an action item.

**Gap.** tree_restore_recurse calls itself for every subdirectory (tree.c:887) and holds a freshly malloc'd 65536-token buffer (~1.5 MB) live at every level until its child loop returns. The file header openly acknowledges 'for very deep trees this could overflow' and tells the operator to run 'ulimit -s unlimited' (tree.c:6-11) -- but a non-technical heir running restore.sh will not do that. A pathologically deep tree (authenticated, but a legitimately deep real tree or a hostile-but-password-held one) exhausts the C stack and/or heap and the process dies with SIGSEGV and zero diagnostic, indistinguishable to the user from 'the software is broken'. The entire tree-walk (lcsas_tree_restore) is also completely unfuzzed: recovery/fuzz/ has harnesses for b64, json_parse, path_safe, repo_strip_v2, and zstd_decode, but nothing drives the recursive restore over crafted/decrypted tree blobs, and AUDIT_FINDINGS.md:113 confirms those paths are the uncovered remainder.

**Evidence.** tree.c:1-11 (header comment admitting stack-overflow risk + ulimit workaround); tree.c:781 (1.5 MB toks per frame), tree.c:887-892 (unbounded self-recursion); `ls recovery/fuzz/*.c` shows no tree harness; recovery/docs/AUDIT_FINDINGS.md:113 ('Remaining lines are corrupted-blob-content paths ... would need ... a fuzz target on lcsas_tree_restore directly').

**Existing partial coverage.** Partial: AUDIT_FINDINGS.md:113 tracks the tree.c coverage gap; no test or harness covers depth.

**Verifier.** Confirmed: tree.c:6-11 header openly admits the overflow risk and prescribes 'ulimit -s unlimited'; self-recursion at tree.c:887; the 65536-token buffer (~2.6 MB at sizeof tok ~40B, slightly larger than the finding's 1.5 MB) is freed only at out: (tree.c:987) AFTER child recursion, so heap is O(depth × 2.6 MB) — deep trees can OOM (clean -1) or SIGSEGV on stack (~5-6 KB/frame). No fuzz_tree_restore.c exists; AUDIT_FINDINGS.md:113 confirms the gap. Differential deep_tree profile is 6 levels only. Requires pathological-but-authenticated depth, so medium fits.

**Fix.** Convert tree recursion to an explicit heap-allocated work stack (or add a hard depth cap, e.g. 1024, that fails LOUDLY with a clear message rather than crashing), and free the token buffer before recursing into children so per-level memory is O(1). Add the missing tree fuzz target.

**Tests / gates to add:**
- Add tests/recovery_hardening/test_tier1_deep_tree.py: a 4000-level-deep directory fixture; assert lcsas-restore either restores it or exits non-zero with a depth-limit message -- never SIGSEGV.
- Add recovery/fuzz/fuzz_tree_restore.c over a corpus of decrypted tree-blob JSON and gate it in make fuzz-smoke / audit-gate.
- Add a depth-cap unit assertion in recovery/tests/test_repo.c or a new test_tree.c.

#### 5. [LOW] 256 MiB decompression cap silently skips oversized index files, surfacing later as cryptic 'blob not in index'

**CONFIRMED** (verifier confidence: high) *(finder said medium; verifier corrected)* — **untracked** (not in any known-issues ledger)

**Gap.** decrypt_repo_file rejects any zstd frame whose decompressed size exceeds 256 MiB (repo.c:384-390) and returns NULL. In lcsas_repo_load_index both passes treat a NULL plaintext as `if (!plain) continue;` (repo.c:519,573), so an oversized index file is SILENTLY DROPPED along with every blob it described. The restore then dies far downstream with 'blob not in index' (tree.c:637) for whatever files those blobs backed -- with no indication that the real cause was an index file exceeding the decompress cap. At the documented 'petabyte / hundreds of discs' scale a single aggregated index file plausibly approaches or exceeds 256 MiB decompressed. The same 256 MiB cap on inline data blobs (repo.c:940) is more defensible (restic chunks are small), but for index files the cap converts an unusual-but-valid repo into an opaque failure.

**Evidence.** repo.c:384-390 ('zstd frame ... reports invalid size' -> return NULL when dsz > 256*1024*1024); repo.c:519,573 ('if (!plain) continue;' silently skips the index file); tree.c:636-639 (downstream 'blob not in index' with no link back to the skipped index); repo.c:940 (same 256 MiB cap for inline blobs).

**Verifier.** Code path confirmed (repo.c:384-390 → NULL; repo.c:519/573 'if (!plain) continue;'), but two corrections. (1) Not silent: the cap path prints 'ERROR: zstd frame at %s reports invalid size %ld' naming the file before the skip — the non-fatal continue and cryptic downstream failure are real, the 'SILENTLY DROPPED' claim is not. (2) Implausible scenario: restic/rustic flush index files at ~50k blobs (~10 MB JSON), and finding 1's 32768-token cap already kills any index past ~3500 entries (~350 KB) — the 256 MiB index cap is unreachable in practice. Downgrade to low; fix folds into finding 1's fail-loud load_index change.

**Fix.** Distinguish 'too large to decompress' from 'decrypt failed' in load_index and make the former a FATAL, clearly-worded error ('index file X exceeds the 256 MiB tier-1 decompress cap; use tier-2') instead of a silent skip; consider raising the index-file cap independently of the blob cap, or streaming index parse.

**Tests / gates to add:**
- Add recovery/tests/test_repo.c case (or a hardening test) with an index file that decompresses just over 256 MiB and assert lcsas-restore exits with an explicit cap message naming the file, not a silent skip + later 'blob not in index'.
- Add an assertion that an oversized index never causes a silently-incomplete-but-exit-0 restore (exit code must be non-zero).

*Verifier refinements:*
- Fold into finding 1's fail-loud fix: a unit case in test_repo.c asserting lcsas_repo_load_index returns an error (not a skip) when decrypt_repo_file fails for a size reason suffices; a literal >256 MiB fixture is unnecessary and slow.

---

## Bootable meta-volume / live-distro method  `boot-live-distro`

> VERDICT: DROP the bootable meta-volume as a supported recovery path and REPLACE the "no working OS" story with the already-solid host-OS path plus a documented "use any contemporary live-Linux USB" procedure. The boot stack is aspirational scaffolding, not a working feature: no kernel, busybox, or FreeBSD artifact has ever been built or pinned; the CLI flag BOOT.txt documents (`--recovery-boot`) does not exist; the two half-built stacks (Alpine live + C89 recovery boot) have mutually inconsistent boot configs that would fail even if a kernel existed; and there is zero boot testing at any level (all tests use fake byte-string artifacts). Worse, heir-facing RECOVER.txt sends an OS-less heir to "boot the recovery medium" — a dead end on every disc the pipeline actually produces — and the designed boot flow restores into a 256 MB RAM tmpfs that evaporates on power-off. A 2026-frozen kernel/initramfs would also age badly against 2035-2050 hardware (Secure Boot default-on with no shim/signing, x86_64-only UEFI, dead legacy-BIOS isolinux, no ARM/Apple Silicon story), whereas the 6-target tier-1 host-OS path rides future OSes' drivers for free; keeping the boot path would require permanent kernel maintenance plus a CI QEMU gate, with the cost-benefit clearly against it.

#### 1. [HIGH] Bootable meta-volume is unreachable scaffolding, yet heir-facing docs route the no-OS scenario to it

**CONFIRMED** (verifier confidence: high) — tracked: Partially: recovery/docs/README.txt:85-87 ('boot test deferred'); READINESS_CHECKLIST.txt:23-30 unchecked manual boot test. The phantom --recovery-boot flag and RECOVER.txt dead-end routing are untracked.

**Gap.** An heir whose machine has no working OS is told by the on-disc decision flow to 'Boot the recovery medium directly. See BOOT.txt' — but no disc the pipeline produces is bootable. `lcsas meta build` exposes only --output/--project-root; `bootable` defaults to False and is never settable from the CLI; the rebuild command BOOT.txt documents (`lcsas meta build --output meta/ --recovery-boot`) does not exist. No kernel, no initramfs .cpio.gz, no FreeBSD loader/kernel, no busybox exists anywhere in the repo or recovery/bin/. The heir sets BIOS to boot from optical, gets 'no bootable device', and has no idea the documented path is fictional.

**Evidence.** src/lcsas/cli/main.py:387-398 (meta build args); main.py:1951-1956 (no bootable=); src/lcsas/meta/builder.py:1647 ('bootable: bool = False'); recovery/docs/BOOT.txt:67 (phantom --recovery-boot); recovery/docs/RECOVER.txt:21; START_HERE.txt heredoc builder.py:~2444-2455 ('Boot directly from the disc... press F12'). Verifier additionally confirmed: BootableISOBuilder's only caller is gated on the never-set flag; no El Torito records on default-built ISOs.

**Existing partial coverage.** Prose acknowledgements only; no automated coverage.

**Verifier.** Run-1 per-finding verifier: every cited fact verified against the repo; worse than stated (START_HERE.txt ships the same dead end on-disc).

**Fix.** DROP the bootable-disc method from the supported story. Rewrite RECOVER.txt + START_HERE 'no working OS' branch: (a) use any other computer (6-target restore.sh), or (b) boot any contemporary Secure-Boot-signed live-Linux USB and run restore.sh from the disc. Move recovery/boot/ + meta/live to an explicitly experimental area; rewrite BOOT.txt as the live-USB procedure.

**Tests / gates to add:**
- tests/recovery_hardening/test_boot_docs_reality.py (always-on): extract every `lcsas ...` invocation from recovery/docs/*.txt AND the START_HERE/README_RESTORE heredocs in builder.py; assert each subcommand/flag exists in the argparse tree (reds immediately on BOOT.txt:67).
- tests/recovery_hardening/test_no_boot_deadend_routing.py (always-on): assert neither RECOVER.txt's no-OS branch nor the START_HERE heredoc instructs booting the meta disc until a CI-tested boot path exists; assert they reference live-USB + other-computer procedures.
- If KEEP instead: tests/e2e/test_boot_qemu.py (LCSAS_BOOT_SMOKE=1 + weekly CI) booting the built ISO under qemu-system-x86_64 + OVMF asserting '[lcsas-init]' on serial console; plus xorriso -report_el_torito structural gate.

#### 2. [HIGH] build_initramfs.sh silently ships zero-byte placeholders for missing binaries (busybox missing for every arch) and exits 0

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** The initramfs manifest requires bin/{{ARCH}}/busybox, which exists for no architecture. build_initramfs.sh handles a missing source with a WARN and a zero-byte placeholder, then exits 0 — verified live: it produced a 3.4 MB initramfs containing a 0-byte /bin/busybox with all 15 shell-tool symlinks pointing at it. If this initramfs ever boots, exec of busybox fails, the /bin/sh fallback is a symlink to the same empty file, and PID 1 enters the 'FATAL: no shell available' infinite sleep loop — a black screen for the heir. Docs contradict reality: README.txt declares 'Phase 2 (Hardening): COMPLETE / Userland: BusyBox static'.

**Evidence.** recovery/boot/initramfs/manifest.txt:32; build_initramfs.sh:42-51 (': > $STAGING$target' placeholder, rc=0); recovery/src/lcsas-init/init.c:136-155 (handoff + FATAL sleep loop); live repro: rc=0, cpio lists 0-byte bin/busybox; meta/bootable.py _validate_inputs only checks the cpio EXISTS.

**Verifier.** Run-1 per-finding verifier: reproduced live; sole missing source is busybox; for riscv64 every binary including /init would be a placeholder (kernel panic).

**Fix.** Missing manifest sources must hard-fail the build (exit 1, no placeholder). If boot is kept: vendor pinned static BusyBox per arch into recovery/bin/<arch>/ + UPSTREAM.sha256. If dropped: delete recovery/boot/ and fix README.txt's false COMPLETE claim in the same PR.

**Tests / gates to add:**
- tests/recovery_hardening/test_initramfs_manifest_sources.py (always-on; needs only sh/cpio/gzip): every manifest 'f' source exists and is non-empty per arch; deleting a source makes build_initramfs.sh exit non-zero with no output file; on success no zero-byte regular file in the cpio.
- recovery/Makefile 'initramfs-check' target wired into make -C recovery test (rc + zero-byte-entry scan).
- Strengthen meta/bootable.py _validate_inputs to reject archives containing zero-byte regular files, with unit test.

#### 3. [HIGH] Boot configs are internally inconsistent: both UEFI and BIOS default menu entries point at paths the builder never creates

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** Even if someone built the kernel tomorrow, the disc still would not boot. In recovery_boot mode, BootableISOBuilder installs the kernel at /boot/vmlinuz and initramfs at /boot/initramfs.cpio.gz, but copies the GRUB config from recovery/boot/efi/grub.cfg whose entries load '/boot/linux/vmlinuz' (wrong directory) — every UEFI menu entry 404s. The isolinux config it installs comes from meta/live/isolinux.cfg, which loads 'initrd=/boot/initramfs' (wrong filename; actual is initramfs.cpio.gz) — the BIOS path fails too. The FreeBSD menu entries reference /boot/freebsd/loader.bin and loader.efi, artifacts that exist nowhere in the repo (recovery/boot/freebsd/ contains only kernel_config.txt and loader.conf). recovery/boot/isolinux/isolinux.cfg (the config that does match the kernel path) is dead — never referenced by the builder — and itself requires menu.c32, which the builder never copies. Nothing can catch this because no test ever boots anything.

**Evidence.** src/lcsas/meta/bootable.py:156-159 (copies to boot/vmlinuz and boot/initramfs.cpio.gz); bootable.py:172-176 (grub.cfg taken from recovery/boot/efi/grub.cfg); recovery/boot/efi/grub.cfg:8 ('linux /boot/linux/vmlinuz ...'); bootable.py:208-212 (isolinux cfg always taken from meta/live/isolinux.cfg even in recovery mode); src/lcsas/meta/live/isolinux.cfg ('APPEND initrd=/boot/initramfs' — no .cpio.gz); recovery/boot/isolinux/isolinux.cfg:9 ('UI menu.c32') vs bootable.py:222-235 (copies only isolinux.bin + ldlinux.c32); ls recovery/boot/freebsd/ (no loader.bin/loader.efi/kernel).

**Verifier.** Verified every citation. bootable.py:156-159 stages boot/vmlinuz + boot/initramfs.cpio.gz; recovery/boot/efi/grub.cfg:8 loads /boot/linux/vmlinuz (mismatch — all 3 Linux UEFI entries fail; FreeBSD entry chainloads loader.efi which is never staged, recovery/boot/freebsd/ has only .txt/.conf). _install_isolinux (bootable.py:208) always copies meta/live/isolinux.cfg whose 'initrd=/boot/initramfs' misses .cpio.gz in recovery mode. recovery/boot/isolinux/isolinux.cfg is referenced nowhere in src/Makefiles and needs menu.c32, never copied. tests/unit/test_bootable.py only asserts grub.cfg existence/entries, never path consistency against the staged tree. No existing gate.

**Fix.** If the boot path is dropped (recommended), delete both config sets. If kept, unify on ONE config source of truth, make BootableISOBuilder fail validation when a menu entry references a path absent from the staged tree, and delete the FreeBSD entries until artifacts exist.

**Tests / gates to add:**
- tests/unit/test_boot_config_paths.py — stage a tree via BootableISOBuilder._install_boot_files()/_install_isolinux() (recovery mode, with stub kernel/initramfs files at the documented names) and assert every KERNEL/APPEND initrd=/linux/initrd path referenced in the installed grub.cfg and isolinux.cfg exists in the staged tree. Pure-unit, always-on.
- tests/e2e/test_boot_qemu.py (opt-in, see finding 1) is the only gate that would prove the menu entries actually load — required before the boot path may be re-documented.

#### 4. [HIGH] Boot stack is structurally incompatible with 2035-2050 hardware: no Secure Boot story, x86_64-only UEFI, dead legacy-BIOS path, no ARM

**CONFIRMED** (verifier confidence: high) — tracked: Adjacent concerns only: UX_CONCERNS.txt ID 007 (optical drives rare, DEFERRED) and CROSS_PLATFORM_META_RFC.md §7 (non-x86_64 boot is a non-goal). Secure Boot and the BIOS-extinction problem are tracked nowhere.

**Gap.** The design targets hardware that is disappearing. (1) Secure Boot: the GRUB EFI binary is unsigned and there is no shim — zero mentions of Secure Boot anywhere in the repo. Consumer machines ship Secure Boot default-on today; by 2035-2050 a non-technical heir cannot be expected to find and disable it in firmware settings. (2) UEFI is x86_64-only: the builder searches for/builds only BOOTX64.EFI with grub-mkimage -O x86_64-efi; no BOOTAA64.EFI despite BOOT.txt claiming aarch64 (and riscv64 — for which not even recovery/bin/riscv64 exists). Apple Silicon Macs and Snapdragon laptops cannot boot this medium at all. (3) The isolinux path targets legacy BIOS/CSM, which Intel removed from client platforms in 2020 — dead weight on future machines. (4) Machines without optical drives need USB boot (see separate finding) or a USB BD reader. A kernel frozen in 2026 also lacks drivers for 2040 storage/USB controllers, so the boot path inherently decays, while the host-OS tier-1 path improves as host OSes update.

**Evidence.** grep -rni 'secure.boot|secureboot|shim' over recovery/, docs/, src/ returns no Secure Boot hits; src/lcsas/meta/bootable.py:249-254 (searches only BOOTX64.EFI) and :301 ('-O x86_64-efi'); recovery/docs/BOOT.txt:6-7 (claims x86_64, aarch64, riscv64); ls recovery/bin/ (no riscv64 dir); docs/CROSS_PLATFORM_META_RFC.md:470-471 (non-goal: 'Booting from the meta-volume on non-x86_64'); recovery/docs/UX_CONCERNS.txt:142-153 (ID 007, optical-drive rarity, DEFERRED).

**Existing partial coverage.** Partially tracked in docs only: UX_CONCERNS.txt ID 007, CROSS_PLATFORM_META_RFC.md §7 — no test/gate.

**Verifier.** Verified: grep for secure.boot/secureboot/shim over recovery/, docs/, src/ hits only unrelated LD_PRELOAD/python shims. bootable.py:249-254 searches only BOOTX64.EFI; _build_grub_efi uses '-O x86_64-efi'. BOOT.txt:7 claims x86_64/aarch64/riscv64 but recovery/bin/ has no riscv64 dir. RFC §7 declares non-x86_64 boot a non-goal; UX_CONCERNS ID 007 (optical rarity) DEFERRED. No Secure Boot/BIOS-extinction tracking anywhere. Architectural finding, accurately evidenced; high fits the heir-journey bar since BOOT.txt presents boot as a supported recovery path.

**Fix.** This is the decisive argument for DROP/REPLACE: a self-maintained boot medium fights hardware evolution for 25+ years, while signed mainstream live-Linux images solve Secure Boot, ARM, and driver coverage for free. Replace BOOT.txt with a 'boot any current live Linux, then run restore.sh from the meta disc' procedure, and add one line to START_HERE.txt for the no-OS case.

**Tests / gates to add:**
- tests/recovery_hardening/test_live_usb_procedure_docs.py — assert the rewritten RECOVER.txt/BOOT.txt no-OS procedure exists, names a concrete live-image source and the exact mount + `sh /mnt/recovery/scripts/restore.sh` command (same doc-pinning style as existing test_disc_swap_docs.py).
- tests/e2e/cdemu-style drill: boot a current Ubuntu live ISO in QEMU+OVMF (Secure Boot enabled with stock MS keys), attach the meta ISO as a second drive, run restore.sh from it, assert tier-1 restore completes — proves the replacement path end-to-end (opt-in, LCSAS_LIVE_USB_SMOKE=1).

#### 5. [HIGH] Designed boot flow restores into a 256 MB RAM tmpfs — capped, and silently lost on power-off

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** lcsas-init mounts /tmp as tmpfs with size=256m, then execs restore.sh with target '/tmp/restored'. So even in the fully-built fantasy version of the boot path, the flagship 'Run lcsas-restore directly' flow (a) fails with ENOSPC for any archive over 256 MB — this system is designed for hundreds of discs — and (b) writes restored files to RAM: an heir who sees 'restore complete' and powers off loses everything restored, with no warning anywhere in init.c or the boot docs. The initramfs environment has no step to select/mount a persistent destination (the Alpine wizard does target selection, but it belongs to the other, also-unbuilt stack); the busybox path would require the heir to manually fdisk/mkfs/mount from a shell.

**Evidence.** recovery/src/lcsas-init/init.c:111 ('try_mount("tmpfs", "/tmp", "tmpfs", 0, "mode=1777,size=256m")'); init.c:136-143 (execs restore.sh with args '/mnt/recovery /tmp/restored latest'); recovery/scripts/restore.sh:351 (default TARGET '/tmp/restored') with no tmpfs/volatility warning (grep for tmpfs/RAM warnings in restore.sh finds only the meta-disc relocate logic at :64-74); src/lcsas/meta/live/restore_wizard.py:545-590 (target-disk selection exists only in the Alpine stack).

**Verifier.** Verified init.c:111 mounts /tmp tmpfs size=256m; init.c:136-143 execs restore.sh with target /tmp/restored; restore.sh:351 defaults TARGET to /tmp/restored with no tmpfs/RAM-volatility warning (only the meta-disc relocate-to-RAM logic mentions RAM). Target-disk selection exists only in the Alpine wizard (restore_wizard.py:545-590), not the C89 path. Not critical: source data stays on discs, so loss is of the restored copy plus heir confusion, retryable. High is correct. No existing coverage.

**Fix.** If any boot path survives: lcsas-init must not hand off until a persistent target is mounted — detect that the target's filesystem is tmpfs and refuse/prompt; restore.sh should warn loudly when TARGET_DIR resolves to tmpfs regardless of entry path (also protects host-OS users who accept the /tmp/restored default on tmpfs-/tmp distros). If the boot path is dropped, the restore.sh tmpfs warning is still worth adding.

**Tests / gates to add:**
- tests/recovery_hardening/test_restore_sh_tmpfs_target_warns.py — run restore.sh with TARGET_DIR on a tmpfs mount (or stub findmnt/stat -f to report tmpfs) and assert a prominent 'target is RAM-backed / will not survive reboot' warning plus interactive confirmation; always-on.
- recovery/tests C unit for lcsas-init target policy (if boot kept): assert init refuses handoff when the restore target is tmpfs without an explicit kernel-cmdline override (e.g. lcsas.target=/dev/...).

#### 6. [MEDIUM] lcsas-init only probes optical device nodes, so the isohybrid USB-boot path (the only option on drive-less machines) dead-ends at a shell

**CONFIRMED** (verifier confidence: high) — **untracked** (not in any known-issues ledger)

**Gap.** The builder applies isohybrid --uefi specifically so the image can be written to and booted from a USB stick — the realistic medium for 2035+ machines without optical drives. But lcsas-init's medium search tries only /dev/sr0-3, /dev/cdrom, /dev/dvd. Booted from USB, the medium is /dev/sdX, which is never probed: init logs 'WARNING: no optical disc found; dropping to shell' and the non-technical heir lands at a bare busybox prompt (or at the FATAL loop given the missing-busybox finding). The Alpine stack's init does scan /dev/sd[a-z] (live/init), but with a different sentinel (rootfs.squashfs), underscoring the two-stacks divergence.

**Evidence.** src/lcsas/meta/bootable.py:483-511 (_make_hybrid, 'bootable from USB too', '--uefi'); recovery/src/lcsas-init/init.c:57-60 (candidates: '/dev/sr0'..'/dev/sr3', '/dev/cdrom', '/dev/dvd' only); init.c:115-121 (no-disc -> drop to shell); src/lcsas/meta/live/init:40-41 (Alpine init scans '/dev/sd[a-z] /dev/sd[a-z][0-9]').

**Verifier.** Verified init.c:57-60 candidates are exactly /dev/sr0-3, /dev/cdrom, /dev/dvd; init.c:115-121 drops to busybox shell on miss. bootable.py _make_hybrid applies isohybrid (--uefi when efiboot.img present) explicitly for USB boot. Alpine init (meta/live/init:40-41) does scan /dev/sd[a-z], confirming the two-stack divergence. recovery/tests/test_disc_locator.c covers lcsas-restore's locator, not init's device scan — no coverage for this. Medium calibrated correctly.

**Fix.** If the C89 boot path is kept, extend try_discs() to scan /dev/sd[a-z](+partitions), /dev/vd*, /dev/nvme*n*p*, and identify the medium by sentinel file (recovery/scripts/restore.sh) rather than device class. If dropped, this disappears with the rest of recovery/boot/.

**Tests / gates to add:**
- recovery/tests/test_init_medium_scan.c — unit-test the device-candidate/sentinel logic against a fake /dev tree (same style as existing test_disc_locator.c), asserting USB/virtio/nvme nodes are probed and selection keys off the restore.sh sentinel.
- QEMU smoke variant in tests/e2e/test_boot_qemu.py: attach the hybrid image as usb-storage instead of -cdrom and assert the same serial-console handoff marker.

#### 7. [MEDIUM] Two parallel half-built boot stacks (Alpine live + C89 recovery), with the Alpine one network-dependent, x86_64-only, unpinned, and its wizard untested

**CONFIRMED** (verifier confidence: high) — tracked: Partially: CROSS_PLATFORM_META_RFC.md §7 flags the Alpine arch story as out of scope. The duplication, the unpinned network-fetched boot artifacts, and the untested wizard are untracked.

**Gap.** The repo maintains two divergent boot implementations: (a) the Alpine stack (src/lcsas/meta/live/: build_rootfs.sh, init, grub/isolinux cfgs, 33 KB restore_wizard.py) and (b) the C89 recovery stack (recovery/boot/ + lcsas-init). The Alpine path fetches packages from Alpine 3.21 repos over the network at build time (apk add ... linux-lts), hardcodes ARCH=x86_64, and none of its outputs (vmlinuz/initramfs/rootfs.squashfs) are pinned in UPSTREAM.sha256 or MANIFEST.sha256 — violating the project's own vendoring/pinning doctrine for runtime dependencies. The restore_wizard.py (the heir-facing TUI for the live environment, doing lsblk/mount/dialog work) has only import/attribute smoke tests. Maintaining both stacks doubles staleness surface while neither works; the docs contradict each other on which is real (BOOT.txt describes the C89 stack as 'the' boot path; RFC §7 calls Alpine 'a separate effort').

**Evidence.** src/lcsas/meta/live/build_rootfs.sh:30-47 (ALPINE_VERSION=3.21, ARCH=x86_64, network apk update/add); recovery/UPSTREAM.sha256 header lists only rustic/ and python/ categories — no kernel/alpine/busybox entries; grep 'alpine|vmlinuz|busybox' over both .sha256 manifests matches only kernel_config .txt docs; tests/unit/test_bootable.py:361-373 (wizard tests are hasattr/import checks); src/lcsas/meta/builder.py:1736-1767 (_install_live_boot wires only the Alpine path; recovery_boot_dir reachable only via BootableISOBuilder directly); docs/CROSS_PLATFORM_META_RFC.md:470-471.

**Existing partial coverage.** Partial doc tracking only: CROSS_PLATFORM_META_RFC.md §7 (Alpine arch out of scope).

**Verifier.** Verified build_rootfs.sh: ALPINE_VERSION=3.21, ARCH=x86_64, unpinned network apk update/add. UPSTREAM.sha256 header lists only rustic/ and python/ categories; zero alpine/vmlinuz/busybox matches in either manifest — violates the project's own pinning doctrine for runtime artifacts. Wizard tests (test_bootable.py:355+) are import/init/dialog-missing smoke only. builder.py _install_live_boot wires only Alpine; recovery_boot_dir reachable only via BootableISOBuilder directly. BOOT.txt vs RFC §7 contradiction confirmed. Referenced test_meta_bundling_completeness.py exists for the proposed extension.

**Fix.** Pick zero or one stack. Recommended: delete src/lcsas/meta/live/ entirely (wizard included) along with the MetaVolumeBuilder bootable/alpine_dir parameters, and quarantine recovery/boot/ as experimental — the heir story should not depend on either. If one is kept, its artifacts must be pinned in UPSTREAM.sha256 and built reproducibly like every other shipped binary.

**Tests / gates to add:**
- tests/recovery_hardening/test_no_unpinned_boot_artifacts.py — assert that any file installed into the meta-volume staging tree which is an executable/kernel/initramfs appears in MANIFEST.sha256 or UPSTREAM.sha256 (extends existing test_meta_bundling_completeness.py).
- Repo gate: ruff/grep-based check (Makefile lint step) that build scripts under src/lcsas/meta/ perform no network fetch (no apk/curl/wget) unless the output is manifest-pinned.

#### 8. [MEDIUM] Zero boot testing at any level — the entire dimension is validated only by unit tests against fake byte-string artifacts

**CONFIRMED** (verifier confidence: high) — tracked: Partially: READINESS_CHECKLIST.txt:23-30 (manual boot test, unchecked) and README.txt:85-87 (deferral note). No automated gate is tracked or proposed anywhere.

**Gap.** Nothing ever boots in any test tier. tests/unit/test_bootable.py fabricates vmlinuz/initramfs/rootfs as 1-2 KB null-byte files and mocks xorriso; tests/integration/test_recovery_orchestration.py only checks constructor validation errors; no workflow in .github/workflows mentions QEMU or boot; the only QEMU usage in the suite (test_tier1_aarch64_qemu.py etc.) is qemu-user for binary execution, not system boot. The misleadingly-named test_tier1_meta_disc_live.py tests disc-locator sentinel behavior, not booting. The only boot validation in the entire project is an unchecked manual checklist line. Consequently every defect in findings 2, 3, 5 and 6 shipped invisibly, and any future 'fix' to the boot path is equally unverifiable.

**Evidence.** tests/unit/test_bootable.py:17-33 (fixtures write b'\x00' fake artifacts); tests/integration/test_recovery_orchestration.py:240-275 (constructor/validation-only); grep -rn 'qemu' .github/workflows/*.yml returns nothing; tests/recovery_hardening/test_tier1_meta_disc_live.py:1-29 (docstring: disc-locator exclusion, not boot); recovery/docs/READINESS_CHECKLIST.txt:23-30 ('[ ] META DISC BOOT TEST', manual, unchecked); recovery/docs/README.txt:85-87 (runtime validation 'deferred').

**Existing partial coverage.** Partial: recovery/docs/READINESS_CHECKLIST.txt manual 'META DISC BOOT TEST' (unchecked, manual-only).

**Verifier.** Verified: test_bootable.py fixtures write 1-2 KB null-byte vmlinuz/initramfs/squashfs; test_recovery_orchestration.py:240-275 is constructor-validation only; grep qemu over .github/workflows returns nothing; the only qemu in the suite is qemu-user binary-exec (test_tier1_aarch64/armv7_qemu.py), no qemu-system anywhere; test_tier1_meta_disc_live.py is disc-locator sentinel behavior; READINESS_CHECKLIST.txt boot test is manual and unchecked; recovery/boot/linux/ contains only kernel_config .txt files (no kernel ever built), so nothing bootable even exists to test. Manual checklist item is partial coverage at best.

**Fix.** Tie this to the KEEP/DROP decision as a forcing function: if the boot path is not given an automated QEMU boot gate within one phase, delete it. The gate is cheap once artifacts exist (OVMF + qemu-system-x86_64 are in Ubuntu CI runners); if the team will not fund the gate, that is itself the evidence that DROP is correct.

**Tests / gates to add:**
- Makefile target `boot-smoke`: build the bootable ISO (real initramfs via build_initramfs.sh, pinned kernel), boot with `qemu-system-x86_64 -M q35 -bios OVMF.fd -cdrom out.iso -serial stdio -display none`, assert '[lcsas-init] starting' and the restore.sh handoff line within 120 s; wire as a weekly scheduled job in .github/workflows/test.yml (not per-PR, to bound CI cost).
- Second matrix leg booting the same image via legacy SeaBIOS and via USB attachment, asserting identical serial markers — covers isolinux config and the USB medium-scan fix.
- Until that gate exists: tests/recovery_hardening/test_boot_path_quarantined.py asserting no heir-facing doc (START_HERE, RECOVER.txt, README_RESTORE) instructs booting the meta disc.


---

# Appendix C — Areas flagged for a follow-up audit round (completeness critic)

The critic spot-verified each of these in the repo; they are real, uncovered by the 78 findings above, and were deliberately not expanded into a third audit round to bound cost.

### burn-operator-protocol

The multi-volume session burn loop (/home/mikmorg/git/lcsas/src/lcsas/burn/orchestrator.py:681-702) burns each ISO to the same device back-to-back with no operator interaction at all — the file contains zero input()/prompt calls, so there is no 'insert next blank disc' pause, no blank-media pre-check, no post-burn readback of the disc's volume label to confirm the right disc is in the drive, and no instruction telling the operator what to write on the disc just burned. With hundreds of hand-labeled discs this is the primary path by which a mis-labeled or swapped disc enters the archive undetected; existing confirmed findings cover verification *content* semantics, not the physical disc-handling protocol.

### catalog-concurrency

Concurrency was designed half-way and never audited: locked_connection (/home/mikmorg/git/lcsas/src/lcsas/db/connection.py:57-89) takes a blocking fcntl LOCK_EX with no timeout and no user feedback, and cmd_burn_session holds it for the entire multi-hour burn (src/lcsas/cli/main.py:1087), so any concurrent lcsas command silently hangs. Worse, cmd_staging_clean (src/lcsas/cli/main.py:968) detects 'orphaned' staging dirs on an UNLOCKED connection and deletes them after an interactive pause — a TOCTOU race that can destroy an in-flight stage's tree, dovetailing with the confirmed 'packs permanently claimed at staging-commit' critical.

### disc-confidentiality-threat-model

No audited dimension asked what an adversary holding any single disc gets. The holographic catalog burned onto every disc stores plaintext hostname, backup paths, tags, and description per snapshot (src/lcsas/db/models.py:53-62 via rustic/parser.py:78-79), and HolographicInjector also copies each repo's rustic keys/ directory onto every disc (src/lcsas/staging/metadata.py:99-120) — so every offsite/escrowed disc reveals the full backup topology of every repo and permits unlimited offline brute-force of every repo password, forever, with no rotation story once discs are immutable.
