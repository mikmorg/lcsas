# UX-02: split-key on-disc docs cite nonexistent restore.sh flags; add docs-vs-reality gate

**Priority:** P0 · **Severity:** high · **Dimension:** ux-journey · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Fix phantom restore.sh flags in burned split-key docs; add doc-contract gate

## Problem

Every split-key archive burns KEY_INFO.txt and START_HERE.txt telling the heir, at
the single most fragile step of the whole journey (password reconstruction from
Shamir share cards), to run `./restore.sh --target ~/restored` and optionally "pass
the saved file with --key repo.key". Neither flag exists. `restore.sh`'s flag loop
accepts only `-h/--help`, `--repo`, and `--version`; `--target` falls through to
positional parsing, becomes the literal TARGET_DIR `"--target"`, and
`mkdir -p "--target"` aborts under `set -eu` — **after** the heir has already typed
the reconstructed password. The combiner's own header repeats the phantom `--key`
flag, and an expert-facing validation doc repeats `--target`.

For a non-technical heir this is a cryptic dead end ("mkdir: invalid option") right
after they did the hardest part correctly. The class of bug — generated/burned text
drifting from the script it describes — has no gate today, which is how four
separate findings of this shape shipped (this one, UX-03 boot promise, UX-04
Windows manual, KEY share-card guidance). This plan owns the **generated doc text
fixes** and the **generic docs-vs-reality contract gate**; combiner/flag
implementation choices (e.g. whether restore.sh grows real `--target`/`--key`
flags) belong to the KEY plans.

## Evidence

(Re-checked 2026-06-10.)

- `src/lcsas/staging/metadata.py:70` — `_share_recovery_lines` (→ KEY_INFO.txt):
  `"      ./restore.sh --target ~/restored",` and `:73` — "(or pass the saved file
  with --key repo.key)".
- `src/lcsas/staging/metadata.py:363-370` — START_HERE split block repeats both
  `python3 keyshare_combine.py <card1> <card2>` and `./restore.sh --target ~/restored`.
- `src/lcsas/meta/keyshare_combine.py:23` — header: `./restore.sh --key repo.key
  --target ~/restored`.
- `recovery/scripts/restore.sh:266-338` — flag loop `case "$1" in` handles only
  `-h|--help`, `--repo`, `--version`, then `*) break ;;`. Positional pattern 1 treats
  the first arg as a recovery root only if it has `bin/` or `src/`; `"--target"` thus
  becomes `TARGET_DIR` (assigned at line 392), and `restore.sh:740`
  `mkdir -p "$TARGET_DIR"` aborts under `set -eu` (line 34) — after the password
  prompt at lines 727-737.
- `recovery/docs/PHYSICAL_DISC_VALIDATION.txt:80` —
  `recovery/scripts/restore.sh --target /tmp/restored ...`.
- Existing partial coverage: `make blind-restore-split-2of5` (Makefile:180) exercises
  the split journey but is opt-in (~$5, cdemu, sudo, Linux-only) and agent-driven —
  the agent may deviate from the literal on-disc commands, which is exactly how this
  shipped.

## Fix design

**A. Fix the four instruction sites to the real interface** (do not wait for KEY's
decision on adding flags; if KEY later adds them, the contract gate keeps both honest):

1. `src/lcsas/staging/metadata.py` `_share_recovery_lines` STEP 2 (lines 68-74):
   ```
   STEP 2: run the normal restore and use that password:

       sh restore.sh ~/restored

     When restore.sh shows the  Password:  prompt, type the password
     from STEP 1.  If you saved it to a file, you can skip the prompt:

       LCSAS_PWFILE=repo.key sh restore.sh ~/restored
   ```
2. Same rewrite in the START_HERE split block (`metadata.py:361-374`).
3. `src/lcsas/meta/keyshare_combine.py:22-24` header — same two commands.
4. `recovery/docs/PHYSICAL_DISC_VALIDATION.txt:80` —
   `recovery/scripts/restore.sh /tmp/restored` (+ `LCSAS_PWFILE=` note).

Also in sites 1-2, add the no-system-python fallback line (the docs currently assume
`python3` on PATH; the meta disc bundles CPython per target):
```
If `python3` is not installed, use the copy on this disc:
    recovery/bin/<your-platform>/python/bin/python3 keyshare_combine.py <card1> <card2>
```
(`keyshare_combine.py` sits at the meta-volume root — `builder.py:1911` — so the
relative path works from the disc root.)

**B. Generic docs-vs-reality contract gate** — new always-on unit test
`tests/unit/test_doc_command_contract.py` (placed under `tests/unit/` deliberately:
CI runs only `make test-unit`/`test-integration`, so `tests/recovery_hardening/`
would not gate merges — verifier-confirmed):

- `accepted_restore_sh_flags()`: parse the `case "$1" in` flag loop of
  `recovery/scripts/restore.sh` → `{-h, --help, --repo, --version}` (future flags
  picked up automatically).
- Corpus, two halves:
  - *Generated*: instantiate `HolographicInjector` against a tmp dir with a fixture
    `LCSASConfig(key_split=True, key_threshold=2, key_shares=5, ...)`, call
    `write_start_here`, `write_key_info`, `write_restore_instructions`; build the
    no-config META START_HERE via `MetaVolumeBuilder._write_start_here` (or scan a
    built fixture tree). Scan the *output text* — the artifact is the contract.
  - *Static*: `src/lcsas/meta/keyshare_combine.py`, `recovery/docs/*.txt`,
    `docs/workflows/*.md`.
- Assertion 1: every line containing `restore.sh` — every `--flag` token on it must be
  in the accepted set.
- Assertion 2: every `standalone_restorer.py` invocation must contain `--repo`,
  `--password-file`, `--target` and no bare positional path args (extended by UX-04).
- Assertion 3 hook: every `lcsas <sub> --flag` mention in `recovery/docs/` exists in
  the argparse tree (introspect `lcsas.cli.main`; UX-03 adds the `meta build` case).
- Keep an explicit allowlist file in the test for intentional counter-examples
  (e.g. docs that *show* a wrong command to warn against it).

## Tests & gates

- `tests/unit/test_doc_command_contract.py` (above) — always-on, runs in
  `make test-unit` and `.github/workflows/test.yml`. Must fail on the pre-fix tree
  (proves the gate) and pass after.
- `tests/unit/test_metadata.py` — extend existing KEY_INFO/START_HERE tests: split-key
  fixture output contains `sh restore.sh` and `LCSAS_PWFILE=`, and does **not**
  contain `--target` or `--key`.
- Blind-drill tightening (opt-in, with KEY-04): extend
  `tests/e2e/cdemu_blind_restore` split-variant `verify.sh` to assert the literal
  STEP 1/STEP 2 commands from the burned KEY_INFO.txt succeed when executed verbatim
  (not merely that the agent eventually restored). Memory note: blind runs use haiku.

## Acceptance criteria

- [ ] `grep -rn -- '--target\|--key' src/lcsas/staging/metadata.py src/lcsas/meta/keyshare_combine.py` shows no restore.sh-related hits.
- [ ] Executing the literal STEP 1 + STEP 2 commands from a freshly generated
      KEY_INFO.txt against a built meta tree restores successfully (manual or blind run).
- [ ] `pytest tests/unit/test_doc_command_contract.py -v` passes; reverting any one
      doc fix makes it fail.
- [ ] The gate also covers START_HERE, RECOVER*.txt, keyshare_combine.py, and
      docs/workflows/ in the same run.

## Dependencies & related plans

- **KEY** "on-disc split-key instructions tell the heir to run restore.sh with flags
  that do not exist" — same finding; KEY owns whether restore.sh gains real
  `--target`/`--key` flags and all combiner behavior. Land this doc/gate plan first;
  it is unconditionally correct either way.
- **UX-04** (RECOVER_WINDOWS.txt sweep) and **UX-03** (boot promise) extend the same
  contract test file — land UX-02's skeleton first.
- **KEY** share-card rejection (P0) — STEP 1 as documented still fails on real card
  files until that lands; sequence both before the next burn.

## Effort

2 days: 0.5 doc text fixes, 1.5 contract-gate test (generator fixtures + extraction
regexes + allowlist). No special environment.
