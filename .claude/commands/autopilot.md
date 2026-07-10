# Autopilot — land the backlog sequentially, without the user

You are running the LCSAS backlog on autopilot: land ONE issue end-to-end
per iteration, or escalate it, then continue.  Invoked as `/loop /autopilot`
(self-paced) or `/loop <interval> /autopilot`.  The loop — not this
iteration — decides when work stops; your job each firing is one issue,
run to *landed* or *escalated*, never to half-done.

## Policy (edit this block as reality changes — nowhere else)

- **Merge authority:** merge on green.  `--admin` is permitted ONLY when
  the sole red checks are the named pre-existing gates — currently
  **bin-parity (#381/#320)**.  Any other red = fix forward on the branch;
  if that stalls, escalate.  A new red is never waved through.
- **Blind-restore gate:** haiku model, `LCSAS_BLIND_ACK_COST=1`, only when
  the change touches the restore path (recovery/src, recovery/scripts,
  src/lcsas/restore, src/lcsas/meta).  Tests+docs-only changes skip it.
- **Committed binaries:** any `recovery/src/**` C change regenerates the
  affected committed bins via the bin_parity recipe (drive
  `bin_parity.rebuild` + copy, then `bin_parity.py` must PASS).
- **Remotes:** after every master change, push `github` AND `origin`;
  delete the merged branch from both.
- **Non-viable (escalate on sight, never attempt):** needs physical
  hardware (burner, disc drills); needs a product/scope decision; needs
  credentials, accounts, or new spend; would change pinned upstream
  hashes (`UPSTREAM.sha256`); labeled `wontfix`, `confer-later`,
  `needs-human`, or already assigned.

## Iteration — one issue, run to done

1. **Survey.**  From a clean, synced master (`git fetch --all --prune`,
   ff-only pull; dirty tree → stash, note it in the summary).  List open
   issues; pick the highest-severity one that passes the viability gate
   (severity:high → medium → low; prefer `area:production-tier1`, then
   `area:tests`).
2. **Viability gate.**  Read the issue fully.  Confirm it is solvable
   with what this box has (toolchain, fixtures, CI) and decidable without
   the user.  A design fork with one clearly-defensible option is viable
   — take it and record the reasoning in the PR.  A genuine judgment
   call the user would want is not — escalate.
3. **Land it.**  branch → fix + tests → real validation (drive the
   affected flow, not just the suite) → PR (`Closes #N`) → watch CI with
   a background watcher (audit-gate ≈ 40–75 min; do useful prep or end
   the turn — the loop re-fires) → merge per Policy → confirm the issue
   closed → sync remotes → delete branch.
4. **Record.**  Update the project memory checkpoint (what landed, what
   surprised you); file follow-up issues for anything real you uncovered
   but didn't fix.
5. **Escalate instead** when the gate fails or landing stalls: comment
   the diagnosis + exactly what decision/resource is needed on the
   issue, label it `needs-human`, and pick the next viable issue within
   this same iteration (one substitution max — then end the iteration).

## Model tactics (subagents)

- **Main loop (opus)** keeps the judgment: issue selection, scope calls,
  EXEMPTIONS/manifest reconciles, merge decisions, delicate C/crypto.
- **sonnet** for parallel legwork with a tight, verifiable spec: writing
  tests to a per-line recipe, classification sweeps, doc updates.
- **haiku** for the blind-restore gate (standing rule) and bulk searches.
- Workers that could touch the same files run in **worktree isolation**
  (`isolation: "worktree"`); the pm.md Step-4 worker template applies.

## Stopping the loop

Stop (end the /loop, don't just end the iteration) when **no open issue
passes the viability gate**, or **two consecutive iterations ended in
escalation with nothing landed**.  Before stopping: post a summary
(issues landed with PR links, issues escalated with the decision each
needs, follow-ups filed), update memory, and send the user a push
notification.

## Hard guardrails

The **Forbidden moves** section of `.claude/commands/pm.md` applies
verbatim (blind-cost ack, `UPSTREAM.sha256`, `verify.sh`, others' tests,
red blind runs).  On any conflict between that section and this file,
the stricter rule wins.
