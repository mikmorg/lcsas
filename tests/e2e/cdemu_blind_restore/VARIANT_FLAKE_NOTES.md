# Blind-restore variant flake notes

Tracks the residual variance in blind-restore variants and the
historical flakes we have since closed.

The full variant set driven by `run_variant.sh` is: `default`
(== `single-key`), `single-tenant`, `5-tenant`, `no-catalog`,
`tier1-missing`, `tier1-tier2-missing`, `split-key-2of5`,
`split-key-docs`, and `windows`. Every variant is cost-gated behind
`LCSAS_BLIND_ACK_COST=1` (it spawns a real headless Claude agent and
burns API credits — see the `make blind-restore*` targets).

## Variants and their flake profile

| Variant | Status | Notes |
|---|---|---|
| `default` / `single-key` | stable 15/15 | the gate that runs in PM cycles; plaintext password, standard flow |
| `single-tenant` | stable 15/15 | promoted cycle 8 (PR #277, closes #256) — no-prompt fast path |
| `5-tenant` | stable 15/15 | promoted cycle 7 (2026-05-27) — multi-tenant prompt stress |
| `no-catalog` | stable 15/15 | promoted cycle 7 (2026-05-27) — hash-only swap-prompt path |
| `tier1-missing` | **stable, promoted 2026-06-15** | tier-1 binary absent → restore.sh rides the fallback path; scored 16/16 on two consecutive runs (issue #227) |
| `tier1-tier2-missing` | **stable 15/15** | promoted cycle 9 2026-05-28 (PR #285 + #286); root cause + fix recorded below |
| `split-key-2of5` | tooling smoke | scripted prompt spoon-feeds the combiner + restore commands; cheap regression catcher for the share-reconstruction tools |
| `split-key-docs` | acceptance gate | KEY-04 docs-driven; scenario-only prompt + 2 production `-card.txt` artifacts, agent derives every command from on-disc text. Must score 15/15 twice consecutively before any heir-contract change merges |
| `windows` | BLOCKED | UX-08 layer-3 restore.bat-under-wine journey; hard-blocked on UX-01 (restore.bat repo discovery) + INFRA-01 (windows fixture builder). Fails loud with that reason rather than scoring a meaningless run |

The XFAIL ledger (`XFAIL.list`, enforced by
`tests/recovery_hardening/test_blind_variant_xfail_ledger.py`) is
**currently empty** — every runnable variant above either passes or is
hard-blocked on a tracked dependency (`windows`), not flaking. Add a
line to `XFAIL.list` only when a new variant has a tracked,
expected-fail bug; an expired entry fails the hardening suite.

---

## RESOLVED: tier1-tier2-missing — why it used to flake 11/15–14/15

> Recorded for posterity. This was fixed by PR #285 (bundled zstandard
> reachable in a RAM dir) + PR #286 (iso9660 catalog opened
> `immutable=1` with `conn.close()`, fixing the EBUSY that blocked disc
> swaps). The variant has scored 15/15 every run since and was promoted
> out of xfail on 2026-05-28. Kept here because the failure mode is a
> realistic tier-3 trap worth understanding.

### Symptom

The Haiku test agent followed `agent_prompt.txt` Step 3 (Repository
prompt, Password prompt), sent the password, then entered Step 4 (the
disc-swap loop).  The `restore-shell expect 'Insert the right disc|...'`
call immediately returned with exit code 2 and `[restore-shell] session
r ended unexpectedly` — the tier-3 process exited before printing any
swap prompt.  The agent had no actionable error in its tool output
and started improvising (cat'ing scripts, writing wrapper scripts,
invoking standalone_restorer.py directly).

### Root cause (environmental, not prompt-compliance)

When `restore.sh` falls through to tier 3 it does:

```sh
exec "$PYBIN" "$PYREST" --repo ... --password-file ... --target ...
```

where `$PYBIN` is the FIRST hit among:

1. `$RECOVERY/bin/$TARGET/python/bin/python3` (bundled CPython)
2. `python3` on `$PATH`
3. `python` on `$PATH`

`standalone_restorer.py` imports `zstandard` at module top to decompress
v2 packs.  On the blind-test host the bundled CPython path didn't
exist (the test variant strips the tier-3 python sidecar paths), so
`$PYBIN` resolved to the system `python3`, and the system `python3`
didn't have `zstandard` installed (it's bundled separately on the
meta disc under `tools/lib/python/`, and `restore.sh` did not export
`PYTHONPATH` to point at it).

The agent saw a session that simply died — no framed prompt, no
error message routed back through `restore-shell screen`.  This was
genuinely unactionable from inside the framed protocol.

### Why this was NOT a prompt-compliance issue

The `agent_prompt.txt` Step 3 / Step 4 protocol is unambiguous and
the verify.sh `restore_complete_check`, `script_invoke_check`, and
`no_authoring_check` regexes work correctly on a successful run
(the `default` variant scores 15/15 every run).

Tightening the prompt could not rescue a silent process death:

- The prompt's "report `RESTORE FAILED: <error>`" rule is correct
  behaviour, but verify still scores 0 on the data-integrity checks
  (#1 manifest, #7 RESTORE COMPLETE) when no restore happens.
- Teaching the prompt to run `pip install zstandard` or to fall
  through to `python3 standalone_restorer.py` directly would itself
  violate the "no improvising" rule the production-code reviewer
  is trying to characterise.

### Which verify.sh checks flaked (and which didn't)

Data-integrity invariants (didn't flake — they correctly fail when no
restore happens):

- **#1 `alpha manifest matches`** — manifest comparison; fails when
  the restorer didn't write any files.
- **#7 `RESTORE COMPLETE printed`** — agent declaration; fails when
  the agent gave up.

Prompt-compliance invariants that flaked when the agent improvised
after the session died:

- **#10 `agent did not cat any script`** — agent reads
  `restore.sh` / `standalone_restorer.py` looking for a fix.
- **#12 `agent drove restore via managed terminal session`** /
  **`agent did not author wrapper scripts`** — agent writes
  `/tmp/do-restore.sh`, `/tmp/restore_wrapper.sh`, etc.
- **`agent did not bypass restore.sh`** — agent runs
  `python3 /mnt/standalone_restorer.py` directly.  PR #235 noted that
  `no_bypass_check.py` strips `sudo|sh|bash|exec` prefixes but **not**
  `python3`, so this check could register a false PASS even when the
  agent obviously bypassed.

The variance between runs (11/15 vs 14/15 in PR #235's two runs) was
not the *failure mode* changing — it was how far the agent got in its
improvisation phase before the wall-clock cap expired.  More
improvisation = more verify checks tripped.

## Follow-ups from the tier1-tier2-missing investigation

The two production fixes that promoted this variant (PR #285/#286)
landed; these adjacent observations from the same investigation may
still be open:

- **`no_bypass_check.py` should strip a leading `python3` / `python`**
  alongside `sudo|sh|bash|exec` so a direct `python3
  standalone_restorer.py` invocation is caught as a bypass
  (PR #235 noted this).
- **`restore.sh` could capture and echo tier-3 stderr before its own
  exit** so the operator (and the test agent) sees the `ImportError`
  instead of a silent session close — now pinned by
  `tests/recovery_hardening/test_tier3_stderr_capture.py`; confirm the
  blind path benefits.
