# KEY-02: On-disc split-key docs reference restore.sh flags that do not exist

> **STATUS: RESOLVED** — landed in `33eb565` (recovery: real --target/--key flags in restore.sh + doc-flag contract gate [KEY-02]); guarded by `tests/unit/test_heir_doc_commands.py`.

**Priority:** P0 · **Severity:** high · **Dimension:** keys-escrow · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Add real --target/--key flags to restore.sh; gate doc-flag consistency

## Problem

The split-key block burned into every disc's `START_HERE.txt` and
`KEY_INFO.txt` tells the heir, as STEP 2 after reconstructing the password:
`./restore.sh --target ~/restored` — and "(or pass the saved file with
`--key repo.key`)". Neither flag exists. `restore.sh`'s flag parser accepts
only `-h/--help`, `--repo`, and `--version`; its `*) break` arm silently
demotes any unknown flag to a positional argument. So `--target` becomes
`TARGET_DIR` (a directory literally named `./--target`) and `~/restored`
becomes the `SNAPSHOT_ID` — the run fails with a snapshot-not-found error a
non-technical heir cannot interpret, *after* they have already done the hard
part (correctly reconstructing the password from share cards).
`keyshare_combine.py`'s own header usage repeats the bogus
`./restore.sh --key repo.key --target ~/restored`.

This is a docs↔script contract break with no gate: the existing flag tests
cover only the flags that do exist, nothing checks that generated heir docs
reference only real flags, and the blind split-key drill spoon-feeds the
correct command so it never exercises the on-disc text (KEY-04). Every burned
disc to date carries the broken instruction permanently.

This plan owns the **decision** (fix the script vs fix the docs) and the
script-side work; the UX-dimension twin of this finding owns sweeping the
remaining doc text once the decision lands.

## Evidence

Re-checked 2026-06-10 against master:

- `recovery/scripts/restore.sh:267-337` — `while [ $# -gt 0 ]; do case "$1" in`
  accepts only `-h|--help` (271), `--repo` (329), `--version` (332);
  `*) break ;;` (336) makes unknown flags positional. Comment at 262-266
  documents the break-on-first-non-flag behavior.
- `recovery/scripts/restore.sh:288-292` — the only key-file mechanisms are
  `LCSAS_PASSWORD` / `LCSAS_PWFILE`; no `--key`.
- `src/lcsas/staging/metadata.py:70` — `"      ./restore.sh --target ~/restored",`
  and `:73` — `"(or pass the saved file with --key repo.key)"` in
  `_share_recovery_lines` (KEY_INFO); `:367-370` — same STEP 2 command in the
  START_HERE `split_block`.
- `src/lcsas/meta/keyshare_combine.py:22-23` — header usage:
  `./restore.sh --key repo.key --target ~/restored`.
- `tests/recovery_hardening/test_restore_sh_repo_flag.py`,
  `test_restore_sh_version_flag.py` — only real flags tested; no doc/flag
  consistency check anywhere.

## Fix design

**Decision: add genuine `--target` and `--key` flags to restore.sh** (rather
than rewriting docs to env-var/positional forms). Two reasons: flags are the
form a non-technical heir can type from a printed page without understanding
env vars or positional grammar, and the already-burned discs + the bundled
`keyshare_combine.py` header *become retroactively correct* — the existing
broken instruction starts working on old discs the moment the heir uses a
fresh META disc's restore.sh. The "restore.sh byte-for-byte unchanged"
principle from the key-escrow build was already relaxed when `--repo` and
`--version` were added; this follows that precedent.

1. **`recovery/scripts/restore.sh`** — extend the case block (267-337):
   ```sh
   --target)
       TARGET_FLAG="${2:?--target requires a DIR argument}"; shift 2 ;;
   --key)
       LCSAS_PWFILE="${2:?--key requires a FILE argument}"
       export LCSAS_PWFILE; shift 2 ;;
   --) shift; break ;;
   -*)
       echo "restore.sh: unknown flag '$1'" >&2
       echo "valid flags: --repo NAME, --target DIR, --key FILE, --version, --help" >&2
       exit 2 ;;
   *) break ;;
   ```
   - `--key` maps onto the existing `LCSAS_PWFILE` plumbing — no new password
     code path. Validate early: file must exist and be readable, else
     `restore.sh: --key: cannot read 'X'` exit 2 (before any disc prompts).
   - After the positional block, apply `TARGET_FLAG`: if set and a positional
     TARGET was also given, exit 2 with "give the target once: either
     --target DIR or a positional TARGET_DIR". If set, `TARGET="$TARGET_FLAG"`.
   - The `-*)` arm **kills the silent-misparse class**: any future phantom
     flag now fails loudly with the valid-flag list instead of producing an
     uninterpretable snapshot error. (Targets beginning with `-` must use
     `--target`; acceptable.)
   - Update the `--help` usage line and FLAGS section (276-330) to document
     both flags.
2. **Generated docs** — `src/lcsas/staging/metadata.py:68-73` and `:367-370`:
   change `./restore.sh` to the mount-robust `sh /mnt/restore.sh --target
   ~/restored` (matching the QUICK START's existing `sh /mnt/restore.sh`
   phrasing) and keep the now-true `--key repo.key` sentence.
   `keyshare_combine.py:22-23` header is now correct as written — leave it.
3. **Contract gate (the doc-vs-reality piece for restore.sh flags)** — new
   always-on `tests/unit/test_heir_doc_commands.py`:
   render START_HERE.txt + KEY_INFO.txt via `HolographicInjector`
   (`write_start_here`/`write_key_info`) with `key_split=True`; regex-extract
   every `--[a-z-]+` token from lines mentioning `restore.sh` (also scan
   `keyshare_combine.py`'s module source and `docs/ESTATE_PLANNING.md`); parse
   `recovery/scripts/restore.sh`'s case block for accepted flags; assert
   extracted ⊆ accepted. This test fails today and passes after steps 1-2.

No catalog/schema impact. Already-burned discs: their START_HERE text is
frozen, but because the fix makes the *script* honor the frozen text, old
discs are healed by any new META disc (and even by old META discs only for
`--target`-as-misparse — note in RELEASE notes that pre-fix META discs still
misparse; the heir-visible remedy is "use the newest META disc").

## Tests & gates

- `tests/unit/test_heir_doc_commands.py` (above) — always-on, `make test-unit`
  → `make gate`. This is the cheap pre-gate that KEY-04's blind variant
  assumes.
- `tests/recovery_hardening/test_restore_sh_target_key_flags.py` (pattern of
  `test_restore_sh_repo_flag.py`):
  - `--target /tmp/x` sets TARGET (assert via the script's early echo/trace or
    a controlled failure point) and never becomes a positional arg;
  - `--key <pwfile>` exports LCSAS_PWFILE and skips the Password: prompt;
  - `--key /nonexistent` → rc 2, message names the path;
  - `--bogus` → rc 2, message lists valid flags (regression guard for the
    silent-misparse class);
  - `--target X` + positional TARGET → rc 2.
  Runs in `make test-recovery-hardening` (CI wiring is the GATE-dimension
  "shippable build gate never runs in CI" plan).
- `make shell-coverage` — add `--target`/`--key`/unknown-flag cases so the
  restore.sh coverage report covers the new arms.
- Blind proof: KEY-04's docs-driven split variant executes STEP 2 exactly as
  burned docs state.

## Acceptance criteria

- [ ] `sh restore.sh --target /tmp/r --key /tmp/pw <recovery_root_args>` restores using /tmp/pw without prompting; target is /tmp/r.
- [ ] `sh restore.sh --frobnicate` exits 2 listing valid flags; nothing is treated as TARGET_DIR.
- [ ] `tests/unit/test_heir_doc_commands.py` passes, and fails if anyone adds a new `--flag` to generated docs without restore.sh support (verified by mutation: add `--fake` to metadata.py locally → test fails).
- [ ] Rendered KEY_INFO/START_HERE split blocks show `sh /mnt/restore.sh --target ~/restored`.
- [ ] Existing `--repo`/`--version`/positional tests still pass; single-key flow unchanged.

## Dependencies & related plans

- **UX** "On-disc split-key (Shamir) instructions tell the heir to run
  restore.sh with flags that do not exist" — doc-text twin; this plan decides
  add-flags, the UX plan sweeps remaining prose. Land this first.
- **GATE** "the 'shippable build' gate … never runs in CI" — makes the
  recovery_hardening flag tests merge-blocking.
- **KEY-04** (docs-driven blind variant) — end-to-end proof; depends on this.
- restore.bat has no flag work here (Windows journey is UX-01/INFRA-01).

## Effort

2 days: 1.0 restore.sh (flags + help + shell-coverage cases), 0.5 metadata.py
doc text + renders, 0.5 contract test. No special environment (sh + pytest).

---
**Implemented:** 2026-06-11. As planned; additionally covered the `--` end-of-flags arm with its own hardening test so shell-coverage covers every new case arm.
