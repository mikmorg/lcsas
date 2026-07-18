# recovery/CLAUDE.md

Guidance for working in the `recovery/` directory (tier-1 C binary and recovery scripts).

## Targets

```bash
# Host build (default):
make -C recovery

# All unit tests + Python hardening suite:
make -C recovery test

# Sanitizer gate (opt-in, needs clang):
make -C recovery sanitize

# Line coverage report (opt-in, needs gcovr):
make -C recovery coverage-c           # writes recovery/build/coverage.txt
LCSAS_COVERAGE=1 pytest tests/recovery_hardening/test_tier1_coverage_baseline.py -v

# Fault-injection sweep (opt-in, no extra deps):
make -C recovery fault-inject         # full sweep (~3 min)
make -C recovery fault-inject MAX_N=100  # smoke (~5 s)

# Fuzz harnesses (opt-in, needs clang with libFuzzer):
make -C recovery fuzz-json-smoke      # 60 s
make -C recovery fuzz-b64-smoke       # 60 s
make -C recovery fuzz-zstd-smoke      # 60 s
make -C recovery fuzz-path-smoke      # 60 s
make -C recovery fuzz-repo-smoke      # 60 s
make -C recovery fuzz-keyshare-smoke  # 60 s
make -C recovery fuzz-tree-smoke      # 60 s
make -C recovery fuzz-rs03-smoke      # 60 s
make -C recovery fuzz-smoke           # all eight × 60 s (~8 min)

# Comprehensive audit gate (opt-in — NOT part of make gate):
make -C recovery audit-gate           # coverage + sanitize + fuzz-smoke (~10 min)
make -C recovery audit-gate THRESHOLD=95   # aspirational 95% target
# Equivalent from repo root:
make audit-gate
```

See `recovery/docs/AUDIT.md` for full details on the audit gate,
threshold rationale, and how to interpret failures.

## When to run audit-gate

Run `make audit-gate` (or `make audit-gate THRESHOLD=95`) **before merging any PR**
that touches `recovery/src/**`.  It requires clang + gcovr locally and
takes a while (the CI run is ~55 min).

Since #414 it IS a CI gate: `.github/workflows/audit-gate.yml` runs on
every PR (an in-workflow `changes` job skip-passes PRs that don't touch
gate paths) and `audit-gate-required` is a required branch-protection
check, alongside `bin-parity-required`.

## Architecture

Tier-1 recovery binary: `recovery/src/lcsas-restore/` — C89, static,
depends only on kernel + libc.  Sibling tier-1 tools under
`recovery/src/`: `lcsas-keyshare` (SLIP-0039 combiner), `lcsas-ecc`
(RS03 verify/repair), `lcsas-iso9660`, `lcsas-init`.  Vendored
dependencies: sqlite3 and zstd (source in `recovery/vendored/`; built
alongside our code — not runtime dependencies).

See the root `CLAUDE.md` for the full recovery cascade (tier 1 → 2 → 3).
