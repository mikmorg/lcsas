# KEY-03: `lcsas key split` never verifies the escrowed secret

**Priority:** P0 · **Severity:** high · **Dimension:** keys-escrow · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Verify-at-split + add `lcsas key verify` annual drill command

## Problem

`cmd_key_split` reads the password bytes, splits them, writes share + card
files, prints success — and verifies nothing. There is (a) no recombine
round-trip of the freshly *written* share files, and (b) no check that the
password actually unlocks the repository's key file, even though a pure-Python
scrypt+Poly1305 unlock path already exists in `restore/restic_fallback.py` and
the repo `keys/` directory sits in the mirror. Nothing else in the burn
lifecycle ever exercises the password either: scan/stage/ISO/ECC are
byte-copies, and the burn preflight checks only binaries and capacity. So a
stale, rotated, or simply wrong `password_file` is split, distributed, and —
per ESTATE_PLANNING's implication that single copies are then destroyed —
becomes the *only* escrow of a password that opens nothing. The heir, decades
later, reconstructs a perfectly checksummed wrong password. SLIP-0039's
checksums all pass; the failure is undetectable until the one moment it is
unrecoverable.

There is also no tool for the READINESS checklist's "re-confirm annually" key
drill (`lcsas key verify` does not exist), and no rotation story: re-keying
the repo silently invalidates every distributed card with nothing to detect or
document it.

## Evidence

Re-checked 2026-06-10 against master:

- `src/lcsas/cli/main.py:3214-3294` — `cmd_key_split`: read (3260) → split
  (3262-3264) → write (3274-3279) → print success (3281-3293). No recombine,
  no unlock check.
- `src/lcsas/burn/orchestrator.py:233-292` — preflight = binary versions +
  capacity only; the password is never touched anywhere in the burn pipeline.
- `src/lcsas/restore/restic_fallback.py:221-300` — `_load_master_key` /
  `_try_keys(keys_dir, password)` already implement scrypt + Poly1305 key-file
  authentication (raises `IntegrityError` on wrong password);
  `PurePythonRestorer.verify_key` (484-493) wraps it.
- `src/lcsas/config/settings.py:67-73` — `RepositoryConfig.mirror_path` gives
  the mirror repo root; rustic layout puts key files under
  `mirror_path / "keys"`.
- `recovery/docs/READINESS_CHECKLIST.txt:32-42` — "ENCRYPTION KEY REDUNDANCY …
  re-confirm annually" is entirely manual; names no command.
- No `key verify` subcommand exists (`main.py:3155-3162` dispatches only
  split/combine). Rotation guidance: docs/workflows mention key rotation in
  passing (multi-tenant.md:322) but nothing addresses escrow-card
  invalidation.

## Fix design

Three layers, all in `src/lcsas/cli/main.py` + small helpers:

1. **Recombine round-trip (always on, no flag).** After writing all files in
   `cmd_key_split`, read the share files **back from disk** and verify:
   ```python
   from itertools import combinations
   written = [out_dir / f"{args.repo}-share-{i}.txt" for i in range(1, shares + 1)]
   cards   = [out_dir / f"{args.repo}-share-{i}-card.txt" ...]
   subsets = list(combinations(written, threshold))
   if len(subsets) > 64: subsets = subsets[:64]   # bound C(N,K) blowup
   for subset in subsets:
       got = decode_master_secret(recover_secret([extract_mnemonic(p.read_text()) ...]))
       if got != password: → delete nothing, exit 1:
           "SPLIT FAILED VERIFICATION: shares {names} do not reconstruct the
            input password. Do NOT distribute these cards."
   ```
   Also round-trip one K-subset of the **card** files through
   `extract_mnemonic` (depends on KEY-01) so the printed artifact is what's
   verified, not just the bare files.
2. **Repo-unlock verification (default on, `--no-verify-repo` to skip).**
   When the repo's mirror is resolvable (config given), authenticate the
   password against the real key files:
   ```python
   keys_dir = repo_cfg.mirror_path / "keys"
   _try_keys(keys_dir, password)   # restic_fallback; IntegrityError on mismatch
   ```
   - Wrong password → exit 1: `"the password in {file} does NOT unlock
     repository '{repo}' ({n} key file(s) checked under {keys_dir}). You were
     about to escrow a wrong password. If you really intend this, pass
     --no-verify-repo."` Share files written before the check? No — run both
     verifications **before** `_write_private_file` calls, so a failed split
     leaves nothing on disk to mis-distribute. (Recombine check then operates
     on in-memory mnemonics first, and re-reads files after writing as a
     write-path check — keep both.)
   - `keys_dir` missing/empty → exit 1 naming the path and `--no-verify-repo`
     (fail closed: escrow is exactly the operation where "probably fine" is
     wrong). `--password-file` without `--config` → unlock check impossible;
     print a prominent warning that the password was NOT verified against any
     repository.
3. **`lcsas key verify` (the annual drill).** New subparser next to
   split/combine (`main.py:483-520` parser region, dispatch at 3155-3162):
   ```
   lcsas key verify --repo R --config lcsas.toml --share-file CARD [--share-file CARD ...]
   lcsas key verify --repo R --config lcsas.toml --password-file FILE
   ```
   Reconstructs from the supplied shares/cards (or reads the file), then runs
   the `_try_keys` unlock against `mirror_path/"keys"`. Success prints:
   `"OK: these {k} share(s) reconstruct the password that unlocks '{repo}'
   ({n} key files checked)."` Failure modes get distinct messages
   (reconstruction failed vs unlocked nothing). Exit code is the contract.
4. **Rotation documentation + card provenance.** Add a "ROTATION" subsection
   to `docs/ESTATE_PLANNING.md` (after the split checklist at 73-95) and
   `docs/KEY_SHARE_FORMAT.md`: re-keying or changing the password invalidates
   ALL distributed cards; procedure = re-split → redistribute → recall/destroy
   superseded cards → `lcsas key verify` with the new cards. Stamp each card
   (`_share_card_text`, `main.py:3183-3211`) with `Split on : YYYY-MM-DD` and
   the SLIP-0039 identifier (`Split ID : NNNNN`, via a small
   `share_identifier(mnemonic)` helper in `lcsas/keyshare`) so stale card sets
   are identifiable in the field. Update
   `recovery/docs/READINESS_CHECKLIST.txt:32-42` to name `lcsas key verify` as
   the annual drill command.

No catalog/schema change here (recorded split state is KEY-08). Old cards
without the `Split on`/`Split ID` lines remain valid combiner input (KEY-01's
extractor ignores non-wordlist lines, so new header lines are also safe).

## Tests & gates

Always-on, `tests/unit/test_cli_key.py` (runs in `make test-unit` → `gate`):

- `test_split_roundtrip_verifies` — monkeypatch `_write_private_file` to
  corrupt one share's bytes; `cmd_key_split` exits 1 with "FAILED
  VERIFICATION"; no success message.
- `test_split_rejects_wrong_repo_password` — fixture repo with a real
  `keys/` key file (reuse/extend the restic_fallback key fixtures in
  `tests/unit/` — they already construct scrypt key documents); point
  `password_file` at a wrong password → exit 1, message names repo, keys dir,
  and `--no-verify-repo`.
- `test_split_no_verify_repo_skips_unlock` — same fixture, flag passed → split
  succeeds, warning on stderr.
- `test_split_fails_closed_when_keys_dir_missing` — no `keys/` → exit 1
  naming the path.
- `test_key_verify_detects_stale_shares` — split, then replace the fixture
  `keys/` file with one derived from a different password; `lcsas key verify
  --share-file …` exits non-zero ("does not unlock").
- `test_key_verify_ok_path` — happy path prints OK, rc 0, accepts `-card.txt`
  files (with KEY-01).
- `test_card_carries_split_date_and_id` — card text contains both stamps; the
  ID matches across all N cards of one split and differs between two splits.
- Doc pin: `tests/recovery_hardening/test_keyshare_docs.py` (or extend
  `test_env_var_docs.py` pattern) — READINESS_CHECKLIST key-redundancy item
  mentions `lcsas key verify`.

## Acceptance criteria

- [ ] Splitting with a password that doesn't unlock the configured repo exits 1 before any share file is written.
- [ ] A corrupted write is caught by the post-write recombine (mutation test passes).
- [ ] `lcsas key verify --repo alpha --config … --share-file card1 --share-file card2` → rc 0 "OK" against the live mirror; rc≠0 after re-keying the fixture.
- [ ] Cards carry split date + SLIP-0039 identifier; combiners still accept them.
- [ ] READINESS_CHECKLIST names the verify command; static doc test enforces it.
- [ ] `make gate` green with all of the above always-on.

## Dependencies & related plans

- **KEY-01** (card-tolerant combiners) — provides `extract_mnemonic` used by
  the card round-trip and lets `key verify` take cards. Land KEY-01 first.
- **KEY-08** (recorded split state) — `cmd_key_split` is the natural place to
  also record K/N/identifier; coordinate the success-output wording.
- **KEY-09** (Recovery Card generator) — single-key sibling; `key verify
  --password-file` is its check path too.
- **FUP-03** (disc-confidentiality follow-up) — notes that discs ship
  `keys/`; unrelated to this verification but shares the `_try_keys` surface.

## Effort

2.5 days: 1.0 split verification + fail-closed paths, 0.75 `key verify`
subcommand + parser, 0.75 fixtures/tests/docs. No external binaries needed
(pure-Python unlock path).
