# LCSAS Full-Project Review — July 2026

**Baseline:** master `f181f11` (post standard-tools tier, PRs #356–#360)
**Date:** 2026-07-01
**Method:** five independent review passes (burn pipeline, restore path + recovery
cascade, cryptography + key handling, test/CI gating, docs + human factors), each
reading actual code — followed by manual verification of every high-severity claim
before inclusion. File:line references were checked against `f181f11`.

This complements `docs/READINESS_ASSESSMENT_2026-06.md` (adversarial readiness
review, concerns C1–C10); §6 below refreshes that assessment's status.

---

## 1. Executive summary

**Value: high and unusual.** LCSAS solves a real problem — multi-decade,
offline-first, heir-recoverable cold storage — with a design discipline rare even
in commercial software: layered dependency-elimination (each recovery tier removes
a class of failure), holographic self-description (every disc carries the full
catalog), and a verification harness that tests the *tests* (CI/Makefile parity
gates, green-by-skip detection, weekly drift canaries against unpinned upstream).

**Correctness: strong core, real edges.** The parts that must be right are right,
in both independent implementations: MAC-verify-before-decrypt with constant-time
comparison (Python and C89), post-decrypt content-hash checks, evidence-based
post-burn verification, compensating transactions in staging, crash-atomic schema
migrations. No path was found where corrupt or tampered data could be *silently*
restored.

**But this review found 6 high- and ~11 medium-severity issues** (§4), all
verified against code. The single most important: **tier-1 (the primary durable
restore binary) reads out of bounds and crashes on any legitimate backup carrying
2+ extended attributes** — routine for SELinux/ACL systems — because the xattr
walk confuses a JSON byte offset for a token index (`tree.c`). The rest cluster in
two places: **restore.sh hygiene** (password file survives `exec`; word-splitting
on spaced mount paths) and **verification blind spots** (every fresh meta-volume
fails its own `sha256sum -c` on `recovery/scripts/restore.sh`; the stock-restic
compat test — foundation of the standard-tools tier — never runs in CI; the
hardening skip-rot floor is stale at 200 vs 581 collected tests). Notably, the
unbounded-scrypt overflow (M1) was found independently by two separate review
passes.

**Bottom line:** the architecture and crypto are sound and the engineering
culture (honest ERRATUM notes, adversarial self-assessment, XFAIL ledgers) is a
genuine asset. The residual risk is not in the algorithms — it is in shell-level
plumbing, in gates that *look* armed but aren't, and in the still-untested human
layer (no real-person restore drill, no physical-media burn in any automated
gate). Prioritized recommendations in §7.

---

## 2. Value analysis

### What exists
- ~25,600 lines of Python (stdlib-only at runtime), ~11,000 lines of C89,
  ~66,000 lines of tests (2,341 test functions; 2.6:1 test-to-source ratio).
- 634 commits over ~5 months (2026-02 → 2026-06), ~360 PRs, ~351 issues closed,
  3 open (all low).
- 22 top-level CLI subcommands, schema v9 catalog, 6 cross-compiled recovery
  targets, 4-tier recovery cascade + zero-LCSAS-code standard-tools tier.

### Why it is valuable
1. **It occupies a gap no off-the-shelf tool fills.** Plain restic/rustic to
   cloud has account/subscription/provider risk; tape has drive-scarcity risk;
   raw optical dumps lack dedup, ECC, and cataloging at multi-hundred-disc
   scale. LCSAS composes audited tools (restic format, RS03 ECC, SLIP-0039)
   into an estate-grade system while keeping each layer independently
   recoverable.
2. **The recovery story is defense-in-depth by construction.** Tier-1 (C89 +
   vendored deps, kernel+libc only) → tier-2 (bundled rustic) → tier-2b (stock
   restic, bundled *and* installable) → tier-3 (bundled CPython). The
   standard-tools tier (#356–#360) is the standout: a proven, byte-identical
   restore path using zero LCSAS code — the single best hedge against
   "the custom code is the thing that fails."
3. **The verification harness is the moat.** Real binaries in default CI,
   weekly format canary against *unpinned* latest rustic, real-macOS and
   real-Windows scheduled jobs, qemu/wine execution of committed cross-arch
   binaries, a doc-contract lattice pinning every command quoted in burned heir
   docs, and meta-tests asserting the CI wiring itself. Most projects assert
   their code works; this one also asserts its *assertions* still run.
4. **Honesty culture.** ERRATUM notes for already-burned discs, UX_CONCERNS.txt,
   XFAIL ledgers, and a self-authored "NOT ready for a non-technical heir"
   assessment. This materially raises trust in everything else.

### Value risks (not code)
- **Bus factor = 1.** Design intent, drill cadence, and the meaning of the
  gates live substantially in one person (plus this repo's docs).
- **Value is realized only through operation.** Burn cadence, annual key
  drills, media refresh — the system's durability claims are conditional on a
  human calendar that is documented but not mechanized (§6, C1/C7).
- **Validation-against-reality gaps remain**: no real-human restore drill, no
  physical burner in any automated gate (dev VM is cdemu-virtual only).

---

## 3. Correctness — what was verified as strong

| Area | Verified strength |
|---|---|
| Crypto (both impls) | Poly1305 MAC over ciphertext verified **before** decryption, constant-time compare: `restic_fallback.py:218-236`, `repo.c:57-63` + `lcsas_ct_memcmp`. Post-decrypt SHA-256 content-hash vs blob ID in both. Corrupt data is rejected, never silently restored. |
| Key hygiene | Shares/passwords written `O_EXCL` 0600; split verified against the live repo, K-subset round-tripped, written cards re-read (`cli/main.py:5427-5506`); rustic invoked with `--password-file`, never argv/env; `key_escrow` stores only K/N + id. |
| SLIP-0039 | CSPRNG (`secrets`), official valid+invalid vectors with completeness check, RFC 8439 / NIST KATs; interop with Trezor reference pinned (`test_stock_restic_compat.py`). |
| Burn verification | Three independent post-burn checks (readability, PVD label, exact-length device read-back SHA-256 vs stage-time hash); failed verify records no copy row (`burn/orchestrator.py:1189-1301`). |
| Staging atomicity | Volume + M:M rows commit atomically; `except BaseException` compensating delete returns packs to the pool on any later failure incl. Ctrl-C (`orchestrator.py:476-598`, pinned by tests). Mirror bytes hash-verified via the hardlinked inode before replication (`staging/builder.py:119-287`). |
| Schema migrations | Per-step `BEGIN IMMEDIATE`, `foreign_keys=OFF` correctly set outside the transaction, wedged-migration detection, natural-key ID remapping in rebuild (`db/schema.py:364-603`). |
| Restore hardening | Hostile-content handling in tier-3 (basename reduction, symlink-escape skip, atomic partial-file writes); zero-byte-binary exec trap caught in restore.sh preflight; tier-1 fault-injection + qemu aarch64/armv7 + wine suites run committed binaries for real. |
| Tier-1 C injection safety | catalog.c uses `sqlite3_prepare_v2` + bound params only, DB opened `SQLITE_OPEN_READONLY`, disc data never string-concatenated (`catalog.c:121-160`); index `offset`/`length` range-checked before malloc/pread (`repo.c:477-484`); AES-CTR increments the full 16-byte counter (matches restic/Go). Six fuzz harnesses (json, tree, path-safety, b64, repo-strip, zstd). |

---

## 4. Findings — verified defects

Severity reflects impact on the project's *own* goals (durable, heir-recoverable
restore). All HIGH findings were manually re-verified against source; agent
attributions below each include a concrete failure scenario.

### HIGH

**H0. Tier-1 restore reads out of bounds and crashes on legitimate backups with
2+ extended attributes.**
`recovery/src/lcsas-restore/tree.c` `apply_node_xattrs`: `elem_t` is a JSON
**token index** (used as `toks[elem_t].parent`, `.type`), but at every loop
continuation it is reassigned a **byte offset** via `elem_t = toks[elem_t].end`
(`tree.c:390,394,401,433`; `json_q.h:35` documents `.end` as an exclusive byte
offset). The next iteration's unbounded `while (toks[elem_t].parent != ea_i)
elem_t++;` (`tree.c:381`) — the function is never passed `ntoks` — then walks
`toks[]` far past its end. With a single xattr the corrupting assignment happens
after the last needed iteration, so it is invisible (which is exactly why
`test_tier1_xattrs.py`, using one `user.foo`, passes). With **≥2** xattrs — the
norm for SELinux labels, POSIX ACLs, capabilities — the second iteration
dereferences out of bounds → heap OOB read → crash/abort of the tier-1 restore.
This is in the default Linux build (`LCSAS_HAS_XATTR`) and triggers on
**non-adversarial, real-world** data, making it the highest-impact finding here.
Untested. (Found by the dedicated C-review pass; verified against source for this
report.)

**H1. restore.sh leaves the plaintext password on disk after every successful
default restore — and echoes it while typed.**
`recovery/scripts/restore.sh:1218-1220` (prompt via `IFS= read -r pw`, no
`stty -echo` → password visible in terminal/scrollback), `:1212-1216` (written to
`/tmp/lcsas-pw.XXXXXX`, 0600). Tiers 1/2/2b `exec` the recovery binary on the
default path (`:1496`, `:1531`, `:1582-1586`), which replaces the shell process —
so the EXIT trap that deletes `PWFILE_TMP` (`:688`) never fires. Failure
scenario: heir restores successfully on a borrowed/shared machine; the archive
password persists in /tmp (and scrollback) indefinitely. Untested.

**H2. Every fresh meta-volume fails its own integrity check on
`recovery/scripts/restore.sh`.**
`src/lcsas/meta/builder.py`: `_regenerate_recovery_manifest` runs during
recovery-tree bundling and keeps non-`./bin/` rows verbatim (`:2330-2342`) —
including the source-tree hash of `scripts/restore.sh`. Later,
`_write_restore_script` (`:1760` in build order, body `:2414-2470`) replaces the
on-disc `recovery/scripts/restore.sh` with a 2-line redirect stub. Result: the
burned `MANIFEST.sha256` row can never match; an heir following the documented
`sha256sum -c` step sees a MISMATCH on the single most important file, eroding
the integrity story exactly when trust matters most. Untested (manifest verify
tests use synthetic trees; no built-disc manifest check in CI).

**H3. restore.sh word-splits on mount paths containing spaces.**
`recovery/scripts/restore.sh:824-864` (`for cand in $REPO_CANDIDATES`),
`:1485-1498` and `:1762-1764` (`$PACK_SEARCH_ARGS $CATALOG_ARG $META_DISC_ARG`
expanded unquoted). A disc auto-mounted at `/media/user/DISC LABEL 1` — the
*default* behavior of desktop Linux for a labeled disc — splits into garbage
arguments. Tests pin argument plumbing only with space-free paths. (POSIX sh
without arrays makes this awkward but solvable — e.g. `set --` argument lists.)

**H4. The standard-tools tier's foundation test never runs in CI.**
`tests/recovery_hardening/test_stock_restic_compat.py` skips unless stock
`restic` is on PATH or `LCSAS_RESTIC_BIN` is set; no workflow installs stock
restic (verified by grep over `.github/workflows/`). A rustic upstream change
that breaks stock-restic readability — the exact drift this test exists to
catch — would pass CI silently. The weekly `format-canary` covers unpinned
*rustic*, not the restic side.

**H5. The hardening skip-rot floor is stale: 200 vs 581 collected tests.**
`.github/workflows/test.yml:151` (`FLOOR: "200"`, comment says re-baseline
after first green run — never done). ~65% of the recovery-hardening suite could
silently rot to skip before the floor trips; this is also the mechanism that
would have surfaced H4.

### MEDIUM

**M1. Tier-1 C: scrypt N/r/p from untrusted key-file JSON are unbounded**
(found independently by two review passes).
`repo.c:133-139` reads N/R/P from disc JSON and runs scrypt *before* any
password/MAC gate; `scrypt.c:170-179` checks only N≥2/power-of-two and r,p≠0,
then computes `bs = 128UL*r` and `malloc(bs*p)`, `malloc(bs*N)`. These
`unsigned long` multiplies overflow — trivially on 32-bit armv7 (an approved
target; e.g. r=2²⁵ → bs≡0), and on 64-bit with e.g. N=2⁶² r=8 → `bs*N` wraps to
256, `malloc(256)` succeeds, then `smix` writes `V[step*bs+k]` out of bounds —
yielding a small alloc + massive OOB write. A tampered `keys/` file is precisely
the pre-authentication surface tier-1 must survive. The Python side is
DoS-bounded by `hashlib.scrypt` maxmem. Unfuzzed/untested.

**M2. The completeness gate does not enforce what the docs promise.**
`meta/required_contents.py:91-98` requires lcsas-restore / rustic-static /
lcsas-ecc / python only. Stock restic (tier-2b, `builder.py:~2162`),
`lcsas-keyshare` (`:2289-2296`, the primary combiner named by
ESTATE_PLANNING.md:117 and used by restore.bat), and per-repo `metadata/` keys
are staged skip-if-absent. A disc missing tier-2b and the Windows split-key
path passes `meta verify --require-complete` while
RESTORE_STANDARD_TOOLS.txt:21-24 promises both binaries are aboard.

**M3. Tier-2b tool-kind detection and terminal exec.**
`restore.sh:1563-1566` matches `*rustic*` against the full path — stock restic
installed at e.g. `/opt/rustic-tools/bin/restic` gets rustic's CLI form and
fails; `:1585` then `exec`s the unvetted PATH binary, ending the cascade so an
old (pre-v2-format) restic fails terminally without tier 3 ever being tried.

**M4. `session clean` can strand verify-failed packs.**
`burn/orchestrator.py:1431-1450`: the unburned-guard counts only
STAGING/BURNING, so a verify-failed volume (BURNED, zero copy rows) passes it —
`lcsas session clean` deletes its ISO while the packs remain "archived"
(BURNED ∈ durable statuses) though no verified disc exists; default staging
never re-stages them. Only the report-only no-active-copies query surfaces it.

**M5. Crash window downgrades burn verification.**
`orchestrator.py:1238-1246`: the pre-Phase-13 compat branch grants VERIFIED on
readability+PVD alone when `sv.iso_sha256` is empty — but a modern crash
between volume commit and `update_session_volume_iso` (`:899-912`) leaves
exactly that state (pinned at `test_session_pipeline.py:1301`). Burning that
session afterwards ships a disc without the read-back hash check.

**M6. ECC redundancy silently dilutes on full discs.**
The 15% RS03 parity budget is enforced only against pack data
(`config/media.py:50-52`, `orchestrator.py:562-568`); nothing bounds the
holographic metadata payload (catalog + rustic index grow with estate size),
and a ~24.9 GB tree on BD25 ships with <1% effective redundancy — surfaced
only as an INFO log (`ecc/dvdisaster.py:148-154`). No test asserts a minimum
effective redundancy on production media.

**M7. Physical burn path is fake-only in every always-on gate.**
`XorrisoWrapper.burn_iso` (`-as cdrecord`, `/dev/sr0`), device_verify, and
format_preflight hit real hardware only via opt-in `LCSAS_BURN_E2E` (cdemu,
local-only) or the manual physical-disc drill. A burn-argument regression
ships green through gate + CI. (Known limitation — dev VM has no writer — but
worth stating as standing risk.)

**M8. Upstream supply chain unenforced in CI.**
No workflow runs `fetch_upstream.sh`/meta-gate; `UPSTREAM.sha256` artifacts
(python-build-standalone, restic) are verified only when fetched locally.
Tier-3's supply chain could 404 or drift for months undetected. Related low:
staged binaries are never re-verified against UPSTREAM.sha256 at bundle time
(`builder.py:2350-2364`) — the manifest is regenerated *from* staged bytes, so
a corrupted cache is legitimized.

**M9. `restore_auto.sh` appears stale/broken.**
`recovery/scripts/restore_auto.sh:34-45` discovers repo names but never exports
`LCSAS_REPO`: multi-repo archives hit restore.sh's interactive prompt (EOF →
fail); single-repo loops restore the same repo N times. Referenced only by
test_verify_self.py.

**M10. Heir-doc drift (post-June-audit).**
(a) RECOVER.txt:58-96, :289 omit tier 2b entirely (added by #359) — an heir
sees "[tier 2b] using stock restic" with no backing in the primary manual;
(b) RECOVER.txt:243 tells the heir `<arch>` is `x86_64` etc., but discs use
rust triples (`x86_64-unknown-linux-musl`) — the manual's first command fails
as written; :39/:63/:99 use `bin/<arch>`//`/mnt/bin/...` where :198/:239
correctly use `/mnt/recovery/bin/<arch>/`;
(c) ESTATE_PLANNING.md:195 letter template says "a computer running Linux"
though Windows restore is first-class — the first artifact a non-technical
heir touches points them away from their most likely machine;
(d) README.md:459/~644 + CLAUDE.md link `docs/development/cross-platform-meta-rfc.md`
— actual file is `docs/CROSS_PLATFORM_META_RFC.md`;
(e) README.md:635 "still-pending targets" is stale (all 6 covered).

**M11. Tier-3 tolerant mode aborts on zstd-level corruption.**
`restic_fallback.py:1108,1226,1252` catch tuples omit `ZstdError`: one
MAC-valid-but-zstd-corrupt blob aborts the whole restore (clean exit 1),
contradicting the "one bad blob never aborts" contract at `:412`. The tolerant
tests inject only MAC-level corruption.

### LOW (abbreviated)

- `execute()` vs `burn_session` handle verify-failure with **opposite** catalog
  semantics (revert-to-STAGING vs stay-BURNED) — divergence untested
  (`orchestrator.py:373-383` vs `:1025-1032`).
- `SubprocessDVDisasterRunner.augment_iso` disk preflight budgets iso+1 MiB but
  RS03 grows the temp copy to padded medium size → possible mid-augment ENOSPC
  (fails loud, original preserved) (`ecc/dvdisaster.py:109-116`).
- `_try_keys` stops iterating on a bit-rotted key file (bad base64) before
  trying healthy keys (`restic_fallback.py:305-310`); `_count_files` pre-pass
  aborts on OSError before tolerant traversal starts (`:1307`).
- restore.bat: parens in target path (`C:\Program Files (x86)\...`) break block
  parsing; `"` in password breaks `if`; catalog pick takes last drive letter
  while claiming most-recent (`restore.bat:377-467`).
- MANIFEST.sha256 is forward-only (new authored file with no row is invisible
  to heirs' `sha256sum -c`) — declared/known.
- ECC repair proof latency: changes routed through burn/ISO layout (not
  `src/lcsas/ecc/`) wait up to 7 days for the Monday run.
- No shellcheck anywhere in gate or CI, for a project whose bare path is a
  1,800-line POSIX-sh script.
- Password/key-material zeroization absent in all tiers (documented,
  defensible for run-once recovery; Python can't wipe `bytes` regardless).
- Interactive prompt aside (H1), blind-restore limits: TEST_TINY media, 0% ECC
  redundancy fixtures, pristine virtual discs — it is a UX/correctness proof,
  not a durability proof.
- Local `make gate` on a toolless machine passes with large silent skip mass
  (only the hardening job has a floor; the 63 integration tests have none).
- Legacy staging dirs / stray ISOs invisible to `staging/cleanup.py` orphan
  detection (`cleanup.py:14,41-43`).
- Tier-1 C: dedicated bounds-audit of `tree.c`/`json_q.c` parsers remains
  worthwhile — the xattr walk (H0) was the concrete instance; a broader pass is
  merited (fuzz harnesses exist but did not catch H0's multi-element case).
- Tier-1 C: absolute symlink targets are accepted by design (`path.c:114-122`,
  issue #187) — relative targets are containment-checked, but a restore run as
  root could follow an absolute link (`/etc/*`) planted in a snapshot. Documented
  design choice, flagged for awareness.
- Tier-1 C: `b64.c:30-43` silently skips `=`/whitespace and drops trailing
  sub-byte bits rather than erroring; mitigated because key fields assert exact
  decoded lengths (`repo.c:167`).

---

## 5. Verification-story map (what actually gates what)

- **Every push/PR:** ruff + mypy(strict) + unit (1,905) + integration (63, real
  rustic/xorriso/dvdisaster, SHA-pinned) + e2e (9, green-by-skip grep) + C
  smoke + doc-contract collect guard + meta-bundling completeness; parallel
  hardening job (581 collected) with qemu + wine real-binary execution, junit
  skip floor (stale — H5), shell-coverage ≥89% on restore.sh.
- **Weekly (all auto-file issues on failure):** ECC repair proof vs real
  dvdisaster; format canary vs **unpinned latest** rustic; bin reproducibility
  parity; real-macOS tier-1; real-Windows e2e; live-USB smoke.
- **Opt-in/local only:** blind-restore (+variants), cdemu burn e2e, live-USB,
  ECC repair, meta-gate/fetch-upstream (H4/M8 live here).
- **Meta-tests:** Makefile↔CI parity (KNOWN_UNWIRED empty), skip-rot floor,
  manifest freshness (forward direction), doc-contract lattice over burned docs.

This is an exceptionally strong harness *where it is wired*; the findings above
are precisely the seams where wiring is missing or stale.

---

## 6. Readiness assessment refresh (C1–C10 of 2026-06)

| # | Concern | Status | Evidence |
|---|---|---|---|
| C1 | No real-human restore drill | **OPEN** | Blind e2e is still an AI proxy |
| C2 | Windows/macOS journeys | **PARTIAL** | Real windows-e2e + macos-tier1 scheduled CI exist; blind doc-following journey Linux-only |
| C3 | Physical media never burned in gate | **OPEN** | Manual drill doc only; no writer on dev VM (M7) |
| C4 | Longevity bets unproven | **OPEN** (by construction) | Framing docs only |
| C5 | Reproducible builds | **OPEN** | #320: Mach-O/PE non-determinism; bins committed + exempt |
| C6 | Custom-crypto blast radius | **PARTIAL → largely mitigated** | Standard-tools tier #356–#360 proven; crypto itself still unaudited externally (and see M1) |
| C7 | Key-escrow SPOF / drills | **PARTIAL** | Annual drill documented; nothing mechanizes/logs it |
| C8 | Self-graded audits | **OPEN** | This review is *also* self-graded (agent-driven); no independent human audit yet |
| C9 | Coverage as proxy | **PARTIAL** | Differential + interop legs added; breadth metric still not headline |
| C10 | CI fragility (shared state) | **PARTIAL** | #327 fixed; single-machine risk remains |

---

## 7. Recommendations (prioritized)

**Now (small, high leverage):**
0. **Fix H0 first.** Advance `elem_t` by token index (scan forward past the
   subtree), never assign `.end` into it; pass `ntoks` and bound the
   `while (...parent...) elem_t++` walk. Add a 2+-xattr fixture to
   `test_tier1_xattrs.py` (SELinux label + POSIX ACL) — this is a crash on
   *real* data in the primary restore path.
1. Fix H2 (reorder manifest regen after stub write, or re-hash the stub) and
   add a built-disc `sha256sum -c` assertion to the meta build tests.
2. Fix H1: `stty -echo` around the prompt; replace tier-1/2/2b `exec` with
   run-then-propagate (the fall-through branches already do this) so the
   cleanup trap fires; add a pwfile-gone-after-restore assertion.
3. Wire H4: install stock restic in the hardening CI job (one apt/download
   line) so the compat gate actually gates.
4. Re-baseline H5's floor to ~(passed−5) per its own comment.
5. Add restic + lcsas-keyshare + repo metadata keys to
   `required_contents.py` (M2) — the docs already promise them.

**Soon (real work, worth an issue each):**
6. H3: space-safe argument handling in restore.sh (`set --` lists), plus a
   spaced-mount-path hardening test; add shellcheck to lint.
7. M1: clamp scrypt N/r/p (restic's own defaults never exceed N=2¹⁷ r=8 p≥1;
   a generous ceiling like N≤2²², r≤32, p≤16 kills the overflow) + fuzz.
8. M4/M5: close the session-clean and empty-iso_sha256 holes; one test each.
9. M6: enforce a minimum effective post-augment redundancy (fail, not INFO).
10. M10: heir-doc corrections (tier-2b in RECOVER.txt, `<arch>` triple names,
    letter's Linux-only line, RFC links) — cheap, directly heir-facing.
11. M3/M9/M11: tier-2b kind detection by basename; fix or retire
    restore_auto.sh; add ZstdError to tolerant catch tuples.

**Standing (unchanged from June, still the real frontier):**
12. C1: one real human (ideally the actual heir) performs a cold restore from
    the letter alone; observe, don't help.
13. C3: physical burn + restore on real BD-R hardware; the drill doc exists.
14. C8: an external human audit of `keyshare/` + `restic_fallback.py` + tier-1
    crypto (~2k LOC core) would retire the last crypto-trust caveat.

---

## 8. Final verdict

LCSAS is **well past prime-time quality for its author-operated use case** —
the burn path protects data with layered verified redundancy, and the restore
path has more independent, genuinely-executed proof legs than most commercial
backup products. The distance to "hand the discs to a non-technical heir with
confidence" is now dominated by: **one real correctness bug in the primary
restore binary (H0 — crashes on multi-xattr backups)**, 4 shell/manifest bugs
(H1–H3 + M10, all cheap), 2 unarmed gates (H4, H5, both one-liners), and the two
standing reality-validation gaps (real human, real disc). None of these threaten
the *bytes* already burned — the data on disc is intact and independently
recoverable via the standard-tools tier even if tier-1 crashes — but H0 means the
*default* restore of a typical Linux backup would abort, and the rest threaten the
experience of the one restore that will ever matter. H0 is the one that would
actually bite a real restore today; fix it first.
