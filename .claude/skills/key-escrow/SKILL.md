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

- **Autonomous within a phase.** Land eligible (dep-satisfied, non-`[!]`) items back to
  back without per-item sign-off. Honor deps and parallel-safety.
- **Phase gates are hard halts.** Never cross a `GATE n→n+1` without the user merging the
  phase's PR(s). At each gate: report results + coverage deltas + the four-line score
  status, then STOP.
- **A `[!]` item halts the loop with its question** — never guess. The only `[!]` today is
  **D0.1 (share format)**: ask the user SLIP-0039 vs GF(256)+checksum, with the trade-off
  (vetted-spec + checksums + human mnemonics vs minimal re-implementation surface), then stop.
- **Phase 4 is the exception:** the blind diagnose→fix→re-run loop runs autonomously until
  both variants hit 15/15 on consecutive runs — that is the whole point of the phase.

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

- A cycle runs until the current phase empties of eligible work, then halts at the gate.
- A `[!]`-blocked item (D0.1) always halts the loop with its question — never spin.
- Phase 4 is the one autonomous loop: keep going until both variants are 15/15 ×2 or the
  budget guard trips. Never auto-advance past a phase gate, even under `/loop`.

## First run

If `PLAN.md` is missing, stop and tell the user (it's hand-authored, not regenerated).
Otherwise begin one cycle. As of 2026-05-31 the share-format decision (D0.1) is `[x]`
(**SLIP-0039**), so the first eligible item is **K0.2** — vendor the stdlib-only pure-Python
SLIP-0039 split/combine module. Phase 0 has no blind-test cost; land K0.2→K0.4, then halt at
GATE 0→1 for the user to merge.
