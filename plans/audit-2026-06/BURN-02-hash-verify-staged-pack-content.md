# BURN-02: Hash-verify every staged pack against its catalog SHA-256

**Priority:** P0 · **Severity:** critical · **Dimension:** burn-pipeline · **Audit status:** confirmed (high confidence) · **Ledger:** partial — operational only (READINESS_CHECKLIST monthly test-restore, latest snapshot only); the code gap is untracked
**Suggested GH issue title:** Verify pack content hashes before mastering; kill the 500 MB hash skip

## Problem

Nothing in the burn pipeline ever reads a pack and checks that its bytes hash to
the SHA-256 in its filename. The scanner registers name + size; staging hardlinks
the mirror file (a fresh hardlink is never read at all, and a pre-existing staged
file is hashed only if ≤ 500 MB); `catalog validate` compares file *names* to the
catalog; post-burn "verify" is a readability check. The pack's filename is trusted
as its content hash at every step.

So a pack that bit-rots on the NAS mirror after rustic wrote it (silent disk
corruption, truncated rsync, filesystem bug) is burned **identically corrupt to
every copy at every location** — the same mirror file feeds every ISO. RS03 ECC
then faithfully protects the corrupt bytes, and the volume reaches VERIFIED. The
corruption is discovered only when tier-1's Poly1305/SHA-256 gate *rejects* the
blob at restore time — decades later, when the mirror is gone and no good copy
exists anywhere. For the heir this is unrecoverable data loss with a green catalog.
`docs/architecture.md:356` even claims "Post-burn: read-back the entire disc and
verify SHA-256 of every pack" — implemented nowhere.

The one moment the data is guaranteed to be read anyway (xorriso is about to read
it all to master the ISO) is exactly where a full hash pass is nearly free relative
to the burn. That is where the gate belongs.

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/packs/scanner.py:13,16-34` — `_PACK_NAME_RE` + `st_size` only;
  no hashing anywhere in the module.
- `src/lcsas/staging/builder.py:113-145` — hash check exists ONLY when
  `dst.exists()` (a leftover from a prior run) AND `dst_size <= _hash_threshold`
  (`500_000_000`); the >500 MB branch at 137-145: *"Large file; skip expensive
  hash check, just verify size"*.
- `src/lcsas/staging/builder.py:155-176` — fresh hardlink path: `hardlink_or_copy`
  then only a zero-size check (`if dst_size == 0`). Content never read.
- `src/lcsas/db/verify.py:39-61` — `_collect_disc_packs`: *"A valid SHA-256 is 64
  hex characters"* — name-shape match only; `validate_disc` compares name sets.
- `src/lcsas/iso/xorriso.py:307-325` — `verify_disc` = `-check_media`
  `returncode == 0` (see BURN-04).
- `docs/architecture.md:356` — the unimplemented read-back claim.

## Fix design

### 1. Mandatory content verification in `StagingBuilder.stage_packs`
(`src/lcsas/staging/builder.py`)

- **Fresh-link path** (after `hardlink_or_copy`, replacing the zero-size-only
  check at 165-174): `sha256_file(dst)` — the dst is a hardlink to the mirror
  inode, so this reads and verifies the actual mirror bytes. On mismatch:
  log ERROR with expected/actual, `dst.unlink()`, append to a new `corrupt` list.
- **Pre-existing-dst path**: delete the `_hash_threshold` branch entirely
  (113-145 collapses to: size check, then unconditional `sha256_file(dst)`;
  mismatch → unlink and fall through to re-stage from source; if the re-staged
  copy *also* mismatches it lands in `corrupt`).
- **New exception** in builder.py:
  ```python
  class CorruptPacksError(Exception):
      def __init__(self, corrupt: list[tuple[str, str]]) -> None:  # (expected, actual)
  ```
  Raised after the loop alongside the existing `MissingPacksError` (raise
  `CorruptPacksError` first if both — corruption is the scarier message).
  Wording: `"<N> pack(s) on the mirror are CORRUPT (content does not match
  filename hash): <first 5 short hashes>. The mirror copy is the only hot copy —
  do NOT delete it. Run 'rustic check --read-data' on the repo and re-replicate
  the mirror, then re-run 'lcsas stage'. Nothing was written to the catalog."`
- Like BURN-01, this all happens before `create_volume` in
  `_stage_single_volume`, so the failure path needs no DB compensation.
- Progress logging every 100 packs already exists (builder.py:177-178); extend the
  message with hashed-bytes so long runs are visibly alive.

**Design choice — no QUARANTINED schema flag.** The audit suggested marking packs
QUARANTINED in the catalog. Rejected for now: a corrupt mirror pack requires mirror
repair (rustic check / re-replication) before any burn can proceed anyway, and a
new `packs` column would be burned into every future on-disc catalog, adding a
forward-compat burden on tier-1/tier-3 readers for no restore-side benefit.
Fail-loud-and-halt is the durable behavior. (If operational experience shows a
need to burn *around* one bad pack, add the column then, with a v6→v7
`ALTER TABLE ... ADD COLUMN` and a restore-side-ignores note.)

### 2. Content mode for disc validation (`src/lcsas/db/verify.py`)

- `validate_disc(disc_path, *, content: bool = False)`: when `content=True`,
  hash every file collected by `_collect_disc_packs` and report mismatches in a
  new `CatalogValidationResult.corrupt_on_disc: list[str]` field (`ok` includes
  `not corrupt_on_disc`).
- CLI: `lcsas catalog validate <path> --content` (parser in `cli/main.py`,
  `catalog` subcommand block near line 3116). Exit non-zero naming corrupt packs.
  This is the heir/operator-facing spot-check for a mounted disc — cheap to add
  since the hashing helper already exists (`utils/hashing.sha256_file`).

### 3. Fix the false doc claim

`docs/architecture.md:356` — rewrite the Verification section to describe reality
after this plan + BURN-04: *staging-time content hash of every pack; post-burn
device read-back SHA-256 of the full ISO (BURN-04); optional
`catalog validate --content` for mounted discs.* The docs-vs-reality contract gate
(UX/GATE plans) should pick this file up.

No schema change; no migration. Performance note: one extra full read of all
staged data per stage (~14 min for 25 GB at 30 MB/s NAS read) — document in the
`stage` --help epilog.

## Tests & gates

Always-on unit (`make test-unit`, `.github/workflows/test.yml`):

- `tests/unit/test_staging.py::test_stage_rejects_pack_with_corrupt_content` —
  pack file whose bytes don't hash to its filename; `stage_packs` raises
  `CorruptPacksError`; dst removed from staging.
- `tests/unit/test_staging.py::test_preexisting_large_staged_pack_is_hash_verified`
  — pre-existing dst with correct size but wrong content (size set > the old
  threshold is irrelevant once the branch is deleted, but keep a >500 MB-shaped
  case via a small monkeypatched threshold-free path to pin the skip's removal:
  assert `sha256_file` is called for a pre-existing dst of any size, using a
  mock on `lcsas.staging.builder.sha256_file`).
- `tests/unit/test_staging.py::test_fresh_hardlink_is_hash_verified` — corrupt
  source file, fresh stage; raises.
- `tests/unit/test_burn_orchestrator.py::test_stage_corrupt_pack_commits_nothing`
  — orchestrator-level: corrupt one seeded pack's mirror file; `orch.stage()`
  raises; no volume row; `get_unarchived_packs()` unchanged.
- `tests/unit/test_db_verify.py::test_validate_disc_content_mode_flags_corruption`
  — build a fake disc tree with one corrupted pack; `validate_disc(p,
  content=True)` reports it; name-only mode still passes (pins the distinction).

Integration (opt-in, requires xorriso — `make test-integration`):

- `tests/integration/test_catalog_validate_content.py` — master a real ISO with
  one corrupted pack, extract/mount, assert `lcsas catalog validate --content`
  exits non-zero naming the pack.

Doc gate: covered by the docs-vs-reality contract test (UX/GATE plan); until that
lands, add a one-line assert in a unit test that architecture.md no longer
contains the literal "read-back the entire disc and verify SHA-256 of every pack"
sentence unless the BURN-04 device path exists.

## Acceptance criteria

- [ ] Corrupting any byte of any pack on the mirror makes `lcsas stage` exit
      non-zero with the `CorruptPacksError` message; no catalog rows written.
- [ ] `grep -n "_hash_threshold\|500_000_000" src/lcsas/staging/builder.py`
      returns nothing.
- [ ] Every staged pack is read+hashed exactly once on the happy path (verify via
      the mocked-`sha256_file` call-count test).
- [ ] `lcsas catalog validate <mounted-disc> --content` detects an
      injected corruption and exits non-zero.
- [ ] `docs/architecture.md` Verification section matches implemented behavior.

## Dependencies & related plans

- **BURN-01** (fail loud on missing mirror) — same function; land first.
- **BURN-04** (device read-back SHA-256) — completes the chain: mirror→staging
  (this plan) and ISO→disc (BURN-04). Together they restore the architecture.md
  claim honestly.
- **GATE/UX docs-vs-reality contract gate** — picks up the architecture.md fix.
- **FMA** disc-rot re-verification plan — `validate_disc --content` is the
  building block it can reuse.

## Effort

2.5 days: 1.0 impl (builder + verify.py + CLI + doc), 1.0 unit tests,
0.5 integration test (needs xorriso locally; already in CI's integration job).

---
**Implemented:** 2026-06-11. As planned, plus collateral test-seed fixes: unit suites that staged packs with fabricated hashes (test_session_pipeline, test_burn_orchestrator, test_staging, test_parser_staging_labels, test_filesystem_failures) now seed content that really hashes to its name; the empty-staged-file case surfaces as CorruptPacksError instead of MissingPacksError.
