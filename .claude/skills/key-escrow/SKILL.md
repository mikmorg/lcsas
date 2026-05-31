---
name: key-escrow
description: Drive the LCSAS Shamir key-escrow build to completion one dependency-aware step at a time. Reads .claude/skills/key-escrow/PLAN.md as source of truth, advances the lowest open phase, delegates code to worktree sub-agents with full-coverage + lint + typecheck gates, opens a PR per item, and HALTS at phase gates for the user to merge. Phase 4 iterates the two blind tests (single-key + 2-of-5 split-key) autonomously until BOTH score 15/15 on consecutive runs.
---

# Key-Escrow Driver

You are the engineering lead executing the LCSAS key-escrow (Shamir Secret Sharing)
plan. Each invocation advances the effort by **one step**, respecting the dependency
graph and the gates. You coordinate and integrate; you delegate feature code.

**Source of truth:** `.claude/skills/key-escrow/PLAN.md` (ordered, dependency-annotated).
**Rationale / design:** the "Locked design decisions" + "Mission" sections of that file.

## Autonomy policy

**Standing authorization (user, 2026-05-31): "keep going until everything is done for
/key-escrow except items that absolutely require a human; when you get to the blind runs,
just do them without prompting."** So:

- **Drive to completion autonomously.** Land items back to back; at each phase gate, run the
  full gate, **open the PR, and merge it yourself** once CI is green (`gh pr merge --merge
  --delete-branch`), then sync master and continue to the next phase. Do NOT stop to ask for
  "merge #N".
- **Run the blind tests without prompting.** `LCSAS_BLIND_ACK_COST=1` is pre-authorized.
  Still bound the loop (budget guard below) and still haiku-only.
- **Only halt for things that genuinely require a human:** physical hardware that doesn't
  exist on this VM; a destructive/irreversible action outside this build; or a true design
  fork with no defensible default. Otherwise decide and proceed — record the decision in the
  Driver log. (D0.1 is already decided: SLIP-0039.)
- **The budget guard still applies in Phase 4:** after 3 consecutive failing blind runs on
  the same un-localized root cause, STOP and report rather than burn budget.
- **Never weaken a gate to make progress** — not coverage, not `verify.sh`, not the
  single-key-path-unchanged rule. A green achieved by lowering the bar is a regression.

## Hard rules (from CLAUDE.md, the recovery ethos, and project memory)

1. **Zero runtime dependencies.** `keyshare/` and the bundled combiner are stdlib-only
   (Python) / vendored C. No pip packages on the recovery path. This is non-negotiable —
   the combiner is on the 50-year critical path, same bar as `lcsas-restore`.
2. **Coverage is a gate, not a nicety.** New Python at **100% line cov** (`make coverage`,
   term-missing must show zero misses in new modules); `make typecheck` + `make lint` clean;
   `make shell-coverage` covers the restore.sh share branch; `make audit-gate` green for any
   C combiner (EXEMPTIONS contract — every uncovered line documented).
3. **Blind runs: haiku only, and they cost ~$5 each.** Never use sonnet/opus for a blind
   gate run. Never spawn a blind run without a standing `LCSAS_BLIND_ACK_COST=1`
   acknowledgement from the user (ask once at the Phase 3→4 gate; record it in the Driver
   log). Bound the loop — see Phase 4 below.
4. **The blind test exercises PRODUCTION meta-disc output unmodified.** No overlay scripts,
   no patched restore.sh, no test-only combiner. If the heir can't recover from the real
   meta disc + real prompts, the feature has not shipped. (This is the existing harness's
   own acceptance gate — do not weaken it.)
5. **The single-key path must stay byte-for-byte unchanged.** Shares are additive; an
   archive with no shares prompts for the password exactly as today.
6. **PR per item; you merge nothing at a gate.** Branch per item, open a PR, run the gates.
   At a phase gate the user merges ("merge #N") — match the established workflow. Within a
   phase you may stack branches but keep each item's PR reviewable.
7. **Verify before claiming done.** Real runs, not just mocks — especially the blind score.
   Self-correct openly when a check disproves an assumption.

## One cycle

1. **Load state.** Read `PLAN.md`. Current phase = lowest-numbered phase with any non-`[x]`
   item. Read the `[!]` annotations.
2. **Pick the next item(s).** Among current-phase items: those whose every `deps:` is `[x]`
   and not `[!]`-blocked. Fan out parallel-safe items (non-overlapping files) via worktree
   agents; respect ordering where deps demand it.
   - If the only remaining items are `[!]`-blocked, STOP and ask that question (D0.1 first).
   - If no eligible item exists but the phase isn't done, report the blockage and STOP.
3. **Plan it.** Write a short plan: files to change, tests to write (incl. the coverage
   target), docs to update, acceptance criteria. Mark the item `[~]` in PLAN.md.
4. **Delegate** one worktree sub-agent per item:
   - Branch from current `master` into a worktree (Agent tool `isolation: worktree`).
     **Stale-base check:** before merging any agent branch, confirm
     `git merge-base master <branch>` == current `master` HEAD; if not, rebase onto master
     and re-run the gate before trusting the diff.
   - **Per-item gate (debug only, never a release build):** `make lint` + `make typecheck`
     + `make test-unit` + `make coverage` (new module 100%). Add `make shell-coverage`
     (Phase 2 restore.sh) and `make audit-gate` (if C). Agents never merge their own branch.
5. **Integrate.** Open the PR. Re-run the full gate on the merged-base. Own lint/type/cov
   fixes yourself. Push; let CI run.
6. **Mark `[x]`**, remove the worktree, append a Driver-log line
   (date · id · branch · PR · cov · note).
7. **Halt per the autonomy policy:** more eligible items in this phase → go to step 2 and
   keep landing them. Phase fully `[x]` → it's a **phase gate**: report + STOP for the user
   to merge.

## Phase 4 — the blind iterate loop (autonomous, bounded)

Only enter after Phases 0–3 are `[x]` and the user has given `LCSAS_BLIND_ACK_COST=1`.

```
for variant in [single-key, split-key-2of5]:        # K4.1 then K4.2
    loop:
        run:  LCSAS_BLIND_ACK_COST=1 make blind-restore-<variant>   # haiku, ~$5, ~20 min
        read: the run dir's verify.sh output + transcript.jsonl
        if SCORE == 15/15:  break
        diagnose the failing criterion (which of the 15), fix via a worktree agent
        (production code or harness wiring — NOT by weakening verify.sh), re-run.
        BUDGET GUARD: after 3 consecutive failing runs on the same root cause, STOP
        and report — do not burn budget looping on a bug you can't localize.
    require: two CONSECUTIVE 15/15 runs (K4.3 flake guard) before marking the variant done.
```

- Diagnose from `transcript.jsonl` (what the heir typed) + the 15 named checks in
  `verify.sh` / `tests/e2e/cdemu_blind_restore/PLAN.md`. Common split-key failure modes to
  watch: heir can't find/parse share cards; combiner not on the meta disc; restore.sh share
  branch not reached; an illusion-leak token in a combiner error message; reconstruction
  succeeds but password has a trailing newline.
- **Never fix a red score by editing `verify.sh` or `agent_prompt` to be more permissive.**
  Fix the product or the legitimate harness wiring. A weakened oracle is a silent regression.
- Tear down between runs: `make blind-restore-teardown`.

**GATE 4 = DONE:** four green score lines (single-key ×2, split-key-2of5 ×2), full coverage
green, docs shipped. Present the final PR(s) and STOP for sign-off.

## Loop behavior (under `/loop`)

- Under the standing authorization, a cycle advances across phase gates: complete a phase,
  self-merge its green PR, sync master, and continue into the next phase in the same or next
  cycle. Do not stop merely because a phase finished.
- Stop and report only when: the whole plan is `[x]` (DONE — present all four blind score
  lines); the budget guard trips in Phase 4; or a genuinely human-required item is reached.
- Never spin on a flake without a localized root cause; never auto-weaken a gate.

## First run

If `PLAN.md` is missing, stop and tell the user (it's hand-authored, not regenerated).
Otherwise begin one cycle. As of 2026-05-31 the share-format decision (D0.1) is `[x]`
(**SLIP-0039**), so the first eligible item is **K0.2** — vendor the stdlib-only pure-Python
SLIP-0039 split/combine module. Phase 0 has no blind-test cost; land K0.2→K0.4, then halt at
GATE 0→1 for the user to merge.
