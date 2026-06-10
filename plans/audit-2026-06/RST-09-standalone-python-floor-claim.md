# RST-09: standalone_restorer.py claims Python ≥ 3.10 but imports datetime.UTC (3.11+)

**Priority:** P2 · **Severity:** low · **Dimension:** restore-python · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Lower standalone restorer to true 3.10 floor; add friendly version check

## Problem

The standalone builder's docstring — and the heir-facing on-disc workflow doc in five places —
advertise that the tier-3 script runs "with nothing but Python ≥ 3.10 stdlib".
`restic_fallback.py` (concatenated verbatim into the generated script) does
`from datetime import UTC` at module level; `datetime.UTC` was added in 3.11, so on a 3.10
interpreter the heir's very first command dies with an ImportError traceback before any help
text. Tier-3's pitch is running on whatever system Python exists when the bundled interpreters
won't; an off-by-one claim costs a non-technical user that path. Subprocess tests exec the
script only under the dev interpreter (3.12), so the stated floor is never validated.

## Evidence

Re-verified against current code (2026-06-10):

- `src/lcsas/restore/standalone_builder.py:6` — "...can run with nothing but Python ≥ 3.10
  stdlib (plus optional ``zstandard``)."
- `src/lcsas/restore/restic_fallback.py:49` — `from datetime import UTC` (module level; survives
  `_strip_header` into the generated file). Usages at `:1231` and `:1235`
  (`.replace(tzinfo=UTC)` / `tzinfo=UTC`).
- `docs/workflows/restore-disc-only.md:4,101,131,160,470` — five "Python 3.10+" claims.
- `tests/unit/test_standalone_subprocess.py:184+` — subprocess runs use `sys.executable` only
  (dev = 3.12); pyproject `requires-python = ">=3.11"`, so 3.10 is never exercised anywhere.

## Fix design

Lower the real floor rather than raising the claim (lower floor = more survivable decades out):

1. `restic_fallback.py:49`: `from datetime import timezone`; replace both `UTC` usages with
   `timezone.utc` (available since 3.2). Grep the other concatenated source (`_aes_pure.py`)
   and, once RST-04 lands, `_zstd_pure.py` for any other >3.10 API.
2. Startup guard in the generated prologue (before any 3.x-sensitive import):
   ```python
   import sys
   if sys.version_info < (3, 10):
       sys.exit("This restore script needs Python 3.10 or newer. "
                f"You are running {sys.version.split()[0]}. "
                "Use the bundled python3 from the recovery disc instead "
                "(see START_HERE.txt).")
   ```
3. Keep the 3.10 claims everywhere (builder docstring, restore-disc-only.md, restore.sh tier-3
   messaging) — now true. The lcsas *package* stays `requires-python >= 3.11`; only the
   generated stdlib-only script carries the 3.10 floor. Old burned discs ship the broken-on-3.10
   script forever; their bundled PBS 3.12 interpreters are unaffected, so impact is limited to
   system-Python-only hosts.

## Tests & gates

Always-on unit (`make test-unit`):

- `tests/unit/test_write_standalone.py::test_generated_script_has_no_post_310_apis` — ast-walk
  the generated script for known >3.10 markers (`datetime.UTC` import, `tomllib`, `Self`,
  `ExceptionGroup`, etc. — small denylist); additionally, if a `python3.10` binary is on PATH,
  `python3.10 -m py_compile` the generated file (skip otherwise).
- `tests/unit/test_standalone_subprocess.py::test_old_python_gets_friendly_version_error` —
  exec with a faked `sys.version_info` shim (or patched guard threshold); assert the
  plain-English message, nonzero exit, no traceback.
- Doc check (GATE plans' docs-contract family): floor stated in `restore-disc-only.md` matches
  the guard constant in `standalone_builder.py`.

## Acceptance criteria

- [ ] Generated script compiles under Python 3.10 (`py_compile`) and runs `--help`.
- [ ] Under a <3.10 interpreter (or simulated), the script prints the friendly message instead
      of a traceback.
- [ ] All "Python ≥ 3.10" claims are true; grep finds no `datetime import UTC` in
      `src/lcsas/restore/`.
- [ ] `make test-unit && make lint && make typecheck` pass.

## Dependencies & related plans

- Coordinate standalone regeneration with RST-02/03/06/08 (single regeneration batch).
- RST-04's `_zstd_pure.py` must obey the same 3.10 floor — the ast-walk test covers it once it
  joins the concatenation set.

## Effort

0.5 day. Optional: install python3.10 (deadsnakes/pyenv on this VM, /scratch) so the py_compile
leg runs locally and in CI.
