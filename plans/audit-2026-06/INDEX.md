# Remediation plan index — deep audit 2026-06

One implementation-ready plan was written per confirmed audit finding (see
[`DEEP_AUDIT_2026-06.md`](../../DEEP_AUDIT_2026-06.md)), plus three follow-up audit
areas (FUP) and the Windows-e2e CI scaffolding (INFRA). **82 plans total.**

## Status summary (audited 2026-06-17)

All 82 plans have **landed in master** — each carries a `STATUS: RESOLVED` stamp at the
top of its file citing the resolving commit and a guarding test.

- **82 resolved** -> archived to [`../done/`](../done/INDEX.md).
- **0 partial.**
- **0 open.**

Status was determined by cross-referencing `git log` (commit subjects tag the plan ID,
e.g. `[FMA-09]`) against the implementing code and tests; see each plan's stamp for the
specific commit + test evidence, and [`../done/INDEX.md`](../done/INDEX.md) for the full
ID -> commit table.

## Remaining partial / open plans

None. Nothing is outstanding from this audit.

## Plans kept in this directory (resolved, but referenced by path)

Two RESOLVED plans stay here rather than under `plans/done/` because shipped artifacts
link to them by path; moving them would break those references:

| ID | Plan | Resolving commit | Referenced by |
|----|------|------------------|---------------|
| BOOT-07 | [Remove the Alpine live stack](BOOT-07-remove-alpine-live-stack.md) | `309ee99` | `tests/recovery_hardening/test_no_unpinned_boot_artifacts.py` |
| FUP-03 | [Disc-confidentiality threat model](FUP-03-disc-confidentiality-threat-model.md) | `f42775f` | `docs/DISC_CONFIDENTIALITY.md` |

## Archive

The remaining 80 resolved plans were moved to [`../done/`](../done/INDEX.md), which holds
the complete ID -> resolving-commit table for all 82.
