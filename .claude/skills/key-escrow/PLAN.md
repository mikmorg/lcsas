# LCSAS Key-Escrow (Shamir Secret Sharing) — Execution Backlog

**Source of truth for the `key-escrow` driver skill.** Ordered, dependency-annotated.
One item = one branch = one PR. Phase gates are hard halts for user sign-off ("merge #N").
Phase 4 (blind iterate) runs autonomously until perfect score, then gates.

Status keys: `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked (needs a decision)

---

## Mission & acceptance gate

Eliminate the single unsolvable failure in `docs/SURVIVABILITY.md` §3.1 (key loss = total
loss) by splitting the per-repo password with Shamir Secret Sharing (SSS): **N shares, any
K reconstruct, K-1 reveal nothing.** Offline, zero-runtime-dependency, spec-reconstructable —
same durability contract as `lcsas-restore` (tier 1).

**DONE means all of:**
- New code at **100% line coverage** (`make coverage`), mypy-strict + ruff clean.
- restore.sh share branch covered by `make shell-coverage`; any C combiner passes `make audit-gate` (EXEMPTIONS contract).
- **Blind run A — single (no-split) key: `SCORE: 15/15`** on two consecutive runs.
- **Blind run B — 2-of-5 split key: `SCORE: 15/15`** on two consecutive runs.
- Both blind runs use the **production** meta-disc output unmodified (no overlay scripts — the PLAN.md acceptance gate of the existing harness).

---

## Locked design decisions (revisit only via a `[!]` item)

- **Secret split = the repo password** (the scrypt seed `restore.sh` already prompts for), not the derived master key. Short, human-relevant, plugs into the existing `Password:` prompt.
- **Format = SLIP-0039** (decided 2026-05-31). Checksummed word-mnemonic shares, optional passphrase, published spec. Vendor a stdlib-only pure-Python implementation + bundle the full spec on the meta-volume (re-implementability is preserved by shipping the spec, not by minimizing code). Official SLIP-0039 test vectors are the known-answer fixtures.
- **Default parameters = 2-of-5** (bias toward recoverability; a backup's dominant risk is loss, not theft). Configurable.
- **Recovery tool joins the 50-year critical path** → pure-Python (stdlib-only) combiner mandatory; C combiner optional but held to the tier-1 audit-gate bar if built.
- **Blind model = haiku only** (per the project's blind-restore-model rule). Never sonnet/opus for a blind gate run.

---

## Phase 0 — Durable primitive (no user-facing change)

- [x] **D0.1** Share format decided: **SLIP-0039** (user, 2026-05-31). Vendored stdlib-only pure-Python impl + spec on disc; official SLIP-0039 vectors as fixtures. deps: —
- [x] **K0.2** `src/lcsas/keyshare/` — stdlib-only SLIP-0039 split/combine + RS1024/digest integrity (812 LOC + 1024-word list). Independently re-verified by lead. deps: D0.1
- [x] **K0.3** 45/45 official SLIP-0039 vectors pass (15 valid / 30 invalid) + property tests; **100% line cov (349/349)**, ruff + mypy-strict clean. deps: K0.2
- [ ] **K0.4** `docs/KEY_SHARE_FORMAT.md` (binary/word layout, KDF tie-in, re-implementation guidance — sibling of `RESTIC_FORMAT_SPEC.md`); register for meta-volume bundling. deps: K0.2
- **GATE 0→1:** format locked, primitive round-trips + tamper-detects, 100% cov on the module, spec written. *Halt for "merge #N".*

## Phase 1 — CLI & share artifacts

- [ ] **K1.1** `lcsas key split --repo R --threshold K --shares N` (default 2-of-5): read repo password, emit N share files + printable plain-language **share cards** (txt: share text, index, K, "you need any K", who-else-holds-one hint). deps: K0.2
- [ ] **K1.2** `lcsas key combine [shares...]` → reconstruct password to stdout/file; `<K` shares → clear actionable error; corrupted share → named failure. deps: K0.2
- [ ] **K1.3** Config fields `key_threshold` / `key_shares` on `LCSASConfig`; wire defaults. deps: K1.1
- [ ] **K1.4** CLI unit tests (happy path, under-threshold, corrupted, wrong-set) — **100% cov on the new cli/key code.** deps: K1.1, K1.2
- **GATE 1→2:** CLI round-trips a real rustic password end-to-end; coverage green. *Halt.*

## Phase 2 — Recovery-path integration (zero-dep, production)

- [ ] **K2.1** Bundle the pure-Python combiner + spec on the meta-volume via `meta/bundler.py`; pin in `recovery/MANIFEST.sha256`. (Optional **K2.1c**: C combiner → must pass `make audit-gate`.) deps: K0.2, K0.4
- [ ] **K2.2** `recovery/scripts/restore.sh` share branch: detect share-based recovery, prompt "Do you have key shares? Enter any K" → reconstruct via bundled combiner → feed the password to the existing flow. **Single-key path stays byte-for-byte unchanged** (no shares → today's `Password:` prompt). deps: K2.1
- [ ] **K2.3** Heir docs: `START_HERE.txt` / `KEY_INFO.txt` / `docs/ESTATE_PLANNING.md` — plain-language share-gathering steps + letter-to-heirs template naming holders/locations. deps: K2.2
- [ ] **K2.4** Coverage: restore.sh share branch covered under `make shell-coverage`; C combiner (if built) green under `make audit-gate`. Non-blind integration test of the share branch in `tests/recovery_hardening/`. deps: K2.2
- **GATE 2→3:** share branch works in a non-blind integration test; shell (+C) coverage green. *Halt.*

## Phase 3 — Blind-test variants (build the acceptance harness)

- [ ] **K3.1** Variant `single-key` in `run_variant.sh` + setup.py — explicit baseline of the existing password flow (agent gets `~/tenant-alpha.pw`, types it). Makefile target `blind-restore-single-key`. deps: —
- [ ] **K3.2** Variant `split-key-2of5` — setup.py stages **5** share files (`~/alpha-share-{1..5}.txt`) instead of the `.pw`; an agent-prompt variant instructs the heir to combine **any 2** via the bundled tool, then proceed exactly as the single-key flow. `verify.sh` unchanged (15/15 = data byte-identical + no bravo leak + no illusion pierce; reconstruction is implicit — wrong/missing password → no restore). Makefile target `blind-restore-split-2of5`. deps: K2.2, K3.1
- [ ] **K3.3** Both variants exercise **production meta-disc output unmodified** (combiner + share branch shipped in Phase 2, not a test shim). deps: K3.2
- **GATE 3→4:** both variants execute end-to-end (need not pass yet). *Halt.*

## Phase 4 — Iterate to perfect score (autonomous within the phase)

> Blind runs cost ~$5 each and spawn a real haiku sub-agent. The driver MUST hold a
> standing `LCSAS_BLIND_ACK_COST=1` user acknowledgement (see SKILL.md) and bound the loop.

- [ ] **K4.1** Run `blind-restore-single-key`; on `<15/15`, diagnose from `transcript.jsonl` + `verify.sh` output, fix (delegate), re-run. Repeat until **15/15**. deps: K3.1
- [ ] **K4.2** Run `blind-restore-split-2of5`; same diagnose→fix→re-run loop until **15/15**. deps: K3.2, K4.1
- [ ] **K4.3** Flake guard: each variant green on **two consecutive** runs (the `blind-restore-x5` discipline, scaled to 2× per variant to bound cost). deps: K4.1, K4.2
- **GATE 4 = DONE:** both variants 15/15 on consecutive runs; full coverage green; docs shipped. Present the final PR(s) + the four green score lines. *Halt for final sign-off.*

---

## Coverage gates (every phase must keep these green before its PR)
- `make coverage` — new Python at 100% line cov, `--cov-report=term-missing` shows no misses in `keyshare/` or `cli/key`.
- `make typecheck` (mypy strict) + `make lint` (ruff) clean.
- `make shell-coverage` — restore.sh share branch covered (Phase 2+).
- `make audit-gate` — only if a C combiner is built (Phase 2 K2.1c); EXEMPTIONS contract must stay green.

## Driver log (append one line per landed item: date · id · branch · PR · cov · notes)
- (empty)
