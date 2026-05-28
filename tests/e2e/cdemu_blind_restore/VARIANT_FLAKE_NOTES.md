# Blind-restore variant flake notes

Tracks the residual variance in blind-restore variants.

## Variants and their flake profile

| Variant | Status | Notes |
|---|---|---|
| `default` | stable 15/15 | the gate that runs in PM cycles |
| `tier1-missing` | xfail | tier-2 falls through to tier-3 (issue #227); tier-3 path needs verification |
| `tier1-tier2-missing` | **stable 15/15** | promoted out of xfail 2026-05-28 (PR #285 + #286) |

## tier1-tier2-missing — why it flakes 11/15–14/15

### Symptom

The Haiku test agent follows `agent_prompt.txt` Step 3 (Repository
prompt, Password prompt), sends the password, then enters Step 4 (the
disc-swap loop).  The `restore-shell expect 'Insert the right disc|...'`
call immediately returns with exit code 2 and `[restore-shell] session
r ended unexpectedly` — the tier-3 process exited before printing any
swap prompt.  The agent has no actionable error in its tool output
and starts improvising (cat'ing scripts, writing wrapper scripts,
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
v2 packs.  On the blind-test host the bundled CPython path doesn't
exist (the test variant strips the tier-3 python sidecar paths), so
`$PYBIN` resolves to the system `python3`, and the system `python3`
doesn't have `zstandard` installed (it's bundled separately on the
meta disc under `tools/lib/python/`, and `restore.sh` never exports
`PYTHONPATH` to point at it).

The agent sees a session that simply died — no framed prompt, no
error message routed back through `restore-shell screen`.  This is
genuinely unactionable from inside the framed protocol.

### Why this is NOT a prompt-compliance issue

The `agent_prompt.txt` Step 3 / Step 4 protocol is unambiguous and
the verify.sh `restore_complete_check`, `script_invoke_check`, and
`no_authoring_check` regexes work correctly on a successful run
(the `default` variant scores 15/15 every run).

Tightening the prompt cannot rescue a silent process death:

- The prompt's "report `RESTORE FAILED: <error>`" rule is correct
  behaviour, but verify still scores 0 on the data-integrity checks
  (#1 manifest, #7 RESTORE COMPLETE) when no restore happens.
- Teaching the prompt to run `pip install zstandard` or to fall
  through to `python3 standalone_restorer.py` directly would itself
  violate the "no improvising" rule the production-code reviewer
  is trying to characterise.

### Which verify.sh checks flake (and which don't)

Data-integrity invariants (don't flake — they correctly fail when no
restore happens):

- **#1 `alpha manifest matches`** — manifest comparison; fails when
  the restorer didn't write any files.
- **#7 `RESTORE COMPLETE printed`** — agent declaration; fails when
  the agent gave up.

Prompt-compliance invariants that flake when the agent improvises
after the session dies:

- **#10 `agent did not cat any script`** — agent reads
  `restore.sh` / `standalone_restorer.py` looking for a fix.
- **#12 `agent did not author wrapper scripts`** — agent writes
  `/tmp/do-restore.sh`, `/tmp/restore_wrapper.sh`, etc.
- **#13 `agent did not bypass restore.sh`** — agent runs
  `python3 /mnt/standalone_restorer.py` directly.  PR #235 already
  noted that `no_bypass_check.py` strips `sudo|sh|bash|exec`
  prefixes but **not** `python3`, so this check can register a
  false PASS even when the agent obviously bypassed — worth a
  follow-up.

The variance between runs (11/15 vs 14/15 in PR #235's two runs) is
not the *failure mode* changing — it's how far the agent gets in its
improvisation phase before the wall-clock cap expires.  More
improvisation = more verify checks tripped.

## Recommended follow-ups (out of scope for #236)

One bullet each — these are observations, not prescriptions:

- **Bundled CPython carry zstandard at default-importable location**
  (or `restore.sh` sets `PYTHONPATH` to the bundled
  `tools/lib/python/` before exec'ing tier 3).  Today the meta
  builder bundles `zstandard` under `tools/lib/python/` but
  `restore.sh` doesn't add it to `PYTHONPATH` on the tier-3 exec
  path.
- **`restore.sh` could capture and echo tier-3 stderr before its own
  exit** so the operator (and the test agent) sees the `ImportError`
  instead of a silent session close.
- **`no_bypass_check.py` should strip a leading `python3` / `python`
  alongside `sudo|sh|bash|exec`** so direct `python3
  standalone_restorer.py` invocations are caught as bypasses
  (PR #235 noted this).

## XFAIL policy

`run_variant.sh` keeps `tier1-tier2-missing` in `LCSAS_VARIANT_XFAIL`
until at least one of the follow-ups above lands.  Removing it from
xfail before then would push the residual variance into the gate
that runs in PM cycles.
