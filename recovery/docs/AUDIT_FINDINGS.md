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

## Phase 1 coverage baseline (gcovr, 2026-05-21)

| File | Line% |
|------|-------|
| aes.c | 100.0% |
| arena.c | 0.0% (dead code — no callers; excluded from threshold) |
| b64.c | 95.7% |
| catalog.c | 72.3% |
| disc_locator.c | 60.3% |
| hex.c | 100.0% |
| json_q.c | 89.1% |
| lcsas_io.c | 90.3% |
| main.c | 79.3% |
| path.c | 98.3% |
| pbkdf2.c | 94.7% |
| poly1305.c | 95.0% |
| repo.c | 75.9% |
| scrypt.c | 98.0% |
| sha256.c | 100.0% |
| tree.c | 70.3% |
| zstd_dec.c | 63.6% |
| **Overall** | **78.5%** |

**Aspirational target:** 95% (after petabyte fixture improvements and additional test coverage).  Blocked by malloc-failure branches requiring fault injection (issue #165).

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
