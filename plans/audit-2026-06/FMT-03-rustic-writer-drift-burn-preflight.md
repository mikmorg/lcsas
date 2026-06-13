# FMT-03: Burn-time gate against rustic writer / pinned-reader format drift

**Priority:** P1 · **Severity:** high · **Dimension:** format-durability · **Audit status:** confirmed (high confidence) · **Ledger:** partially tracked: docs/SURVIVABILITY.md §5 covers "rustic project abandoned → format spec on disc", not the writer silently outrunning the pinned readers; the drift-gate gap is in no ledger
**Suggested GH issue title:** Add burn preflight proving mirror repo is readable by pinned recovery readers

## Problem

The burn pipeline treats pack files as opaque bytes. Neither the mirror scanner nor the burn
orchestrator ever checks the repository format version, KDF parameters, or compression mode;
the orchestrator's preflight checks only that xorriso/dvdisaster binaries exist at minimum
versions. The tier-1 C reader never parses the repo config either — it assumes restic v1/v2
crypto and sniffs zstd by magic bytes. Meanwhile the *writer* is whatever rustic the operator
has installed on the NAS; only the tier-2 fallback binary is pinned (v0.11.2).

restic/rustic have bumped the repository format before (v1→v2 added compression). If a future
rustic migrates the mirror to a v3 format (different MAC, KDF, or compression framing), every
disc burned afterward is undecodable by all three recovery tiers — while the operator's
routine `lcsas restore exec` test-restores keep passing, because they use the same live rustic
that wrote the data. The drift is actively masked. The only end-to-end catch is the blind-restore
drill: manual, cost-gated (`LCSAS_BLIND_ACK_COST=1`), and absent from CI (cdemu can't run on
hosted runners). An heir could inherit years of discs whose packs no shipped reader can open.

## Evidence

Re-checked against current code:

- `src/lcsas/burn/orchestrator.py:233-240` — preflight checks only
  `check_binary_version(xorriso, (1,4,0))` and `(dvdisaster, (0,79,0))`; zero repo-format
  checks anywhere in the file. `src/lcsas/packs/scanner.py` — no `version`/`config` reference.
- `recovery/src/lcsas-restore/repo.c:24` — `ZSTD_MAGIC` sniffing; `:927-931` — inline pack
  blobs detected as compressed by magic only; `grep -n config repo.c` → zero matches (the repo
  config file is never parsed).
- `recovery/docs/FORMAT.txt:40-56` — documents only v1/v2 semantics;
  `docs/RESTIC_FORMAT_SPEC.md:308` — "`version`: repository format version (1 or 2)".
- `src/lcsas/restore/restic_fallback.py:1202` — tier-3 reads `config.version` only into a
  stats dict ("unknown" fallback); no gate.
- `.github/workflows/test.yml:68-73` — cdemu NOT installed in CI; blind-restore suite local-only.
  `Makefile:80-94` — blind-restore is cost-gated manual.
- Partial existing cover: `tests/recovery_hardening/test_tier1_vs_tier2_differential.py` does
  real `rustic init`+`backup` → tier-1 restore round-trips, but with the pinned 0.11.2 writer —
  it validates the reader, not live-writer drift.

## Fix design

Two layers: an operator-side hard gate at burn time (the effective fix) and a CI canary for
early warning.

### 1. Burn preflight: prove the bytes are readable by the shipped stack

New module `src/lcsas/burn/format_preflight.py`:

```python
SUPPORTED_REPO_VERSIONS = (1, 2)   # frozen contract of tiers 1/2/3

def check_repo_recoverable(mirror_path: Path, password_file: Path) -> None:
    """Raise FormatDriftError unless the mirror decodes with the pinned readers.

    Proof, using PurePythonRestorer (tier-3 code, stdlib-only, in-process):
      1. decrypt + parse `config`; assert version in SUPPORTED_REPO_VERSIONS
      2. load one index file; 3. fetch + MAC-verify + hash-verify one blob.
    """
```

Why PurePythonRestorer rather than a tier-1 `--check-repo` mode: it is already importable in
the operator install (the recovery C binaries are not shipped with `pip install lcsas`), and
it exercises the same v1/v2 assumptions tier-1 hard-codes. (A tier-1 `--check-repo` mode can
be added later for belt-and-braces; not required to close this.)

Call it from `BurnOrchestrator` immediately after the existing binary-version preflight
(`orchestrator.py:233-240`), once per repo participating in the session, using each
`RepositoryConfig.password_file` (`config/settings.py:72`). Edge cases:

- `password_file` not configured for a repo → fail the preflight by default with: *"repo 'X':
  no password_file configured — cannot prove discs will be restorable. Set password_file or
  set `allow_unverified_repo_format = true` in lcsas.toml to burn anyway (NOT recommended)."*
- Unknown config version → *"repository 'X' is format version 3; the pinned recovery readers
  support versions 1-2. Discs burned now would be unreadable by every bundled restore tier.
  Refusing to burn. See docs/RESTIC_FORMAT_SPEC.md."*
- Unknown compression type byte in the sampled index/blob → same refusal wording.
- Empty repo (no index/blobs yet) → version check only, log that the decode proof was skipped.

Also update `FORMAT.txt`/`RESTIC_FORMAT_SPEC.md` to state versions 1-2 are a *frozen, gated*
contract. No catalog/schema change; the gate runs before staging, so no migration concerns.

### 2. CI canary against the LATEST upstream rustic (per verifier refinement)

The originally proposed CI job (pinned-rustic write → tier-1 read) cannot detect live-writer
drift — it pins the writer by construction and duplicates the differential suite. Instead:
new scheduled workflow `.github/workflows/format-canary.yml` (weekly): download the *latest*
rustic release (deliberately unpinned), `init`+`backup` a fixture tree, restore with the
tier-1 binary AND PurePythonRestorer, byte-compare. Failure = upstream format bump landed;
gives years of warning before any operator upgrades their NAS rustic.

## Tests & gates

1. `tests/unit/test_burn_preflight_repo_version.py` — always-on (`make test-unit`, CI):
   - synthetic repo fixture with config `version: 3` → orchestrator aborts before ISO creation
     with the format-drift message;
   - fixture with unknown compression byte (0x03) in a sampled file → aborts;
   - v1 and v2 fixtures → preflight passes, decode proof reaches the blob check;
   - missing password_file → aborts unless `allow_unverified_repo_format`.
   Build fixtures with the existing pure-Python writer helpers used by restic_fallback tests
   (no rustic needed).
2. `tests/integration/test_burn_preflight_live.py` — integration (rustic on PATH): real
   `rustic init`+`backup` mirror passes the preflight end-to-end through
   `BurnOrchestrator.run` up to staging.
3. `.github/workflows/format-canary.yml` — weekly scheduled, latest-rustic round-trip vs
   tier-1 + tier-3 (see design §2). Failure pings the repo (issue auto-filed or job failure
   notification).
4. Existing `test_tier1_vs_tier2_differential.py` stays as the pinned-reader oracle; no change.

## Acceptance criteria

- [ ] `lcsas burn` against a v3-config mirror exits non-zero before any ISO is created, with
      the refusal message naming the repo and the supported versions.
- [ ] `lcsas burn` against a healthy v2 mirror logs the decode proof (config+index+blob) and
      proceeds.
- [ ] Repo with no password_file refuses to burn unless the config override is set.
- [ ] `format-canary.yml` runs green on schedule with today's latest rustic.
- [ ] Unit tests above pass in CI on every PR.

## Dependencies & related plans

- Independent of other FMT plans; can land any time. Land before the next real burn alongside
  the P0 BURN family (it shares the "catalog/pipeline must not lie" theme: BURN "mirror
  silent-skip", "no content hashing").
- GATE: "blind-restore Linux-only/cost-gated" and "recovery-hardening never runs in CI" — the
  canary workflow complements, not replaces, those.
- RST: tier-3 zstd availability plan — the preflight's decode proof via PurePythonRestorer
  will also fail loud when zstandard is missing for a v2 repo, surfacing that finding earlier.

## Effort

**2.5 focused days**: 1d preflight module + orchestrator wiring + messages, 1d unit/integration
fixtures, 0.5d canary workflow. No special environment (rustic already in CI; tier-1 binary
already built by `make -C recovery`).

---
**Implemented:** 2026-06-13. As planned, with these notes: the burn gate runs in BOTH
`stage()` (session path) and `prepare()` (legacy single-volume path) via a new
`BurnOrchestrator._format_preflight`, before any side effect. The DB repo is joined to its
`RepositoryConfig` by mirror_path (authoritative: `lcsas repo add` title-cases the DB `name`,
which differs from the config key), falling back to name. New config flag
`allow_unverified_repo_format` (deviation: plan named it inline in the message; added as a real
`[defaults]` key). Pre-existing burn/session unit fixtures use fake non-restic mirrors with no
password_file, so they opt into the override (`allow_unverified_repo_format=True`) — the gate
correctly refused them otherwise. Canary test is opt-in (`LCSAS_FORMAT_CANARY=1`) under
`tests/recovery_hardening/`, round-trips latest rustic through tier-1 AND tier-3; workflow files
an issue (label `format-drift`) on failure. All three test layers run green locally against the
installed rustic + freshly built tier-1 binary.
