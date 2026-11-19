# Recovery hardening tests

These are the **last gate** before a build is considered shippable.
Every test in this directory exists because a specific bug slipped
through the unit/integration/e2e tiers and was only caught when a
real blind-restore agent ran the production recovery path end-to-end.

They are pedantic by design: many are static-analysis or stub-binary
tests with no real e2e cost, but each one closes a concrete failure
mode that we lived through.

Run them as the final step of `make`:

```
make                       # default target == `make gate`
make test-recovery-hardening   # this tier only
```

## Opt-in gates (off by default)

A handful of tests in this tier are slow or need a special toolchain, so
they self-skip unless an env var opts them in. They are NOT part of the
default `make gate`; run them explicitly when touching the tier-1 C
binary or before a release:

| Env var | Enables |
|---------|---------|
| `LCSAS_COVERAGE=1` | `test_tier1_coverage_baseline.py` — runs `make coverage-c`, asserts `recovery/build/coverage.txt` is written. |
| `LCSAS_SANITIZE=1` | `test_tier1_sanitize.py` — runs `make sanitize`, asserts 0 ASan/UBSan/LSan findings. |
| `LCSAS_FAULT_INJECT=1` | `test_tier1_fault_inject.py` — malloc fault-injection sweep over every tier-1 unit binary. |
| `LCSAS_ZSTD_QEMU=1` | `test_tier3_zstd_qemu.py` — tier-3 zstd restore under aarch64 CPython via qemu-user (`make test-tier3-qemu`). |
| `LCSAS_OPTIONAL_ARCHES` | suppresses skips for cross-compile arches a dev doesn't have a toolchain for. |

Adjacent gates that live in OTHER tiers (named here only so it is clear
they are not in this directory): `LCSAS_ECC_REPAIR=1` drives the real
dvdisaster RS03 repair proof under `tests/integration/`, and
`LCSAS_BLIND_ACK_COST=1` acknowledges the LLM API cost of the
`make blind-restore*` blind-agent e2e harness under
`tests/e2e/cdemu_blind_restore/`.

## Catalogue

The catalogue is not committed.  Run `python3 tools/gen_hardening_catalogue.py`
(or `make gen-catalogue`) to print one row per test file, taken from the first
line of its module docstring.

## Adding a new hardening test

1. Trace the bug back to its underlying failure mode.  If you find
   yourself writing "we should also..." while triaging, that's a
   candidate.
2. Name the file after the failure surface it covers, not the bug.
   `test_tier3_invocation.py` outlives the specific tier-3-arg bug;
   `test_tier3_arg_order_bug.py` does not.
3. Write a docstring that explains *what failure mode this catches*
   and *how the production code regressed*.  The catalogue printed by
   `make gen-catalogue` reads the first line, so make it one whole sentence.
4. Hard-fail on any regression — no warnings, no skips except for
   honestly-optional hosts (e.g. cross-compile toolchains a dev
   doesn't have).  Use env vars like `LCSAS_OPTIONAL_ARCHES` for
   the rare legitimate skip.
5. Run `make gen-catalogue` to see your row.  Nothing needs committing.

## Why these are the LAST step

Unit tests verify functions.  Integration tests verify subsystems.
e2e tests verify pipelines.  Hardening tests verify that **the
production code path a real user runs** doesn't have any of the
specific failure modes we've already paid for in pain.  If any
hardening test fails, no other green light matters.
