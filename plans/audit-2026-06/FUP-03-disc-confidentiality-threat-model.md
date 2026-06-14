# FUP-03: Disc confidentiality — threat model for a stolen disc, passphrase reality, no-rotation story

**Priority:** P2 · **Severity:** medium · **Dimension:** follow-up: disc-confidentiality-threat-model · **Audit status:** flagged by completeness critic (citations re-verified against code 2026-06-10) · **Ledger:** untracked (named only in DEEP_AUDIT Appendix C / P2 roadmap)
**Suggested GH issue title:** Document the stolen-disc threat model; fix key-on-disc wording and passphrase guidance

## Problem

No audited dimension ever asked what an adversary holding any *single* disc gets. The
answer today: the complete SQLite catalog in plaintext (every snapshot's hostname,
backup paths, tags, description; every repository's name and mirror path; every
location name and the full volume-copies map of where every other copy lives; the
volume_events audit trail), plus every repo's rustic `keys/` directory — the
scrypt-wrapped master keys, whose key files also carry plaintext username/hostname —
plus the owner's `key_storage_hints` printed verbatim on every disc ("USB copy in safe
deposit box #1234" is the shipped example). Both data discs and the meta disc carry all
of this, for **every tenant** in a multi-tenant archive.

Much of this is **by design**: the heir needs the wrapped keys and the catalog on every
disc, or the holographic single-disc-recovery property dies. The actual gaps are
informed consent and posture, not the artifact set. (1) No document states the threat
model, so the owner choosing escrow locations cannot weigh it. (2) The only barrier
between a disc thief and the data is the repo password — unlimited offline brute-force,
forever — yet no passphrase-strength guidance exists anywhere, and the KDF cost
(scrypt N=2^15, r=8, p=1 ≈ tens of milliseconds per guess) is modest against decades of
hardware improvement. (3) Discs are immutable: changing the password later does
*nothing* for already-burned discs (their old key files stay valid forever), and no doc
says so. (4) Two heir-facing files actively claim "The key is NOT on any disc (for
security)" — true only of the password, and falsely comforting given (2).

## Evidence

Re-checked 2026-06-10:

- `src/lcsas/staging/metadata.py:101-115` — `HolographicInjector.inject_metadata`
  copies `index/`, `snapshots/`, `keys/` (`METADATA_SUBDIRS`,
  `utils/pack_layout.py:24`) plus `config` for every repo onto every data disc;
  `inject_catalog` (:117-120) copies the complete live `catalog.db` verbatim.
- `src/lcsas/meta/builder.py:2235-2278` — `_bundle_metadata` copies `config`, `keys`,
  `index`, `snapshots` for **all** repos onto the meta disc too.
- `src/lcsas/db/schema.py:73-83` — snapshots table: plaintext `hostname`, `paths`,
  `tags`, `description` (populated via `rustic/parser.py:74-81` →
  `db/snapshots.py:upsert_snapshot`); `schema.py:86-90` locations (`name`,
  `description`); `:93-108` volume_copies (volume↔location map); `:134-147`
  volume_events audit trail; `:41-47` repositories (`name`, `mirror_path`).
- `docs/RESTIC_FORMAT_SPEC.md:55-82` — key files: scrypt `N=32768, r=8, p=1`, and the
  key-file JSON carries plaintext `username` and `hostname`.
- `src/lcsas/staging/metadata.py:396` — START_HERE.txt: "The key is NOT on any disc
  (for security)."; `:228` — RESTORE_INSTRUCTIONS.txt: "Your encryption key file (NOT
  stored on any disc for security)".
- `src/lcsas/staging/metadata.py:330-333, 484-488` — `key_storage_hints` printed
  verbatim into START_HERE.txt and KEY_INFO.txt on every disc;
  `config/settings.py:151` ships the example "Paper copy in the home safe; USB copy in
  safe deposit box #1234".
- Docs sweep: `grep -rni "adversary|attacker|brute|stolen" docs/ recovery/docs/` —
  no threat model anywhere; the sole posture statement is `docs/ESTATE_PLANNING.md:61`
  ("the dominant risk for a backup is *loss*, not theft"), asserted without analysis,
  and no passphrase-strength guidance exists in any owner- or heir-facing doc.

## Fix design

### Part 1 — immediate mitigations (docs + wording; no artifact changes)

**1. Write the threat model** — new `docs/DISC_CONFIDENTIALITY.md`, linked from
ESTATE_PLANNING.md, SURVIVABILITY.md, and `recovery/docs/TIERS.txt`. Contents: (a) the
exact plaintext inventory above ("what someone holding one disc learns"); (b) what they
do NOT get (file contents — AES-256, sound per the audited crypto); (c) the single
defense: password entropy vs unlimited offline scrypt guessing, with a small table
(e.g. 40-bit password: hours-to-days on 2026 GPU rigs; ≥90 bits / 7+ diceware words:
out of reach through 2060s under any plausible scaling); (d) **immutability = no
rotation**: a password change protects only future burns; the only revocation for a
leaked password is physical destruction of every copy of every disc; (e) escrow
guidance: treat every disc as carrying this inventory when choosing offsite/escrow
locations; never store a key-share card *with* a disc. Explicitly state why keys stay
on the discs (restorability beats confidentiality for this system's goal) so the
tradeoff is a documented decision, not an accident.

**2. Passphrase-strength guidance at the point of decision.** Add a "Choose the
password like it will be brute-forced" checklist item to `docs/ESTATE_PLANNING.md`
(§ key options) and to the `lcsas key split` section of `docs/KEY_SHARE_FORMAT.md`,
recommending generated diceware ≥7 words and stating the scrypt parameters being bet
against. Optional code assist (cheap, do it): `cmd_key_split` warns (not blocks) when
the password file is under 16 chars / estimated <60 bits.

**3. Fix the false-comfort wording** in `staging/metadata.py` (both generated files):

> "Your password is NOT written on any disc. The discs DO carry password-locked key
> files — that is what your password opens. Anyone who holds a disc can try password
> guesses against it forever, so the password must be long and random."

Keep the heir-friendly register; this also stops contradicting `inject_metadata`.

**4. `key_storage_hints` informed consent.** In ESTATE_PLANNING.md and the
`settings.py:151` example, state that hints are printed on every disc and must locate
the key for an heir without handing it to a thief — replace the shipped example with
"Sealed envelope with the family attorney (see my will)" style wording.

**Catalog/schema note:** no schema-v5 or artifact change in Part 1. Any future
sanitization option (charter (d)) affects newly burned discs only — every already-
burned disc carries the full plaintext catalog forever, and the threat-model doc must
say exactly that.

### Part 2 — follow-up audit charter

Scope: confidentiality only (restorability findings live elsewhere). Questions:

- (a) **Multi-tenant leakage:** every tenant's keys + backup topology ride on every
  other tenant's discs. Is per-tenant volume separation (binpack constraint) worth
  offering? What does KEY_INFO's "one key likely works for all repositories"
  (metadata.py:490-492) imply when tenants are different people?
- (b) **Brute-force economics, measured:** read the KDF params the *live* rustic writer
  actually records in fresh key files (calibration may exceed N=2^15), benchmark
  guesses/sec on current hardware, and produce the entropy table for mitigation 1(c)
  from data, not folklore.
- (c) **Shamir interplay:** verify K−1 cards + any disc yields nothing; confirm heir/
  estate guidance never co-locates a card with a disc; check whether a disc + the
  on-disc `KEY_SHARE_FORMAT.md` weakens any share-handling assumption.
- (d) **Catalog sanitization tradeoff:** would a redacted on-disc catalog (drop
  locations/volume_events detail, blank snapshot descriptions) break catalog rebuild
  (`db/rebuild.py`), restore planning, or FMA-06/08 work? Decide deliberately; default
  expectation is "keep full catalog, document it" — restorability is the system's bar.
- (e) **Key files' plaintext `username`/`hostname`** — cosmetic scrub at inject time
  possible? (Likely no: key file bytes are content-addressed by the repo. Confirm and
  document.)

Deliverable: DISC_CONFIDENTIALITY.md gains a "verified numbers" section; new findings
filed as plans. Expected 1-1.5 focused days.

## Tests & gates

- `tests/unit/test_staging.py::test_start_here_and_key_info_password_wording` — render
  via `write_start_here`/`write_key_info`/`write_restore_instructions`; assert the old
  string "NOT on any disc (for security)" is gone and the new wording (password-locked
  key files + guessing warning) is present in all three files. Always-on,
  `make test-unit`.
- `tests/unit/test_cli_handlers.py::test_key_split_warns_on_short_password` — short
  password file ⇒ warning text on stderr, exit code unchanged. Always-on.
- Docs presence gate: extend the docs-vs-reality contract test (UX phantom-flag plan)
  to also assert `docs/DISC_CONFIDENTIALITY.md` exists and is referenced from
  ESTATE_PLANNING.md — keeps the threat model from silently rotting out of the doc set.
- Charter (b) benchmark lands as a script under `tools/` with its output table pasted
  into the doc (not a CI gate; hardware-dependent).

## Acceptance criteria

- [ ] `docs/DISC_CONFIDENTIALITY.md` exists covering: plaintext inventory, brute-force
      table, no-rotation/immutability statement, escrow + card-placement guidance, and
      the explicit keys-on-disc-by-design rationale.
- [ ] Generated START_HERE.txt / KEY_INFO.txt / RESTORE_INSTRUCTIONS.txt no longer
      claim the key is "NOT on any disc (for security)"; new wording asserted by unit
      test in `make test-unit`.
- [ ] ESTATE_PLANNING.md and KEY_SHARE_FORMAT.md carry passphrase-strength guidance;
      `settings.py` example hint no longer names a findable location.
- [ ] `lcsas key split` warns on a weak password file.
- [ ] Charter questions (a)-(e) answered in writing; measured KDF/benchmark numbers in
      the doc; any new findings filed as plans.

## Dependencies & related plans

- UX "phantom-flag on-disc docs + docs-vs-reality contract gate" (UX-02) — the wording
  fixes here ride the same generated-docs surface and the same contract test; land
  after or with it.
- KEY "key split never verifies secret" (KEY-03) and KEY "recovery card template"
  (KEY-09) — passphrase guidance and card-placement rules belong beside their output.
- FMA "rebuild resurrects volumes" (FMA-06) — charter (d) must not break rebuild.
- FUP-01 charter item (c) (receipts/labeling) — location names on printed artifacts
  share the same map-of-copies concern.

## Effort

Mitigations: 1 day (docs + wording + two unit tests). Charter audit: 1-1.5 days
(includes the rustic key-file KDF measurement and a GPU-rate literature pass; no
special hardware beyond this VM and one fresh rustic repo). Total ≈ 2.5 days.

---
**Implemented:** 2026-06-14. Part 1 only, as planned: docs/DISC_CONFIDENTIALITY.md written + linked from ESTATE_PLANNING/SURVIVABILITY/TIERS.txt; START_HERE/KEY_INFO/RESTORE_INSTRUCTIONS wording fixed; passphrase guidance added to ESTATE_PLANNING + KEY_SHARE_FORMAT; settings.py example hint de-located; cmd_key_split weak-password warning (warn, never block); 4 new tests + docs-presence gate. Part 2 charter remains a document.
