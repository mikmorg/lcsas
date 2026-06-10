# UX-05: production META discs get the weakest START_HERE.txt

**Priority:** P1 · **Severity:** high · **Dimension:** ux-journey · **Audit status:** confirmed (high confidence) · **Ledger:** adjacent only: recovery/docs/UX_CONCERNS.txt ID 002/008 (OPEN); the config-variant regression itself is untracked
**Suggested GH issue title:** META-specific START_HERE: merge config fields with per-OS dispatch

## Problem

`MetaVolumeBuilder._write_start_here` produces two very different documents. **With
a config — the production case — it reuses the DATA-disc generator**
(`HolographicInjector.write_start_here`), whose text: claims "You need a computer
running Linux, or someone who can help you use one" (contradicting the shipped
Windows/macOS support); contains no runnable command at all (only "a script called
restore.sh that automates everything"); never mentions `restore.bat`; and directs
the reader to "the file RESTORE_INSTRUCTIONS.txt on this disc" — a file the meta
builder never writes (it is only written to data discs by the burn orchestrator).

Only the **no-config fallback** variant carries the useful per-OS dispatch
(Windows: double-click restore.bat / macOS-Linux: `sh restore.sh ~/restored` /
boot). And even that variant's command omits the disc path — a Terminal opens in
`$HOME`, so the literal `sh restore.sh ~/restored` fails with "sh: restore.sh: No
such file". Net effect: the first document the heir reads is the worst one on
exactly the archives that matter (real, config-built ones), and it actively
misdirects non-Linux heirs away from routes that exist.

## Evidence

(Re-checked 2026-06-10.)

- `src/lcsas/meta/builder.py:2421-2427` — config path:
  `injector.write_start_here(self._config)` + key_info/config_summary/disc_care;
  no per-OS text, no `write_restore_instructions` call anywhere in `build()`
  (steps at builder.py:1711-1724).
- `src/lcsas/staging/metadata.py:404` — "3. You need a computer running Linux, or
  someone who can help you use one."
- `src/lcsas/staging/metadata.py:424-426` — "the file RESTORE_INSTRUCTIONS.txt on
  this disc has step-by-step manual recovery instructions";
  `write_restore_instructions` is called only by `src/lcsas/burn/orchestrator.py:425`
  (data discs) — never by the meta builder.
- `src/lcsas/meta/builder.py:2447-2448` — no-config variant: "Open a Terminal, then
  run: `sh restore.sh ~/restored`" — no mount path.
- `tests/unit/test_meta_builder.py:347-361` — asserts only that START_HERE.txt
  exists and has OS sections in the no-config fixture; the config-variant content is
  untested.

## Fix design

One generator, always used for meta volumes. In `src/lcsas/meta/builder.py`:

1. New method `_render_meta_start_here(config: LCSASConfig | None) -> str` that
   merges:
   - the per-OS dispatch block (from the current no-config heredoc, lines 2433-2456),
     with **mount-path-aware commands**:
     ```
     >>> Windows 10 or 11 <<<
          Open this disc in File Explorer and double-click  restore.bat
     >>> macOS <<<
          Open Terminal, then run:   sh /Volumes/<DISC_LABEL>/restore.sh ~/restored
     >>> Linux <<<
          sh /media/$USER/<DISC_LABEL>/restore.sh ~/restored
          (or: sudo mount /dev/sr0 /mnt && sh /mnt/restore.sh ~/restored)
     ```
     Use the real volume label when known (builder has it via volume info; else the
     literal `LCSAS_META`).
   - the config survivability fields when `config is not None`: owner, description,
     key_storage_hints, technical_contact, and the split-key block (reuse
     `HolographicInjector`'s `_share_recovery_lines` content — post-UX-02 text).
   - the no-OS block from UX-03 (conditional on `self._bootable`).
2. `_write_start_here` calls the new renderer in **both** branches; keep
   `injector.write_key_info/write_config_summary/write_disc_care` calls for the
   config case. `HolographicInjector.write_start_here` remains the data-disc
   generator, untouched in role.
3. Kill the dangling reference: the META text must point to files that exist on the
   meta volume — replace "RESTORE_INSTRUCTIONS.txt on this disc" with
   `recovery/docs/RECOVER.txt` (bundled by `_bundle_docs`). Do not write
   RESTORE_INSTRUCTIONS.txt to the meta volume (its content is data-disc-oriented;
   one canonical manual per disc type avoids drift).
4. Data-disc wording fix (same root text): `metadata.py:404` → "3. You need a
   computer running Windows, macOS, or Linux (or someone who can help you use one)."
   — the data-disc payload (standalone_restorer.py) is cross-platform.

No catalog/schema impact; already-burned discs keep the weak text — corrected text
ships with the next meta burn.

## Tests & gates

In `tests/unit/test_meta_builder.py` (always-on, `make test-unit` / CI):

- `test_start_here_with_config_has_os_dispatch` — build with a fixture
  `LCSASConfig`; assert START_HERE.txt mentions `restore.bat`, contains a runnable
  `restore.sh` command with a path separator before `restore.sh`, contains the owner
  string, and does NOT contain "computer running Linux,".
- `test_start_here_references_only_files_present` — extract referenced filenames
  (regex for `[A-Z_]+\.txt`, `restore\.(sh|bat)`, `keyshare_combine.py`, doc paths)
  from the built START_HERE.txt and assert each exists in the output tree. This is
  the generic guard that catches the RESTORE_INSTRUCTIONS.txt class.
- `test_start_here_split_block_present_when_key_split` — config with
  `key_split=True` → split block present and (per UX-02) free of phantom flags.
- The UX-02 contract test automatically scans this generator's output once both land.

## Acceptance criteria

- [ ] A config-built meta tree's START_HERE.txt has per-OS dispatch, owner/key-hint
      fields, a path-qualified restore.sh command, and no Linux-only claim.
- [ ] Every file referenced by START_HERE.txt exists on the built meta volume.
- [ ] No-config builds produce the same structure minus the config fields.
- [ ] `pytest tests/unit/test_meta_builder.py -v` passes; the three new tests fail on
      the pre-fix tree.

## Dependencies & related plans

- **UX-03** — supplies the no-OS block text; merge into this renderer (UX-03's
  builder edit can land first on the no-config heredoc, then move here).
- **UX-02** — split-block text and the contract gate that scans this output.
- **UX-01** — keep the Windows dispatch line in sync with restore.bat behavior.
- BURN "holographic catalog predates its own burn" (medium) — unrelated content but
  same injector file; avoid merge conflicts by sequencing.

## Effort

1.5 days: 1 generator + text, 0.5 tests. No special environment.
