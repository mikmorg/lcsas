# AUDIT_FINDINGS.md

## Status

Audit complete through Phase 5.  See tracker issue #166.

| Phase | Description | Result |
|-------|-------------|--------|
| 0 | Manual code audit — find bugs | 5 bugs found (BUG-1 through BUG-5) |
| 1 | gcov coverage baseline | 78.5% overall; see breakdown below |
| 2 | ASan + UBSan + LSan | 0 findings on full test suite |
| 3 | LibFuzzer harnesses (5 targets, 60s each) | 0 crashes |
| 4 | Bug fixes (BUG-1 through BUG-5) | All resolved |
| 5 | audit-gate composite target + CI | Delivered |
| 6 | Fault-injection malloc harness | **Deferred** — see issue #165 |

## Coverage history

| Phase | Date | Overall | Notes |
|-------|------|---------|-------|
| Phase 1 baseline | 2026-05-21 | 78.5% | After arena.c removal + zstd_dec to 100% |
| Phase 2 fault-inject sweep | 2026-05-21 | 80.8% | LD_PRELOAD malloc fault sweep on test_catalog |
| Phase 5 (catalog+main+json_q+disc_locator) | 2026-05-21 | **87.1%** | Assertion-pinned unit tests |

## Phase 5 per-file coverage (gcovr, 2026-05-21)

| File | Line% |
|------|-------|
| aes.c | 100.0% |
| b64.c | 95.7% |
| catalog.c | 98.0% (was 72.3%) |
| disc_locator.c | 81.6% (was 60.3%) |
| hex.c | 100.0% |
| json_q.c | 97.1% (was 89.1%) |
| lcsas_io.c | 90.3% |
| main.c | 88.1% (was 79.3%) |
| path.c | 98.3% |
| pbkdf2.c | 94.7% |
| poly1305.c | 95.0% |
| repo.c | 75.9% |
| scrypt.c | 98.0% |
| sha256.c | 100.0% |
| tree.c | 70.3% |
| zstd_dec.c | 100.0% (was 63.6%) |
| **Overall** | **87.1%** (was 78.5%) |

Note: `arena.c` (was 0%) was deleted in PR #175 — dead code with no callers.

## Remaining gaps and rationale

| File | Current | To reach 95% requires |
|------|---------|----------------------|
| repo.c | 75.9% | Python-generated valid encrypted key/index fixtures fed to a new test_repo.c that calls lcsas_repo_load_key_file with real ciphertexts. |
| tree.c | 70.3% | Same fixture work plus a valid tree-blob structure. End-to-end blind-restore covers this but at ~$5/run. |
| disc_locator.c | 81.6% | Tests for drain_disc edge cases (fs-full, missing source) and the interactive prompt loop. Most remaining lines require user-namespace mount fixtures. |
| main.c | 88.1% | Remaining branches are the snapshot-walking happy paths (need fixture). |

**Aspirational target: 95%.** See `recovery/docs/AUDIT.md` for the path forward.

---

---

## Findings

| id    | file:line                       | kind                                    | severity | found-by       | status                      |
|-------|---------------------------------|-----------------------------------------|----------|----------------|-----------------------------|
| BUG-1 | json_q.c:290                    | stack overflow via unbounded buffer write | critical | Phase-0 audit  | fixed in PR #168            |
| BUG-2 | repo.c:179 (lcsas_repo_load_keys_dir) | silent key cap at 256              | medium   | Phase-0 audit  | fixed in PR #169            |
| BUG-3 | repo.c:429 (lcsas_repo_load_index)    | silent index cap at 2048           | medium   | Phase-0 audit  | fixed in PR #169            |
| BUG-4 | repo.c:480 (lcsas_repo_load_index)    | silent supersedes cap at 8192 (fail-loud) | medium | Phase-0 audit | fixed in PR #169           |
| BUG-5 | disc_locator.c (5 sites)        | silent path-too-long drops at 5 sites   | low      | Phase-0 audit  | fixed in PR #169            |
