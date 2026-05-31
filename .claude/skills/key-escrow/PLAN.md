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
- [x] **K0.4** `docs/KEY_SHARE_FORMAT.md` — SLIP-0039 layout, algorithm + re-implementation guidance, LCSAS tie-in, 45-vector conformance pointer. Auto-bundled on the meta-volume via `_DOC_ITEMS=("docs",...)` (no code change needed). deps: K0.2
- **GATE 0→1:** ✅ format locked, primitive round-trips + tamper-detects, 100% cov, spec written + bundled. *Awaiting "merge #N".*

## Phase 1 — CLI & share artifacts

- [x] **K1.1** `lcsas key split` (default 2-of-5): reads repo password, writes per-share mnemonic + plain-language card files (all mode 0600), security warning to stderr, password never printed. deps: K0.2
- [x] **K1.2** `lcsas key combine` → reconstruct from ≥K shares (files or stdin) to stdout/`--out`; `<K`/corrupted/foreign → clear error, **exit non-zero**. deps: K0.2
- [x] **K1.3** Config `key_threshold` / `key_shares` (default 2/5) on `LCSASConfig` + TOML parse. deps: K1.1
- [x] **K1.4** CLI tests (round-trip, under-threshold, corrupted, foreign, perms, config override) + codec tests — **100% on new code (codec 18/18; key handlers fully covered)**. deps: K1.1, K1.2
- **GATE 1→2:** ✅ real password round-trips end-to-end via the CLI; coverage green. *(Also fixed a pre-existing bug: `python -m lcsas` swallowed exit codes — `__main__.py` now `sys.exit(main())`, with a regression test.)* *Awaiting "merge #N".*

## Phase 2 — Recovery-path integration (zero-dep, production)

- [x] **K2.1** Standalone `src/lcsas/meta/keyshare_combine.py` shipped at meta-volume root + `lcsas.keyshare` package (incl. wordlist) bundled via `bundle_python_package`. Imports ONLY keyshare, so reconstruction survives a broken LCSAS. MANIFEST: combiner/wordlist live in `src/` and are copied at build time like `standalone_restorer.py` (the `recovery/`-rooted MANIFEST intentionally doesn't pin them; git-pinned + 45-vector guarded; documented in `_bundle_keyshare_combiner`). C combiner deferred (documented future enhancement). deps: K0.2, K0.4
- [x] **K2.2** **Design change (lead, per standing authorization): reconstruct-then-restore PRE-STEP, restore.sh byte-for-byte UNCHANGED** (lowest risk to the recovery-critical script; single-key path provably untouched). Heir runs `python3 keyshare_combine.py <K cards>` → password → normal `restore.sh` `Password:` flow. deps: K2.1
- [x] **K2.3** Heir docs gated on `config.key_split`: `KEY_INFO.txt`/`START_HERE` (`staging/metadata.py`) + `docs/ESTATE_PLANNING.md` show the two-step share recovery with K/N; single-key archives show none of it. deps: K2.2
- [x] **K2.4** 100% cov on `keyshare_combine.py` (46/46) + bundler/builder/settings/metadata new lines; independent clean-machine (`env -i`, bundled-layout) reconstruction verified by lead; restore.sh unchanged so `make shell-coverage` unaffected. deps: K2.2
- **GATE 2→3:** ✅ standalone combiner reconstructs on a clean machine; coverage green; restore.sh untouched. *(Self-merge per authorization.)*

## Phase 3 — Blind-test variants (build the acceptance harness)

- [x] **K3.1** Variant `single-key` — explicit baseline alias of `default` (agent gets `~/tenant-alpha.pw`). Makefile `blind-restore-single-key`. deps: —
- [x] **K3.2** Variant `split-key-2of5` — setup.py stages **5** share cards (no plaintext pw, split via the real keyshare module); `agent_prompt_split.txt` has the heir reconstruct any 2 via `/mnt/keyshare_combine.py` then proceed as single-key; `verify.sh` unchanged. Makefile `blind-restore-split-2of5`. deps: K2.2, K3.1
- [x] **K3.3** Both variants use **production meta-disc output unmodified** (the Phase 2 combiner, not a shim) — proven by the accidental live runs below. deps: K3.2
- **GATE 3→4:** ✅ both variants execute end-to-end. During Phase 3 verification a `make -n` footgun ($(MAKE) recipe lines run under -n) accidentally launched **both** blind variants — **both scored 15/15** (incl. no-bravo-leak on split-key-2of5). Phase 4 re-runs deliberately for the consecutive-greens flake guard.

## Phase 4 — Iterate to perfect score (autonomous within the phase)

> Blind runs cost ~$5 each and spawn a real haiku sub-agent. The driver MUST hold a
> standing `LCSAS_BLIND_ACK_COST=1` user acknowledgement (see SKILL.md) and bound the loop.

- [x] **K4.1** `blind-restore-single-key` → **15/15** (deliberate, committed code). deps: K3.1
- [x] **K4.2** `blind-restore-split-2of5` → **15/15** (heir reconstructed the password from 2 of 5 share cards via the production combiner, then restored). deps: K3.2, K4.1
- [x] **K4.3** Flake guard met: each variant green on **two consecutive** runs. deps: K4.1, K4.2
- **GATE 4 = DONE ✅:** all four blind runs 15/15 (single-key ×2, split-key-2of5 ×2), zero FAIL criteria; full coverage green; docs shipped. **/key-escrow COMPLETE.**

---

## Phase 5 — C combiner (make split-key fully python-free)

Goal: a tier-1-grade C89 `lcsas-keyshare` combiner so split-key reconstruction needs no python3 — closing the documented trade-off. Oracle = the 45 official SLIP-0039 vectors AND byte-match vs the Python `recover_secret`+`decode_master_secret`.

- [x] **C5.1** `recovery/src/lcsas-keyshare/` C89 combiner (slip39.c/h + main.c) — RS1024/GF(256)/Feistel/HMAC-digest/codec, reuses sha256/pbkdf2/hmac/hex. Builds warning-clean (the only build warnings are pre-existing in untouched lcsas-restore/iso9660). deps: Phase 0–2
- [x] **C5.2** `recovery/tests/test_keyshare.c` (+ embedded vectors header) wired into recovery `Makefile` `all:`+`test`. **45/45 official vectors**; lead independently cross-checked C vs Python byte-exact on fresh random passwords (4/4, incl. binary/16-byte-min/40-byte); under-threshold fails loud. deps: C5.1
- [~] **C5.3** ASan/UBSan **clean** (lead re-ran). Test exercises 15 valid + 30 invalid vectors (error paths) + codec edges. Full coverage-c/EXEMPTIONS/fuzz integration of the new dir = documented follow-up (the coverage tooling is currently lcsas-restore-scoped). deps: C5.2
- [ ] **C5.4** Bundle `lcsas-keyshare` per-arch on the meta-volume (`meta/bundler`); heir docs + `agent_prompt_split.txt` prefer the C combiner (python fallback). deps: C5.1
- [ ] **C5.5** Re-validate: `split-key-2of5` blind run forcing the C combiner → 15/15 (proves python-free reconstruction end-to-end). deps: C5.4
- **GATE 5 = DONE:** C combiner passes 45 vectors + Python cross-check + audit-gate; bundled; blind split-key 15/15 via the C path.

## Coverage gates (every phase must keep these green before its PR)
- `make coverage` — new Python at 100% line cov, `--cov-report=term-missing` shows no misses in `keyshare/` or `cli/key`.
- `make typecheck` (mypy strict) + `make lint` (ruff) clean.
- `make shell-coverage` — restore.sh share branch covered (Phase 2+).
- `make audit-gate` — only if a C combiner is built (Phase 2 K2.1c); EXEMPTIONS contract must stay green.

## Driver log (append one line per landed item: date · id · branch · PR · cov · notes)
- 2026-05-31 · Phase 0 (K0.2–K0.4) · feat/keyshare-slip39-primitive · PR #311 · keyshare 100% (349/349) · 45/45 official SLIP-0039 vectors
- 2026-05-31 · Phase 1 (K1.1–K1.4) · feat/keyshare-cli · PR #312 · codec 100% + key handlers · also fixed `python -m lcsas` exit-code swallow
- 2026-05-31 · Phase 2 (K2.1–K2.4) · feat/keyshare-recovery-integration · PR #313 · combiner 100% (46/46) · clean-machine reconstruction verified
- 2026-05-31 · Phase 3 (K3.1–K3.3) · feat/keyshare-blind-variants · PR #314 · verify.sh untouched · both variants 15/15 (accidental + deliberate)
- 2026-05-31 · Phase 4 (K4.1–K4.3) · BLIND GATE · — · single-key 15/15 ×2; split-key-2of5 15/15 ×2 · /key-escrow COMPLETE
