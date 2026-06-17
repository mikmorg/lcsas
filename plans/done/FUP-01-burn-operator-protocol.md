# FUP-01: Operator burn protocol — disc swaps, blank checks, identity readback, labeling

> **STATUS: RESOLVED** — landed in `ba1f735` (burn+cli+iso: operator burn protocol — swap prompts, blank-check, eject, per-disc receipts [FUP-01]); guarded by `tests/unit/test_burn_orchestrator.py`.

**Priority:** P1 · **Severity:** high · **Dimension:** follow-up: burn-operator-protocol · **Audit status:** flagged by completeness critic (citations re-verified against code 2026-06-10) · **Ledger:** untracked (named only in DEEP_AUDIT Appendix C / P2 roadmap)
**Suggested GH issue title:** Add operator checkpoints to multi-disc burns; verify disc identity after burn

## Problem

`burn_session` burns every ISO in a session to the same optical device back-to-back with
**zero operator interaction**: orchestrator.py contains no `input()` call at all. There is no
"insert the next blank disc" pause, no eject after a burn, no blank-media pre-check before a
burn, no post-burn readback of the disc's volume label, and nothing telling the operator what
to physically write on the disc just burned. The labels the operator hand-writes on hundreds
of discs are the *only* link between the catalog and a physical object decades later.

Two concrete consequences. First, **a real multi-disc session cannot complete as shipped**:
after volume 1 burns and verifies, the loop immediately starts burning volume 2 onto the
still-loaded, just-burned disc 1 (no eject is requested, no pause exists). That burn fails,
the `except` path rolls back and marks the session PARTIAL — and a retry then hits the
`is_reburn` path for volume 1 whose ISO was already deleted, raising FileNotFoundError
(the BURN "retain ISO for multi-location burns" finding). Second, **a mislabeled or swapped
disc is undetectable forever**: `verify_disc` is a readability check that never reads the
volume id, receipts are written only after the whole session into the WARM staging dir, and
no restore-side tooling consults `volume_info.json` — so two swapped marker labels surface
decades later as an heir stuck in an "Insert the right disc and press ENTER to retry" loop
with no tool to identify the stranger disc in hand.

Existing confirmed BURN/FMA findings fix verification *content* semantics (SHA-256 readback,
failed-verify bookkeeping). This plan covers the physical disc-handling protocol around them,
plus a scoped follow-up audit charter for the parts not yet examined.

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/burn/orchestrator.py:681-811` — `for sv in session_vols:` burns each volume
  consecutively: `self._xorriso.burn_iso(iso_path, device)` (:702), `verify_disc(device)`
  (:707), copy recorded (:743-748), ISO deleted (:792-795) — then the loop immediately
  continues to the next volume. `grep -n "input(" orchestrator.py` → no matches.
- `src/lcsas/iso/xorriso.py:272-305` — `burn_iso` runs `xorriso -as cdrecord -v dev=… -dao
  fs=64m <iso>`; no `-eject`, no media inquiry beforehand. `_translate_burn_error` (:18-50)
  only explains failures after the fact ("No disc found…"), it is not a pre-check.
- `src/lcsas/iso/xorriso.py:307-325` — `verify_disc` = `xorriso -indev <dev> -check_media`,
  `return result.returncode == 0`. The volume id of the disc in the drive is never read, so
  "disc in drive == volume just recorded" is never established.
- `src/lcsas/burn/orchestrator.py:806-809, 965-999` — receipts (which do carry
  `volume_label`) are written only after **all** volumes finish, into
  `<session_dir>/receipts/` on the staging disk; nothing is shown per-disc at swap time, and
  a crash mid-session leaves zero receipts for the discs already burned.
- `grep -rn volume_info src/lcsas/restore/ recovery/scripts/restore.sh` → no matches: the
  self-identity file written onto every disc (`staging/metadata.py:122-160`) is consumed by
  nothing on the restore side.
- `src/lcsas/cli/main.py:1144-1205` — `cmd_burn_iso` (the manual re-burn path) has the same
  shape: burn, optional readability verify, optional receipt; no identity check, so
  `--emit-receipt` happily records a receipt for whatever disc was in the drive.

## Fix design

### Part 1 — immediate mitigations (implement now)

**1. Operator checkpoint between volumes** (`burn/orchestrator.py`). Introduce a small
injectable prompt seam so unit tests keep running without a TTY:

```python
class OperatorPrompt(Protocol):
    def checkpoint(self, message: str) -> None: ...   # blocks until operator confirms

class ConsoleOperatorPrompt:
    def checkpoint(self, message: str) -> None:
        print(message, file=sys.stderr)
        input("Press ENTER when ready... ")
```

`BurnOrchestrator.__init__` gains `prompt: OperatorPrompt | None = None`; `burn_session`
gains `interactive: bool = True` (CLI flag `--no-prompt` for scripted/cdemu use; `skip_burn`
implies non-interactive). In the loop, before each physical burn:

```
=== Disc 2 of 3 — volume LCSAS-BD25-0042 ===
Insert a BLANK disc into /dev/sr0.
```

After each verified burn: eject the tray (run `eject <device>`, best-effort, log on failure)
and print the labeling instruction **before** moving on:

```
Burn VERIFIED. Remove the disc and write on it now, with a soft felt-tip marker:
    LCSAS-BD25-0042        (location: Offsite_Safe)
```

**2. Blank-media pre-check** (`iso/xorriso.py`). New protocol method
`media_status(device) -> MediaStatus` (`blank | appendable | closed | no_media | unknown`)
implemented via `xorriso -outdev <device> -toc` output parsing. `burn_session` calls it
before each burn; on `closed` raise with: *"Disc in /dev/sr0 is not blank (it already
contains data). Remove it and insert a blank disc."* On `unknown`, warn and proceed (don't
brick odd drives). This also converts the "forgot to swap" failure from a raw
CalledProcessError + PARTIAL session into a clean retryable prompt.

**3. Post-burn identity readback** (`iso/xorriso.py` + orchestrator). New method
`read_volume_id(device) -> str | None` via `xorriso -indev <device> -pvd_info` ("Volume id"
line). In `burn_session`, after `verify_disc` passes, compare against `vol.label` (the
mastering call sets `-V volume_label`, xorriso.py:126). Mismatch ⇒ treat exactly like a
failed verify (`verify_passed = False`, `VERIFY_FAIL` event with detail
`"volume id mismatch: disc says '<X>'"`). This is cheap, independent of, and complementary
to BURN-04's full SHA-256 device readback; keep both (label check gives an actionable
*which-disc* message, hash check gives content truth).

**4. Per-disc receipt at burn time.** Move the receipt write for each volume to immediately
after its copy is recorded (inside the loop), keeping the end-of-session summary. A
mid-session crash then still leaves receipts for every disc actually burned. No schema
change — receipts are JSON files.

`cmd_burn_iso` gets mitigations 2 and 3 behind its existing `--verify` flag.

**Catalog/schema note:** none of the above changes schema v5 or catalog semantics; the only
new write is the existing `VERIFY_FAIL` event type with a new detail string, which old
restore-side readers ignore.

### Part 2 — follow-up audit charter (run after mitigations land)

Scope: the operator-facing burn protocol end-to-end, against the bar "a mislabeled disc must
not surface decades later as an unidentifiable 'pack not found'." Questions to answer:

- (a) Media-swap semantics: with mitigations in place, exercise still-loaded-old-disc,
  wrong-blank-size, and appendable-media cases on real hardware (this VM has no physical
  writer — needs the burn host). Does every path end in a clear retry prompt?
- (b) Identity end-to-end: should the *restore* paths (restore.sh disc-retry loop,
  `cmd_restore_from_disc`) read `volume_info.json` and say "this disc is actually
  LCSAS-BD25-0007 — its marker may be wrong" instead of silently retrying? Spec a
  `lcsas disc id <mount-or-device>` helper for identifying a stranger disc in hand.
- (c) Labeling artifacts: is a printable per-session label sheet needed (ties into the
  UX "printable recovery card" plan), and should receipts be archived somewhere durable
  given staging is WARM tier (ties into FMA "burn provenance not holographic")?
- (d) Phantom copies: `burn_session(skip_burn=True)` records ACTIVE copies and VERIFIED
  status with no disc ever burned (orchestrator.py:701-748 — all DB writes happen
  regardless of `skip_burn`); decide whether skip_burn must be quarantined from
  production catalogs.

Deliverable: findings appended to this plan or new BURN-xx plans; expected 1-2 focused days.

## Tests & gates

All unit tests use the existing fake-runner pattern (`tests/unit/test_burn_orchestrator.py`)
plus a scripted `FakeOperatorPrompt` recording messages; always-on via `make test-unit`
(.github/workflows/test.yml).

- `test_burn_session_checkpoints_between_volumes` — 3-volume session, interactive=True:
  prompt called once per volume, message contains the volume label and "Disc i of 3";
  eject invoked after each verified burn; labeling instruction emitted with exact label.
- `test_burn_session_no_prompt_flag` — `interactive=False` and `skip_burn=True`: prompt
  never called (protects cdemu/CI flows).
- `test_burn_refuses_closed_media` — fake `media_status` returns `closed`: burn_iso never
  called, error names the device and says "insert a blank disc", volume stays STAGING,
  no copy row written.
- `test_verify_fails_on_volume_id_mismatch` — fake `read_volume_id` returns wrong label:
  `verify_passed` False, `VERIFY_FAIL` event detail contains both labels, and (with
  BURN-05 landed) no ACTIVE copy is recorded.
- `test_receipt_written_per_volume` — kill the fake burn on volume 2 of 3; assert volume 1's
  receipt file exists.
- Integration (opt-in, cdemu env per `tests/integration` conventions): extend the blind
  restore/burn harness to run a 2-volume session with `--no-prompt` against virtual drives;
  physical blank-check/eject behavior must be validated once on the real burn host
  (no optical writer on this VM).

## Acceptance criteria

- [ ] `grep -c "input(" src/lcsas/burn/orchestrator.py` ≥ 1 via the prompt seam, and a
      3-volume `burn session` run pauses twice with the next label on screen.
- [ ] Burning onto a closed disc produces a retryable "insert a blank disc" error, not a
      PARTIAL session.
- [ ] After every burn, the disc's volume id is read back and a mismatch fails verify with
      both labels in the message.
- [ ] Each verified burn ejects the tray and prints "write on it now: <label>".
- [ ] A receipt JSON exists for every burned volume even if the session aborts midway.
- [ ] All new unit tests pass in `make test-unit`; `make gate` green.
- [ ] Follow-up charter questions (a)-(d) answered in writing (new findings filed as plans).

## Dependencies & related plans

- BURN "retain ISO for multi-location burns" (BURN-06) — the retry-after-PARTIAL dead-end
  this plan exposes; land in the same milestone.
- BURN "device readback SHA-256 verify" (BURN-04) and "failed verify no ACTIVE copy"
  (BURN-05) — identity readback should reuse BURN-04's verify plumbing and BURN-05's
  bookkeeping; coordinate the `verify_passed` pathway, order after them or rebase onto them.
- FMA "burn provenance not holographic" (FMA-10) and UX "printable recovery card" (UX-09) —
  charter item (c).
- FUP-02 catalog-concurrency — the burn-long lock hold interacts with prompts that now make
  burns even longer (operator away from keyboard).

## Effort

Mitigations: 2 days impl + 1 day tests (fakes only; +0.5 day on the real burn host for
hardware validation of blank-check/eject). Charter audit: 1-2 days. Total ≈ 4-5 days.

---
**Implemented:** 2026-06-13. Part (1) immediate mitigations as planned — operator checkpoint seam (`burn/operator.py`: `OperatorPrompt` Protocol, `ConsoleOperatorPrompt`, `NullOperatorPrompt`, `eject_tray`), `--no-prompt`/`interactive` flag (skip_burn implies non-interactive), blank-media pre-check (`MediaStatus` + `media_status` via `xorriso -outdev -toc`), per-disc durable receipt at burn time, and `cmd_burn_iso` blank-check + identity readback behind `--verify`. Identity-readback mismatch (mitigation 3) reuses the existing `read_disc_volume_id` + `_verify_burned_disc` VERIFY_FAIL plumbing landed earlier (BURN-04/FMA-03). `MediaStatus` uses `StrEnum` (py3.11+, ruff UP042). Part (2) charter remains documentation. Touched test fixtures inject `NullOperatorPrompt` so the new console-default prompt never blocks the suite.
