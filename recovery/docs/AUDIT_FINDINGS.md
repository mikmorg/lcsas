# AUDIT_FINDINGS.md

## Status

Audit in progress — phases 1-5 tracked in issue #165.

---

## Findings

| id    | file:line                       | kind                                    | severity | found-by       | status                      |
|-------|---------------------------------|-----------------------------------------|----------|----------------|-----------------------------|
| BUG-1 | json_q.c:290                    | stack overflow via unbounded buffer write | critical | Phase-0 audit  | fixed in PR #168            |
| BUG-2 | repo.c:179 (lcsas_repo_load_keys_dir) | silent key cap at 256              | medium   | Phase-0 audit  | fixed in this PR            |
| BUG-3 | repo.c:429 (lcsas_repo_load_index)    | silent index cap at 2048           | medium   | Phase-0 audit  | fixed in this PR            |
| BUG-4 | repo.c:480 (lcsas_repo_load_index)    | silent supersedes cap at 8192 (fail-loud) | medium | Phase-0 audit | fixed in this PR           |
| BUG-5 | disc_locator.c (5 sites)        | silent path-too-long drops at 5 sites   | low      | Phase-0 audit  | fixed in this PR            |
