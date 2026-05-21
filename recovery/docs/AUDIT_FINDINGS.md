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
| Phase 5 (catalog+main+json_q+disc_locator) | 2026-05-21 | 87.1% | Assertion-pinned unit tests |
| Phase 7 (test_repo with encrypted fixture) | 2026-05-21 | 90.7% | gen_fixture.py + test_repo.c covering full repo + tree pipeline |
| Phase 8 (fixture-driven CLI + disc_locator extensions) | 2026-05-21 | **92.4%** | fixture-based CLI tests (main.c) + catalog/drain/cache/interactive tests (disc_locator); fault-inject sweep extended to lcsas-restore for prod-robustness verification |

## Phase 8 per-file coverage (gcovr, 2026-05-21)

| File | Line% | Δ from baseline |
|------|-------|-----------------|
| aes.c | 100.0% | — |
| b64.c | 95.7% | — |
| catalog.c | 98.0% | +25.7 (Phase 5) |
| disc_locator.c | **88.5%** | +28.2 (Phase 5 + 8) |
| hex.c | 100.0% | — |
| json_q.c | 97.1% | +8.0 (Phase 5) |
| lcsas_io.c | 90.3% | — |
| main.c | **94.8%** | +15.5 (Phase 5 + 8) |
| path.c | 98.3% | — |
| pbkdf2.c | 94.7% | — |
| poly1305.c | 95.0% | — |
| repo.c | 85.9% | +10.0 (Phase 7) |
| scrypt.c | 98.0% | — |
| sha256.c | 100.0% | — |
| tree.c | 89.2% | +18.9 (Phase 7) |
| zstd_dec.c | 100.0% | +36.4 (Phase 1) |
| **Overall** | **92.4%** | **+13.9** |

Note: `arena.c` (was 0%) was deleted in PR #175 — dead code with no callers.

## Remaining gaps (Phase 8+)

| File | Current | To reach 95% requires |
|------|---------|----------------------|
| disc_locator.c | 81.6% | Drain edge cases (fs-full, missing source), interactive prompt loop, user-namespace mount fixtures for the chroot-style branches. |
| repo.c | 85.9% | Malloc-failure paths in `read_blob` and a multi-blob compressed pack (lines 790-845). The fault-injection harness in #165 covers some; the rest need a compressed-blob fixture. |
| tree.c | 89.2% | Remaining lines are corrupted-blob-content paths (malformed JSON tokens after decryption) — would need an attacker-crafted fixture, or a fuzz target on `lcsas_tree_restore` directly. |
| main.c | 88.1% | Snapshot-walking error branches (snapshot blob fetch failure mid-restore). |

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
