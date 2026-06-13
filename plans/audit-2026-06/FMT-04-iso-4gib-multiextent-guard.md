# FMT-04: Guard against ≥4 GiB files in ISOs (Windows CDFS multi-extent trap)

**Priority:** P2 · **Severity:** medium · **Dimension:** format-durability · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Reject ≥4 GiB files at ISO mastering; document Windows multi-extent limit

## Problem

xorriso is invoked with `-iso-level 3`, commented "Support files > 4 GB". ISO 9660 Level 3
stores a >4 GiB file as multiple extents — which Windows' built-in CDFS driver (the exact path
behind `restore.bat` / `Mount-DiskImage` per the cross-platform RFC) does not reassemble. The
heir sees a truncated or duplicated file with **no error**, on the statistically most likely
heir platform. Nothing in the pipeline prevents a ≥4 GiB single file from being mastered:
bin-packing only rejects items larger than whole-media capacity, so a large `catalog.db` at
scale, an operator-tuned rustic pack size, or a future bundled meta-volume artifact slides
through silently and the failure surfaces decades later.

Keeping ISO 9660 over UDF is the right long-horizon call (UDF revision/driver fragmentation is
worse); the gap is the missing guard and the missing doc caveat, not the format choice.
SURVIVABILITY.md rates ISO 9660 "Low risk" with no multi-extent caveat.

## Evidence

Re-checked against current code:

- `src/lcsas/iso/xorriso.py:125` and `:225` — `"-iso-level", "3"` (data ISO in `create_iso`,
  meta ISO in `create_bootable_iso`), the former commented `# Support files > 4 GB`.
- `src/lcsas/binpack/algorithm.py:43-58` — only items exceeding whole-media usable capacity
  are flagged (and only via `_logger.error`, returned in `remaining`).
- `docs/SURVIVABILITY.md:259-262` — §4.1 ISO 9660 "✅ Low risk", no caveat.
- `docs/CROSS_PLATFORM_META_RFC.md:112-114` — "On Windows, the existing `restore.bat` mounts
  via PowerShell `Mount-DiskImage`", i.e. native CDFS.
- `recovery/docs/RECOVER_WINDOWS.txt` — `grep -iE '4 ?gi?b|multi.?extent'` → zero matches.

## Fix design

1. **Hard guard at the mastering choke point** — `src/lcsas/iso/xorriso.py`,
   `SubprocessXorrisoRunner`: new private helper called at the top of both `create_iso` (:98)
   and `create_bootable_iso` (:196):

   ```python
   _ISO_MAX_FILE_BYTES = 0xFFFF_F800  # 4 GiB - 2 KiB: max single-extent ISO9660 file section

   def _assert_no_multiextent_files(self, source_dir: Path) -> None:
       offenders = [(p, p.stat().st_size) for p in source_dir.rglob("*")
                    if p.is_file() and p.stat().st_size > _ISO_MAX_FILE_BYTES]
       if offenders:
           raise OversizeFileError(...)  # names each file + size
   ```

   Error wording: *"file 'data/ab/cd/<hash>' is 4,512,345,678 bytes (> 4 GiB - 2 KiB). ISO
   9660 would store it multi-extent, which Windows' native mount silently truncates. Refusing
   to master. Split the file or reduce rustic pack size."*

   Why here and not `staging/builder.py` (the audit's suggestion): the xorriso wrapper is the
   single choke point for *all* images — data volumes AND meta-volumes (whose bundled
   artifacts never pass through StagingBuilder) AND files injected after pack staging
   (catalog.db, holographic metadata). One guard covers everything.

2. **Belt-and-braces early warning** — `src/lcsas/binpack/algorithm.py:43-58`: extend the
   existing oversize check to also flag items `> _ISO_MAX_FILE_BYTES` (import the constant) so
   the operator hears about it at plan time, not after staging completes. Keep the mastering
   guard as the hard stop.

3. **Docs** — `recovery/docs/RECOVER_WINDOWS.txt` (mount section): add the caveat — files over
   4 GiB cannot be read through Windows' built-in mount; copy the ISO to disk and extract with
   7-Zip (handles multi-extent) instead. `docs/SURVIVABILITY.md` §4.1: add the multi-extent
   note and reference the guard.

No catalog/schema impact. Already-burned discs are unaffected (no known >4 GiB files have
been burned; the guard prevents future ones; the doc caveat covers any that exist).

## Tests & gates

1. `tests/unit/test_iso_oversize_guard.py` — always-on (`make test-unit`, CI):
   - create a sparse file of `_ISO_MAX_FILE_BYTES + 1` bytes (`os.truncate`; no disk usage,
     pytest tmp under `/var/tmp/pytest-lcsas`), assert `create_iso` raises `OversizeFileError`
     naming the file *before* any subprocess is spawned (fake binary path proves no exec);
   - a file of exactly `_ISO_MAX_FILE_BYTES` passes the guard;
   - same pair for `create_bootable_iso`.
2. `tests/unit/test_binpack_oversize_warning.py` — FFD flags a > 4 GiB item even when it fits
   the media.
3. `tests/recovery_hardening/test_windows_doc_multiextent_caveat.py` — static doc pin (pattern
   of `test_env_var_docs.py`): RECOVER_WINDOWS.txt documents the >4 GiB/CDFS limitation and
   the 7-Zip workaround. Runs under `make test-recovery-hardening` (CI once the GATE plan
   wires that suite in).

## Acceptance criteria

- [ ] Mastering a tree containing a 4 GiB+1 sparse file fails with the named-file error; no
      xorriso process is spawned.
- [ ] Meta-volume build path (`create_bootable_iso`) enforces the same guard.
- [ ] Bin-pack run logs a warning for a 5 GiB item on BDXL100 media.
- [ ] RECOVER_WINDOWS.txt contains the multi-extent caveat; doc-pin test green.

## Dependencies & related plans

- None blocking; standalone. Doc-pin test joins the heir-docs contract gate family (UX
  "docs-vs-reality contract gate").
- GATE: "recovery-hardening suite never runs in CI" — needed for test #3 to gate merges.
- INFRA-01: a future Windows e2e could mount a fixture ISO with a multi-extent file and prove
  the truncation empirically — nice-to-have, not required.

## Effort

**1 focused day** (0.5 impl, 0.5 tests + docs). No special environment.

---
**Implemented:** 2026-06-13. As planned: `OversizeFileError` + `_ISO_MAX_FILE_BYTES` guard at the xorriso choke point (both `create_iso` and `create_bootable_iso`, before any subprocess spawn); bin-packer plan-time multi-extent warning; RECOVER_WINDOWS.txt + SURVIVABILITY.md §4.1 caveats; three test modules (unit oversize guard, binpack warning, recovery-hardening doc-pin) all green.
