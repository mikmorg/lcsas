# BURN-04: Post-burn verify must compare the physical disc to the ISO SHA-256

**Priority:** P0 · **Severity:** high · **Dimension:** burn-pipeline · **Audit status:** confirmed (high confidence) · **Ledger:** untracked (docs/architecture.md:356 documents the intent; no ledger tracks that it is unimplemented)
**Suggested GH issue title:** Grant VERIFIED only after device read-back matches recorded ISO SHA-256

## Problem

VERIFIED is the catalog's strongest durability claim — `check_deprecation_safe`
trusts it when deciding whether the last replica of a pack may be deprecated. But
post-burn "verification" is `xorriso -indev <dev> -check_media` with
`returncode == 0`: a readability smoke test. It never compares the disc to the
mastered image, never even checks the volume label — **any readable disc sitting
in the drive passes**. A drive that silently mis-burned, a wrong/leftover disc in
the tray, or a truncated burn all yield a VERIFIED volume whose data may be absent.
The heir discovers this decades later at restore time.

The bitter part: the pipeline already computes and stores the post-ECC ISO SHA-256
(`session_volumes.iso_sha256`) at stage time — and then never uses it against the
device. Worse, `add_volume_copy` accepts an `iso_sha256` parameter that
`burn_session` doesn't pass, so every copy row gets NULL there, and
`volume_copies.last_verified_at` is never written by any code.

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/iso/xorriso.py:307-325` — `verify_disc`: `["-indev", device,
  "-check_media"]`, `return result.returncode == 0`. No content/label compare.
- `src/lcsas/burn/orchestrator.py:623-637` — `iso_hash = sha256_file(...)` stored
  into `session_volumes` at stage time; `:704-727` — `verify_passed` set solely
  from `verify_disc(device)`; VERIFIED granted at 726.
- `orchestrator.py:742-748` — `add_volume_copy(self._conn, volume_id=...,
  location=..., commit=False)` — the `iso_sha256=` kwarg
  (`db/volume_copies.py:46`) is omitted → NULL on the copy row; the UPSERT
  (`volume_copies.py:61-66`) even **overwrites** a previously stored hash with
  NULL on re-burn (the FMA counterpart flags this).
- `src/lcsas/cli/main.py:1713-1718` — `lcsas verify --disc` calls the same
  `verify_disc`; the SHA-256 fallback compare (1733-1758) applies only to ISO
  **files**, never devices.
- `db/volumes.py:241-265` — deprecation safety trusts `BURNED`/`VERIFIED` status.
- Test gap: `tests/unit/test_session_pipeline.py:478,507` mock `verify_disc` to
  True/False; nothing exercises a content-mismatched disc.

## Fix design

### 1. Device read-back hashing

New module `src/lcsas/burn/device_verify.py`:

```python
def read_device_sha256(device: str, length_bytes: int,
                       chunk: int = 4 * 1024 * 1024) -> str:
    """Read exactly length_bytes from a block device and return hex SHA-256."""
```

Pure stdlib (`open(device, 'rb')` + `hashlib`), no subprocess. Reading exactly the
ISO's byte length works because RS03-augmented images are sector-aligned and
xorriso burns the image byte-exact from sector 0; drive padding beyond the image
length is excluded by construction. Errors (short read, EIO) raise `OSError` with
the device and offset — a short read is itself a verify failure.

The ISO length must be known at verify time (the ISO file may already be gone on
re-verification). **Schema v6→v7 migration:** `ALTER TABLE session_volumes ADD
COLUMN iso_size_bytes INTEGER` (nullable, crash-safe single ALTER, mirroring the
v3→v4 pattern at `db/schema.py:244-252`); bump `CURRENT_SCHEMA_VERSION` to 7 and
populate it in `stage()` next to `iso_sha256`.
*Compat:* burned discs carry v≤6 catalogs forever; no restore-side code reads
`session_volumes.iso_size_bytes` (tier-1/tier-3 read packs + volumes only), so old
catalogs are unaffected. Hot-DB rows predating the migration have NULL → fall back
to `iso_path.stat().st_size` when the ISO still exists, else skip the device-hash
step with a loud `WARNING: cannot device-verify <label>: no recorded ISO size
(pre-upgrade session)` and do NOT grant VERIFIED on `-check_media` alone.

### 2. `burn_session` (orchestrator.py:704-748)

Inject the reader for testability: `BurnOrchestrator.__init__` gains
`device_reader: Callable[[str, int], str] = read_device_sha256` (matches the
existing protocol-injection pattern for xorriso/dvdisaster). Verify becomes:

1. Fast pre-pass: existing `verify_disc(device)` (`-check_media`).
2. If it passes and `sv.iso_sha256` is set: `device_hash =
   self._device_reader(device, iso_size)`; `verify_passed = (device_hash ==
   sv.iso_sha256)`. Mismatch → `VERIFY_FAIL` event with detail
   `"device hash mismatch: expected <8>.., got <8>.."`.
3. Record the copy with the evidence:
   `add_volume_copy(..., iso_sha256=sv.iso_sha256, commit=False)` and set
   `last_verified_at` (extend `add_volume_copy` to accept it, or a follow-up
   UPDATE in the same transaction). This fixes the NULL `iso_sha256` and the
   never-written `last_verified_at` in one stroke, and gives FMA's disc-rot
   re-verification plan its data.
4. `skip_burn=True` skips the device read exactly as it skips the burn.

Timeout/runtime: reading 25 GB at BD 6x (~27 MB/s) ≈ 16 min — log a "reading back
<N> GB, this takes a while" INFO line and chunk progress every 1 GiB.

### 3. `lcsas verify --disc` (cli/main.py:1713-1718)

After `-check_media`, look up the volume's `session_volumes.iso_sha256` +
`iso_size_bytes` and run the same device compare; print PASS/FAIL per step.
Update `volume_copies.last_verified_at` on PASS when `--location` is given.

## Tests & gates

Always-on unit (`make test-unit`, CI test.yml):

- `tests/unit/test_session_pipeline.py::test_verify_compares_device_hash` —
  fake `device_reader` returning a wrong hash while `verify_disc`→True; assert
  volume stays BURNED, `VERIFY_FAIL` event detail contains "hash mismatch", and
  (with BURN-05) no ACTIVE copy.
- `::test_verify_device_hash_match_grants_verified` — matching fake reader →
  VERIFIED + copy row with `iso_sha256` populated and `last_verified_at` set.
- `::test_burn_session_writes_copy_iso_sha256` — assert
  `volume_copies.iso_sha256 == session_volumes.iso_sha256` after burn (kills the
  NULL and the UPSERT-blanking regression).
- `::test_legacy_session_without_iso_size_warns_no_verified` — NULL
  `iso_size_bytes`, ISO file absent → warning path, volume stays BURNED.
- `tests/unit/test_db_schema.py::test_migration_v6_to_v7` — v6 fixture DB
  migrates; column exists; old rows NULL.
- `tests/unit/test_burn_device_verify.py` — `read_device_sha256` against a temp
  file: exact-length read, short-read raises.

E2e (CDEmu is available on this VM per project memory; opt-in like
blind-restore): add a burn-side leg under `tests/e2e/` — burn an ISO to a cdemu
virtual device, flip one byte in the backing file, assert `lcsas verify --disc`
fails and an untouched backing file passes. Wire as `make verify-burn-e2e`;
candidate for the weekly scheduled CI job proposed in the GATE plans.

## Acceptance criteria

- [ ] A disc whose bytes differ from the recorded ISO hash can never reach
      VERIFIED (unit-proven via injected reader; e2e-proven via cdemu byte-flip).
- [ ] `-check_media` alone can no longer grant VERIFIED for sessions that carry a
      recorded hash.
- [ ] `volume_copies.iso_sha256` and `last_verified_at` are populated on every
      verified burn; re-burns no longer blank a stored hash.
- [ ] v6 catalogs migrate cleanly to v7; restore from a pre-v7 on-disc catalog is
      unaffected (existing restore tests stay green).
- [ ] `lcsas verify --disc` reports both check_media and hash-compare results.

## Dependencies & related plans

- **BURN-05** (failed verify must not record an ACTIVE copy) — same function;
  implement together in one PR if possible; BURN-05's "skip copy on fail" decides
  what step 3 does on mismatch.
- **FMA** "post-burn verification" + "no disc-rot re-verification /
  last_verified_at never written" — this plan provides the mechanism both need.
- **GATE** weekly-CI plan — hosts the cdemu e2e leg.
- **BURN-02** — staging-side content hashing; together they close mirror→disc.

## Effort

2.5 days: 1.0 impl (reader + orchestrator + CLI + migration), 1.0 unit tests,
0.5 cdemu e2e (needs the cdemu VM environment; no real burner required).

---
**Implemented:** 2026-06-11. As planned, plus required wiring: create_all() now applies pending migrations (nothing ever called migrate(), so v7 would never run); the legacy no-size/no-ISO warning path is unit-tested via the extracted _verify_burned_disc helper (unreachable through burn_session, which always has the ISO and falls back to st_size). BURN-05 ("no ACTIVE copy on failed verify") intentionally not included.
