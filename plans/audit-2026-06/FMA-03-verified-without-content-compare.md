# FMA-03: Post-burn "verification" never compares disc content to the ISO hash

**Priority:** P0 · **Severity:** high · **Dimension:** failure-modes · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: partial — docs/PHASE_12_20_PLAN.md Phase 14 [S1] (remote mark-verified workflow only) + recovery/docs/PHYSICAL_DISC_VALIDATION.txt (manual annual drill)
**Suggested GH issue title:** Make disc verify compare device bytes to recorded ISO SHA-256

## Problem

`VERIFIED` — the terminal good state of the volume lifecycle, the status every redundancy
and pick-list query trusts — is granted on a readability smoke test. `verify_disc()` runs
`xorriso -indev <dev> -check_media` and returns `returncode == 0`. It never compares the
burned bytes to the staged ISO's SHA-256 (which *is* computed and stored in
`session_volumes.iso_sha256` at stage time) and never checks that the disc in the drive is
the volume being verified. A truncated or mis-burned disc that is structurally readable
passes. So does the wrong disc entirely.

The standalone path is worse for fool-proofing: `lcsas verify <LABEL> --disc` runs the same
readability-only check on *whatever disc happens to be in the drive* and then promotes that
volume `BURNED → VERIFIED` with a `VERIFY_PASS` event. An operator verifying a 50-disc set
can mark every volume VERIFIED while never inserting a single correct disc. The byte-level
compare exists only as a manual hardware drill in `PHYSICAL_DISC_VALIDATION.txt`. For the
heir-proof goal this means the catalog's strongest durability claim is unbacked: the owner
sees green for years; the heir discovers the truncation decades later when tier-1's blob
auth rejects the data and re-burning is impossible.

## Evidence

Re-verified 2026-06-10:

- `src/lcsas/iso/xorriso.py:307-325` — `verify_disc()` = `[xorriso, -indev, device,
  -check_media]`, `return result.returncode == 0`. No hash, no identity.
- `src/lcsas/burn/orchestrator.py:289-293` (single-volume `burn()`) and `:704-721`
  (`burn_session()`) — `verify_ok = self._xorriso.verify_disc(device)` gates VERIFIED;
  event detail says "Post-burn read-back" but no content compare.
- `src/lcsas/burn/orchestrator.py:623-626` — `iso_hash = sha256_file(manifest.iso_path)`
  computed and stored via `add_session_volume(..., iso_sha256=iso_hash)` (`:629-637`) —
  then never used for any disc verification.
- `src/lcsas/cli/main.py:1713-1720` — `--disc` path: `runner.verify_disc(device=args.device)`
  on whatever is inserted; `:1766-1769` — `if passed and vol.status == "BURNED":
  update_status(..., "VERIFIED")`.
- `recovery/docs/PHYSICAL_DISC_VALIDATION.txt` — `sha256sum -c` compare is manual-drill-only.
- The ISO label is written as the disc Volume ID at mastering (`xorriso.py:126-128`,
  `-V volume_label`) — i.e. disc identity *is* machine-readable from the PVD; it's just
  never checked.

## Fix design

Two new primitives in `src/lcsas/iso/xorriso.py` (subprocess-free where possible so the
fake-runner Protocol stays testable):

```python
def read_disc_volume_id(self, device: str) -> str:
    """Read the ISO9660 PVD Volume ID from the disc (xorriso -indev dev -pvd_info,
    parse 'Volume id'). Returns '' on failure."""

def sha256_device(device: str, length: int, progress_cb=None) -> str:
    """SHA-256 of the first `length` bytes read from the block device.
    Plain file read in 4 MiB chunks — no external tool needed."""
```

`length` must be the **augmented** ISO size (RS03 parity is appended to the image before
burning), so record it at stage time: in `_stage_single_volume`, after ECC augmentation,
capture `iso_size_bytes = iso_path.stat().st_size` and store it alongside the hash.

**Schema v7 (additive):** `ALTER TABLE session_volumes ADD COLUMN iso_size_bytes INTEGER`
(and the same column on `volume_copies` so the size survives receipt import / rebuild).
Additive `ALTER TABLE ... ADD COLUMN` migration — crash-safe, no table recreation; bump
`CURRENT_SCHEMA_VERSION` to 7. **Compat:** old catalogs (all burned discs, forever) lack
the column — `_row_to_copy`-style tolerant readers (`volume_copies.py:13-25` pattern) return
`None`, and verification falls back to readability-only **with an explicit downgrade
warning**: `"No recorded ISO size for <label> — content verification not possible; running
readability check only (catalog predates v7)."` Never silently pretend the strong check ran.

Call sites:

1. **`burn_session()` (`orchestrator.py:704-721`) and `burn()` (`:289-293`)** — after
   `burn_iso`, verification becomes: (a) `read_disc_volume_id(device) == vol.label`, else
   fail with `"Disc in <device> identifies as '<got>' — expected '<label>'. Wrong disc?"`;
   (b) `sha256_device(device, sv.iso_size_bytes) == sv.iso_sha256`, else fail with a
   mismatch message naming both hashes. Keep `-check_media` as a cheap pre-pass (surfaces
   drive-level read errors with better messages). `verify_passed` requires all three.
2. **`cmd_verify --disc` (`main.py:1713-1720`)** — same sequence; identity check uses the
   *requested* label, and the hash comes from `get_iso_sha256_for_label()`
   (`volume_copies.py:112-153`, already falls back to `session_volumes`). Promotion at
   `:1766-1769` only on full pass. On identity mismatch: record **nothing** (it's the wrong
   disc, not a bad volume); print the wrong-disc message and exit 1.

Design choice: device-read + Python SHA-256 over `xorriso -compare_r` because it needs no
mounted filesystem, works identically for the ECC-augmented tail (which is outside the
ISO9660 filesystem and invisible to `-compare_r`), and reuses the already-trusted
`sha256_file` chunking pattern.

Edge cases: drives that over-read past the track return padding — reading exactly
`iso_size_bytes` sidesteps it; short device (truncated burn) → read returns < length →
fail with `"Disc is shorter than the recorded image (got X of Y bytes) — truncated burn"`.

## Tests & gates

- `tests/unit/test_burn_orchestrator.py::test_burn_marks_verified_only_after_content_compare`
  — fake xorriso runner + monkeypatched `sha256_device` recording its (device, length) args;
  hash mismatch ⇒ status stays `BURNED`, `VERIFY_FAIL` event emitted, no promotion. Always-on.
- `tests/unit/test_burn_orchestrator.py::test_burn_verify_rejects_wrong_volume_id` —
  `read_disc_volume_id` returns a different label ⇒ verify fails, message names both labels.
- `tests/unit/test_cli_handlers.py::test_verify_disc_rejects_wrong_volume` — `verify <A>
  --disc` with disc claiming label B ⇒ exit 1, no `VERIFY_PASS` event, no promotion. Always-on.
- `tests/unit/test_cli_handlers.py::test_verify_disc_downgrade_warning_on_old_catalog` —
  pre-v7 catalog row (no `iso_size_bytes`) ⇒ readability-only path + the downgrade warning
  string. Always-on.
- `tests/unit/test_db_schema.py::test_v7_migration_adds_iso_size_bytes` — v6 fixture
  migrates; column present; old rows NULL. Always-on.
- `tests/integration/test_disc_content_verify.py` — opt-in `LCSAS_DISC_VERIFY=1`, CDEmu
  (this VM has CDEmu, no physical writer — see ECC test environment notes): load ISO B in
  the virtual drive, run `lcsas verify <A> --disc` ⇒ FAIL/no promotion; load ISO A ⇒ PASS
  and promotion; truncate the backing image ⇒ "truncated burn" failure. Wire into the
  existing cdemu e2e harness; candidate for the weekly scheduled CI job (GATE plans).

## Acceptance criteria

- [ ] A burn whose read-back hash mismatches leaves the volume `BURNED`, emits
  `VERIFY_FAIL`, and (per FMA-04) records no ACTIVE copy.
- [ ] `lcsas verify <LABEL> --disc` with the wrong disc inserted exits 1, names both
  labels, and changes nothing in the catalog.
- [ ] `lcsas verify <LABEL> --disc` against a pre-v7 catalog prints the explicit
  downgrade warning instead of silently smoke-testing.
- [ ] CDEmu integration test passes under `LCSAS_DISC_VERIFY=1`.
- [ ] Schema v7 migration is additive-only and green on a copy of the live catalog.

## Dependencies & related plans

- **BURN — "Post-burn 'verification' never compares the physical disc to the recorded ISO
  SHA-256"**: same finding from the burn-pipeline dimension — implement once; this plan is
  the authoritative design, the BURN plan covers its pipeline integration context.
- **FMA-04 — failed verify still records ACTIVE copy**: same `burn_session` region; land
  together (one PR is fine).
- **FMA-02 — migration wiring**: schema v7 only reaches existing catalogs if `migrate()`
  actually runs; FMA-02 first.
- **FMA-05 — disc-rot re-verification**: builds its batch mode directly on this plan's
  identity+hash disc check.

## Effort

3 days: 1.5 impl (primitives + two call sites + v7 migration), 1 unit tests, 0.5 CDEmu
integration. Needs the CDEmu loop locally (already available on this VM); do not run pytest
concurrently with other suites here.

---
**Implemented:** 2026-06-11. Hash-compare half landed earlier as BURN-04/BURN-05 (schema v7,
`read_device_sha256`); this commit adds the remainder: PVD Volume-ID identity gate on both burn
paths and `verify --disc` (wrong disc → exit 1, nothing recorded), schema v8
(`volume_copies.iso_size_bytes`) so device verification survives receipt import / catalog
rebuild, and the size in burn receipts. CDEmu drill wired into the existing
`tests/e2e/test_burn_verify_disc.py` under `LCSAS_BURN_E2E=1` (not a new
`LCSAS_DISC_VERIFY` file as drafted).
