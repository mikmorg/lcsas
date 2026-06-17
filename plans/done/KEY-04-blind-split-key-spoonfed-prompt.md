# KEY-04: Blind split-key drill spoon-feeds commands and stages non-production share artifacts

> **STATUS: RESOLVED** — landed in `5e463f3` (test(blind): keep split-docs prompt spoon-feed-free [KEY-04]); guarded by `tests/unit/test_blind_prompt_hygiene.py`.

**Priority:** P1 · **Severity:** high · **Dimension:** keys-escrow · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Add docs-driven blind split-key variant with real card files

## Problem

The split-key blind-restore variant — the project's only end-to-end proof of
the heir's split-key journey, scored 15/15 on four consecutive runs — does not
test the journey at all. Its prompt contains a QUICK REFERENCE giving the
agent the precise winning command sequence: the arch-specific combiner path
(`/mnt/recovery/bin/x86_64/lcsas-keyshare`), the exact
`sh /mnt/restore.sh ~/restored/ latest` invocation, and "DO NOT deviate from
these steps." Its setup stages bare mnemonic files (mnemonic + newline) as
"cards", not the production `-card.txt` artifacts holders actually receive.

Consequently the drill never exercises the on-disc START_HERE/KEY_INFO
instructions, card-file parsing, or the typed-from-print path — and it
demonstrably failed to detect that the on-disc instructions are broken in two
independent ways (KEY-01: cards rejected by every combiner; KEY-02: phantom
restore.sh flags). Both sailed through four 15/15 runs. The gate gives false
assurance on exactly the layer that is broken: what the heir reads and holds.

## Evidence

Re-checked 2026-06-10 against master:

- `tests/e2e/cdemu_blind_restore/agent_prompt_split.txt:11-35` — full command
  sequence including
  `/mnt/recovery/bin/x86_64/lcsas-keyshare ~/alpha-share-1.txt ~/alpha-share-2.txt > ~/alpha.pw`
  and `restore-shell start sh /mnt/restore.sh ~/restored/ latest`; line 105:
  "DO NOT deviate from these steps." The prompt forbids reading on-disc
  scripts but never requires following on-disc docs.
- `tests/e2e/cdemu_blind_restore/setup.py:575-586` — variant
  `split-key-2of5` writes `share_dst.write_text(mnemonic + "\n")` — bare
  mnemonics, never `-card.txt` card text.
- `tests/e2e/cdemu_blind_restore/run.sh:25-29` — prompt selection by
  `LCSAS_VARIANT`; `run_variant.sh:52-121` — variant plumbing + XFAIL list.
- Contrast: the burned instructions the heir would actually follow are at
  `src/lcsas/staging/metadata.py:61-73` (broken until KEY-01/KEY-02 land).

## Fix design

Add a **docs-driven** variant and demote the current scripted one to a tooling
smoke test. Keep both: the scripted variant isolates tool regressions cheaply;
the docs-driven variant is the acceptance gate for heir-doc changes.

1. **New prompt** `tests/e2e/cdemu_blind_restore/agent_prompt_split_docs.txt`:
   scenario only —
   - "You inherited a box of discs labelled LCSAS_*. You hold 2 printed share
     cards (files `~/alpha-share-1-card.txt`, `~/alpha-share-2-card.txt`).
     Insert the disc labelled LCSAS_META and follow the instructions you find
     on it. Restore everything into ~/restored/ then say RESTORE COMPLETE."
   - Plus ONLY test-rig mechanics (disc-loader usage, restore-shell facade,
     the single-drive note) — no LCSAS command, path, or binary name anywhere.
2. **Setup stages production artifacts** — in `setup.py`'s variant branch
   (575-586), for `LCSAS_VARIANT=split-key-docs`: invoke the real split path
   (call `cmd_key_split` via the CLI against `SECRETS/alpha.pw`, or import and
   reuse `_share_card_text` after `split_secret`) and stage the resulting
   `-card.txt` files (mode 0600) — bare share files are NOT staged. This makes
   card parsing and the START_HERE STEP 1/STEP 2 text load-bearing.
3. **Variant wiring** — `run.sh` prompt selection gains the
   `split-key-docs` → `agent_prompt_split_docs.txt` mapping; root `Makefile`
   gains `blind-restore-split-docs` (clone of `blind-restore-split-2of5`,
   lines 180-188, same `LCSAS_BLIND_ACK_COST` guard; **haiku model only**, per
   the established blind-restore policy). Score on the same 15-point rubric
   via the existing `verify.sh`.
4. **Prompt-hygiene check (no LLM cost, always-on)** — new
   `tests/e2e/cdemu_blind_restore/prompt_hygiene_check.py` (pattern of
   `no_bypass_check.py`): assert `agent_prompt_split_docs.txt` contains none
   of `restore.sh`, `keyshare`, `lcsas-`, `/mnt/recovery/bin`, `--target`,
   `--key`. Run it from a unit test
   (`tests/unit/test_blind_prompt_hygiene.py`) so `make gate` enforces that
   the docs-driven prompt stays docs-driven.
5. **Acceptance policy** — record in
   `tests/e2e/cdemu_blind_restore/PLAN.md`: any PR changing
   `staging/metadata.py` heir text, `keyshare_combine.py`, or restore.sh flags
   must pass `blind-restore-split-docs` 15/15 **twice consecutively** before
   merge; the cheap pre-gate for every PR is KEY-02's
   `test_heir_doc_commands.py`.

Edge cases: the rubric's no-bypass check stays valid (the agent may now read
on-disc *docs*; `no_bypass_check.py` only flags direct binary invocation —
unchanged). Expect first runs to FAIL until KEY-01/KEY-02/KEY-05 land; that
failure is the point and must not be XFAIL'd (contrast the permanently-XFAIL
tier1-missing variant criticized by the GATE dimension).

## Tests & gates

- `tests/unit/test_blind_prompt_hygiene.py` — always-on (`make gate`): the
  docs-driven prompt contains no command spoon-feed tokens (list above); the
  scripted prompt is exempt.
- `tests/unit/test_blind_setup_stages_cards.py` — run setup's card-staging
  helper in-process: staged files end with `-card.txt`, contain
  `LCSAS KEY SHARE` header AND a 20/33-word mnemonic line; no bare share file
  staged for the docs variant.
- `make blind-restore-split-docs` — opt-in (`LCSAS_BLIND_ACK_COST=1`,
  ~USD 5/run, haiku): gate = 15/15 twice consecutively; teardown via existing
  `blind-restore-teardown`.
- Local-only/cdemu constraints apply (no optical hardware on CI); the
  UX-dimension plan for promoting blind drills to scheduled CI covers hosting.

## Acceptance criteria

- [ ] `LCSAS_VARIANT=split-key-docs make blind-restore-split-docs` runs with the new prompt and card-only secrets.
- [ ] Prompt-hygiene unit test green and proven by mutation (add `restore.sh` to the prompt → test fails).
- [ ] After KEY-01/02/05 land: two consecutive 15/15 docs-driven runs, transcript shows the agent derived commands from on-disc docs (mounted META, read START_HERE/KEY_INFO).
- [ ] Before those fixes land, the variant demonstrably fails at STEP 1 or STEP 2 (recorded once as evidence, not XFAIL'd).
- [ ] Existing `split-key-2of5` scripted variant still passes (tooling smoke).

## Dependencies & related plans

- **KEY-01**, **KEY-02**, **KEY-05** — must land for the variant to pass;
  build the variant in parallel, run it red once, then gate on green.
- **KEY-02** — provides the always-on cheap contract pre-gate.
- **UX** "The only end-to-end journey gate (cdemu blind restore) is
  Linux-only, local-only, cost-gated" — scheduling/hosting umbrella; this
  variant slots into whatever cadence that plan establishes.
- **GATE** "Tier-2 fallback … permanently XFAIL" — same anti-pattern to avoid
  here.

## Effort

1.5 days impl (prompt + setup branch + Makefile + hygiene tests) + ~USD 20-30
of blind runs across the red/green proof. Needs the local cdemu rig (this VM
has CDEmu; do not run pytest concurrently with blind runs).

---
**Implemented:** 2026-06-13. As planned, with one deviation: the card-staging
test asserts the mnemonic line is >= 20 words (the plan's "20/33" was
illustrative; SLIP-0039 word count tracks the framed-secret length — a
28-byte password yields a 31-word share). The recombine round-trip test
proves the words are genuine. New docs-driven variant `split-key-docs`
(scenario-only prompt + 2 production `-card.txt` artifacts), wired through
run.sh/run_variant.sh/Makefile (`blind-restore-split-docs`, haiku-only, not
XFAIL'd). Always-on `prompt_hygiene_check.py` + two unit test files enforce
docs-driven hygiene and card staging under `make gate`. The cost-gated blind
run itself was not executed (opt-in, requires the cdemu rig + real sub-agent).
