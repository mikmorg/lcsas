# Recovery Toolchain Build & Manifest

> **Status note.** This document specifies the planned `lcsas recovery
> *` subcommand family, the on-disk `recovery/` build tree, and the
> `src/lcsas/recovery/build.py` module that orchestrates them.  None
> of these artefacts exist in the repository today — every workflow
> in this file is marked **[gap]**.  The doc is written as a
> precise spec so the implementation can land against it.  Source
> refs below cite either (a) the closest existing analog (the
> meta-volume builder in `src/lcsas/meta/`) or (b) the planned path
> the implementation must occupy.  Lines cited at planned paths are
> expected line numbers in the future file; reviewers should treat
> them as anchors to be honoured by the implementing PR.

## Why a recovery toolchain exists

LCSAS is built on a strict **zero runtime dependencies** rule
(`CLAUDE.md:71`): the Python codebase uses only the standard library
plus an optional `zstandard`, so a fresh machine can decrypt and
extract a Rustic repo with nothing but a Python 3 interpreter
(`src/lcsas/restore/restic_fallback.py`).  The meta-volume goes
further — it bundles static x86_64 binaries (rustic, xorriso,
python3) so a bare machine with no network and no package manager
can still drive the full restore pipeline (`CLAUDE.md:73`,
`src/lcsas/meta/builder.py:168`).

The **recovery toolchain** closes the last gap: the static binaries
themselves.  Today the meta-volume opportunistically copies whatever
`rustic-static` happens to be on the build host
(`src/lcsas/meta/builder.py:1754`).  That is fine on x86_64 with
musl, but it is fragile (host-dependent), it is unverifiable
(no manifest), and it has no story for aarch64, riscv64, or
Windows recovery hosts.  The `recovery/` tree fixes this by:

1. Vendoring the **C source** for a minimal restore reader (the
   "C recovery binary") that can decrypt and decompress a single
   Rustic pack without Rust, Python, or any shared library.
2. Building **prebuilt static binaries** for every supported target
   arch — `x86_64-linux`, `aarch64-linux`, `riscv64-linux`,
   `x86_64-windows` — under reproducible build flags so the
   SHA256 of each artefact is stable across rebuilds.
3. Publishing a **SHA256 manifest** (`recovery/MANIFEST.sha256`)
   that ships on every meta-disc, so an operator restoring 20 years
   from now can verify that the prebuilt they trust hasn't bit-rotted.
4. Documenting a **fallback cascade** (Tier 1 → 2 → 3 → 4) so the
   live-restore wizard can degrade gracefully from "prebuilt binary
   on disc" to "compile from vendored source on a foreign host" to
   "rebuild rustic from upstream" — without ever requiring network.

The cascade is documented at `recovery/docs/TIERS.txt` and summarised
in [the Tier cascade](#the-tier-1234-fallback-cascade) section
below.

## Table of contents

- [Why a recovery toolchain exists](#why-a-recovery-toolchain-exists)
- [Layout of the `recovery/` tree](#layout-of-the-recovery-tree)
- [`lcsas recovery build` — build all targets](#lcsas-recovery-build--build-all-targets)
- [`lcsas recovery build --target <arch>` — single-target build](#lcsas-recovery-build---target-arch--single-target-build)
- [`lcsas recovery test` — run the recovery test suite](#lcsas-recovery-test--run-the-recovery-test-suite)
- [`lcsas recovery manifest` — produce SHA256 manifest](#lcsas-recovery-manifest--produce-sha256-manifest)
- [`lcsas recovery verify` — verify manifest against artefacts](#lcsas-recovery-verify--verify-manifest-against-artefacts)
- [Reproducible build verification](#reproducible-build-verification)
- [The Tier 1→2→3→4 fallback cascade](#the-tier-1234-fallback-cascade)
- [Architecture detection (`detect_arch.sh`)](#architecture-detection-detect_archsh)
- [Cross-cutting variant matrix](#cross-cutting-variant-matrix)
- [Test coverage summary](#test-coverage-summary)
- [Consolidated source refs](#consolidated-source-refs)

## Layout of the `recovery/` tree

All paths below are **planned** — none of the directory exists yet.

```
recovery/
├── Makefile                       # build rules, one target per arch
├── MANIFEST.sha256                # checked-in expected hashes
├── src/                           # vendored C source (zlib/aes/rustic-reader)
├── prebuilt/
│   ├── x86_64-linux/recover
│   ├── aarch64-linux/recover
│   ├── riscv64-linux/recover
│   └── x86_64-windows/recover.exe
├── scripts/
│   ├── detect_arch.sh             # arch detection for live-restore
│   ├── rebuild.sh                 # Tier 3 fallback: rebuild C from source
│   └── rebuild_rustic.sh          # Tier 4 fallback: rebuild rustic
├── tests/
│   └── recover_smoke.sh           # round-trip test on a known pack
└── docs/
    ├── TIERS.txt                  # the 4-tier cascade
    └── BUILD.txt                  # toolchain prerequisites
```

The Python orchestrator lives at `src/lcsas/recovery/build.py` and
its CLI surface is mounted under `src/lcsas/cli/main.py` as the
`recovery` subparser (planned slot adjacent to the existing `meta`
subparser at `src/lcsas/cli/main.py:377`).

---

## `lcsas recovery build` — build all targets

**Purpose:** Drive the `recovery/Makefile` to produce prebuilt static
binaries for every supported target arch, then refresh
`recovery/MANIFEST.sha256` so it agrees with the new artefacts.
This is what a release engineer runs before cutting a meta-disc.

**Prerequisites:**

- `make`, `gcc` (host).
- `musl-gcc` or `x86_64-linux-musl-gcc` for the Linux static targets
  (the C reader links against musl to avoid glibc symbol-version
  pinning).
- `aarch64-linux-musl-gcc` (cross) for the aarch64 target.
- `riscv64-linux-musl-gcc` (cross) for the riscv64 target.
- `x86_64-w64-mingw32-gcc` (mingw-w64) for the Windows target.
- `SOURCE_DATE_EPOCH` must be exported (see
  [Reproducible build verification](#reproducible-build-verification)).
- Prerequisite list is mirrored in `recovery/docs/BUILD.txt`.

**Steps:**

1. CLI parses `lcsas recovery build` and dispatches to
   `cmd_recovery_build` (`src/lcsas/cli/main.py:LINE` — **[gap]**,
   slot reserved adjacent to `cmd_meta_build` at
   `src/lcsas/cli/main.py:1698`).
2. `cmd_recovery_build` calls
   `lcsas.recovery.build.build_all(project_root)`
   (`src/lcsas/recovery/build.py:LINE` — **[gap]**).
3. `build_all` resolves `recovery/` relative to the project root
   and shells out to `make -C recovery all`
   (`recovery/Makefile:LINE` — **[gap]**, expected to define an
   `all:` target that depends on
   `prebuilt/x86_64-linux/recover`,
   `prebuilt/aarch64-linux/recover`,
   `prebuilt/riscv64-linux/recover`, and
   `prebuilt/x86_64-windows/recover.exe`).
4. Each per-target rule in the Makefile invokes the matching
   musl/mingw toolchain with reproducible flags
   (`-ffile-prefix-map=$(PWD)=.`, `-D__FILE__=...`,
   `-Wl,--build-id=none`, `-static` for Linux,
   `-static-libgcc -static-libstdc++` for Windows).
5. Once all four binaries are present, `build_all` calls
   `lcsas.recovery.build.write_manifest()`
   (`src/lcsas/recovery/build.py:LINE` — **[gap]**) which
   regenerates `recovery/MANIFEST.sha256`.
6. The handler logs each artefact path + hash and returns 0 on
   success.

**Expected outcome:** Four binaries under
`recovery/prebuilt/<arch>/` and a refreshed
`recovery/MANIFEST.sha256` whose lines exactly match
`sha256sum recovery/prebuilt/*/recover*` output.  Exit code 0.

**Variant axes that apply:**

- Target architecture: **all four** in one shot
  (x86_64-linux, aarch64-linux, riscv64-linux, x86_64-windows).
- Recovery tier: produces the **Tier 1** prebuilt artefacts.
- Reproducibility: **yes** — every rule is required to honour
  `SOURCE_DATE_EPOCH` and strip build-id.

**Test coverage:**

- Existing: **none**.  No integration test exercises a recovery
  build today; `tests/integration/test_recovery_orchestration.py`
  is **[gap]**.
- Gaps: full multi-arch CI matrix; a smoke test that runs the
  Makefile under `SOURCE_DATE_EPOCH=0` and asserts the hashes
  match `recovery/MANIFEST.sha256`.

**Source refs:** `src/lcsas/cli/main.py:LINE` **[gap]**,
`src/lcsas/recovery/build.py:LINE` **[gap]**,
`recovery/Makefile:LINE` **[gap]**,
`recovery/docs/BUILD.txt` **[gap]**,
`src/lcsas/cli/main.py:377` (analog: `meta` subparser),
`src/lcsas/cli/main.py:1698` (analog: `cmd_meta_build`),
`src/lcsas/meta/builder.py:1754` (analog: static rustic bundling).

---

## `lcsas recovery build --target <arch>` — single-target build

**Purpose:** Rebuild exactly one target — typically used (a) in CI
to fan out one job per arch and (b) by a recovery operator at
**Tier 3** of the cascade, when the prebuilt binary on the meta-disc
is missing or won't run and the operator has a working toolchain
for one arch only.

**Prerequisites:** The single musl or mingw toolchain matching the
chosen `--target`.  Valid values: `x86_64-linux`, `aarch64-linux`,
`riscv64-linux`, `x86_64-windows`.

**Steps:**

1. CLI parses `--target` and validates it against the allowed list
   (`src/lcsas/cli/main.py:LINE` — **[gap]**; the argparse
   `choices=` list must match the `prebuilt/` subdirectories).
2. `cmd_recovery_build` dispatches to
   `lcsas.recovery.build.build_one(target)`
   (`src/lcsas/recovery/build.py:LINE` — **[gap]**).
3. `build_one` shells out to `make -C recovery prebuilt/<target>/recover`
   (or `recover.exe` for Windows)
   (`recovery/Makefile:LINE` — **[gap]**).
4. The handler **does not** rewrite
   `recovery/MANIFEST.sha256` — that is the responsibility of
   `lcsas recovery manifest`, so single-target rebuilds never
   silently desynchronise the manifest.
5. The new artefact's hash is logged and compared against the
   existing manifest line; a mismatch logs a warning but is not
   fatal (see [Reproducible build verification](#reproducible-build-verification)).

**Expected outcome:** Exactly one binary refreshed under
`recovery/prebuilt/<target>/`; manifest untouched; exit code 0.

**Variant axes that apply:**

- Target architecture: **one**.
- Recovery tier: when invoked by hand at Tier 3, this is the
  workflow the operator runs.
- Reproducibility: yes — same flags as the all-targets build.

**Test coverage:**

- x86_64-linux: **[gap]**
- aarch64-linux: **[gap]**
- riscv64-linux: **[gap]**
- x86_64-windows: **[gap]**
- All four are gaps because the orchestration test file does
  not yet exist (`tests/integration/test_recovery_orchestration.py`).

**Source refs:** `src/lcsas/cli/main.py:LINE` **[gap]**,
`src/lcsas/recovery/build.py:LINE` **[gap]**,
`recovery/Makefile:LINE` **[gap]**,
`recovery/scripts/rebuild.sh:LINE` **[gap]**.

---

## `lcsas recovery test` — run the recovery test suite

**Purpose:** Execute the round-trip smoke test that proves a freshly
built `recover` binary can decrypt a known fixture pack and emit the
expected plaintext.  This is what guards against silent C-side
regressions; it is the "does this binary actually do what it claims"
check that the manifest alone cannot provide.

**Prerequisites:** A built `recover` binary for the host arch under
`recovery/prebuilt/<host-arch>/recover`.  Host arch is detected by
`recovery/scripts/detect_arch.sh` (see
[Architecture detection](#architecture-detection-detect_archsh)).
A small fixture pack ships under `recovery/tests/fixtures/`.

**Steps:**

1. CLI parses `lcsas recovery test` and dispatches to
   `cmd_recovery_test` (`src/lcsas/cli/main.py:LINE` — **[gap]**).
2. Handler calls `lcsas.recovery.build.run_tests(project_root)`
   (`src/lcsas/recovery/build.py:LINE` — **[gap]**).
3. `run_tests` first invokes `recovery/scripts/detect_arch.sh`
   to pick the right prebuilt
   (`recovery/scripts/detect_arch.sh:LINE` — **[gap]**).
4. It then invokes `recovery/tests/recover_smoke.sh`
   (`recovery/tests/recover_smoke.sh:LINE` — **[gap]**) which
   feeds the fixture pack into the binary, captures stdout, and
   diffs it against the known plaintext.
5. A non-zero exit from the smoke script propagates up as a
   non-zero exit from the CLI.

**Expected outcome:** Exit 0 with a single "OK" line per arch
tested; exit 1 with the smoke script's diff output otherwise.

**Variant axes that apply:**

- Target architecture: **host arch only** by default; cross-arch
  testing requires QEMU and is out of scope for the smoke script.
- Recovery tier: validates **Tier 1** (prebuilt) and **Tier 3**
  (rebuild-from-source) when run after `recovery build --target`.
- Reproducibility: not applicable (this tests behaviour, not bytes).

**Test coverage:**

- Existing: **none** — `tests/integration/test_recovery_orchestration.py`
  is **[gap]**.
- Gaps: a pytest-level wrapper that invokes
  `lcsas recovery test` and asserts exit 0 on the host arch.

**Source refs:** `src/lcsas/cli/main.py:LINE` **[gap]**,
`src/lcsas/recovery/build.py:LINE` **[gap]**,
`recovery/scripts/detect_arch.sh:LINE` **[gap]**,
`recovery/tests/recover_smoke.sh:LINE` **[gap]**,
`tests/integration/test_recovery_orchestration.py` **[gap]**.

---

## `lcsas recovery manifest` — produce SHA256 manifest

**Purpose:** Hash every artefact under `recovery/prebuilt/` and
write the result to `recovery/MANIFEST.sha256` in canonical
`sha256sum` format (one line per file, lexicographic order).
The manifest is what ships on the meta-disc and what
`lcsas recovery verify` checks against.

**Prerequisites:** Built artefacts under `recovery/prebuilt/`.

**Steps:**

1. CLI parses `lcsas recovery manifest` and dispatches to
   `cmd_recovery_manifest` (`src/lcsas/cli/main.py:LINE` — **[gap]**).
2. Handler calls `lcsas.recovery.build.write_manifest(project_root)`
   (`src/lcsas/recovery/build.py:LINE` — **[gap]**).
3. `write_manifest` walks `recovery/prebuilt/` in
   lexicographic order, hashes each regular file with
   `hashlib.sha256` (Python stdlib — honours the zero-runtime-deps
   rule from `CLAUDE.md:71`), and emits lines of the form
   `<hex>  prebuilt/<arch>/<name>`.
4. The manifest is written **atomically** (write to
   `MANIFEST.sha256.tmp`, then `os.replace`) so a crashed run
   never leaves a half-written manifest.
5. Each line is also logged at INFO so the release engineer can
   diff against the previous manifest in their terminal.

**Expected outcome:** A `recovery/MANIFEST.sha256` whose contents
match `cd recovery && sha256sum prebuilt/*/recover*` byte-for-byte.
Exit 0.

**Variant axes that apply:**

- Target architecture: covers all artefacts under `prebuilt/`,
  not filtered by arch.
- Recovery tier: produces the artefact that **Tier 1** trusts.
- Reproducibility: the manifest itself is reproducible — the same
  set of artefacts always produces the same manifest because the
  walk is deterministic.

**Test coverage:**

- Existing: **none**.
- Gaps: a unit test that builds a fake `recovery/prebuilt/` tree
  with synthetic files and asserts the resulting manifest matches
  a golden string.

**Source refs:** `src/lcsas/cli/main.py:LINE` **[gap]**,
`src/lcsas/recovery/build.py:LINE` **[gap]**,
`recovery/MANIFEST.sha256` **[gap]**.

---

## `lcsas recovery verify` — verify manifest against artefacts

**Purpose:** Compare the on-disk artefacts under
`recovery/prebuilt/` against the hashes recorded in
`recovery/MANIFEST.sha256`.  This is the audit step run (a) in CI
to gate releases, (b) on the live-restore disc to detect bit-rot,
and (c) by a paranoid operator who suspects supply-chain tampering.

**Prerequisites:** Both `recovery/MANIFEST.sha256` and the
`recovery/prebuilt/` artefacts must be present.

**Steps:**

1. CLI parses `lcsas recovery verify` and dispatches to
   `cmd_recovery_verify` (`src/lcsas/cli/main.py:LINE` — **[gap]**).
2. Handler calls `lcsas.recovery.build.verify_manifest(project_root)`
   (`src/lcsas/recovery/build.py:LINE` — **[gap]**).
3. `verify_manifest` parses `recovery/MANIFEST.sha256`,
   recomputes each artefact's SHA256, and collects mismatches.
4. Three failure modes are distinguished:
   - **missing** — the manifest references a file that does not
     exist on disk (Tier 1 broken; fall through to Tier 2).
   - **extra** — a file exists on disk but the manifest does not
     mention it (warn, but do not fail — leftover from a partial
     build).
   - **mismatch** — file exists and is in the manifest but the
     hash differs (HARD FAIL — possible tampering or bit-rot).
5. On any mismatch or missing entry, the handler returns a
   non-zero exit code and logs each offending file.

**Expected outcome:** Exit 0 when every manifest entry hashes
correctly; exit 1 otherwise.  Stdout is a one-line summary;
stderr enumerates the offenders.

**Variant axes that apply:**

- Target architecture: verifies all arches whose binaries are
  present.
- Recovery tier: this is the **Tier 1 gate** — if `verify` fails,
  the live-restore wizard must fall through to Tier 2.
- Reproducibility: indirectly verifies it — a mismatch on
  artefacts that were produced reproducibly elsewhere is
  diagnostic of a reproducibility break.

**Test coverage:**

- Existing: **none**.
- Gaps: a unit test that mutates a single byte in a fake artefact
  and asserts `verify_manifest` reports it as a mismatch; a
  second test that deletes an artefact and asserts it is reported
  as missing.

**Source refs:** `src/lcsas/cli/main.py:LINE` **[gap]**,
`src/lcsas/recovery/build.py:LINE` **[gap]**,
`recovery/MANIFEST.sha256` **[gap]**.

---

## Reproducible build verification

**Purpose:** Prove that two independent runs of
`lcsas recovery build` on the same source tree produce
**byte-identical** artefacts.  Without this, the SHA256 manifest is
worthless: any innocuous variation (build timestamp, debug paths,
build-id) would invalidate the recorded hashes on every rebuild
and the operator would have no way to distinguish "expected
rebuild" from "tampering".

**Prerequisites:** Two clean checkouts of the source tree on
machines (or containers) with identical musl/mingw toolchain
versions.  `SOURCE_DATE_EPOCH` set to the same value in both runs.

**Steps:**

1. Set `SOURCE_DATE_EPOCH` to a fixed value (the project convention
   is to pin it to the timestamp of the most recent commit touching
   `recovery/src/`).
2. Run `lcsas recovery build` in checkout A.
3. Run `lcsas recovery build` in checkout B (clean tree, same
   `SOURCE_DATE_EPOCH`).
4. Diff `recovery/prebuilt/` byte-for-byte between A and B; the
   diff must be empty.
5. Equivalently, diff `recovery/MANIFEST.sha256` between A and B;
   that diff must also be empty.
6. The Makefile is required to enforce reproducibility-relevant
   flags on every rule (`recovery/Makefile:LINE` — **[gap]**):
   - `-ffile-prefix-map=$(PWD)=.` to strip absolute build paths.
   - `-Wl,--build-id=none` to suppress the per-build linker GUID.
   - `-static` to remove dependence on host shared-library
     versions.
   - For Windows: `-Wl,--no-insert-timestamp` so the PE header
     timestamp is zeroed.

**Expected outcome:** Two independent builds produce identical
artefacts and identical manifests; `lcsas recovery verify` passes
against either build's manifest using the other build's artefacts.

**Variant axes that apply:**

- Target architecture: must hold for **all four** target arches
  independently.
- Recovery tier: this is what makes **Tier 1 trustworthy**.
- Reproducibility: this *is* the reproducibility check.

**Test coverage:**

- Existing: **none**.
- Gaps: a CI job that runs `lcsas recovery build` twice in
  separate containers and `diff -r`s the `prebuilt/` trees; a
  per-arch breakdown of `[gap]` markers:
  - x86_64-linux **[gap]**
  - aarch64-linux **[gap]**
  - riscv64-linux **[gap]**
  - x86_64-windows **[gap]**

**Source refs:** `recovery/Makefile:LINE` **[gap]**,
`recovery/docs/BUILD.txt` **[gap]**.

---

## The Tier 1→2→3→4 fallback cascade

**Purpose:** Define how the live-restore wizard chooses *which*
recovery binary to run when an operator boots the meta-disc on
unknown hardware.  Each tier is more expensive and slower than the
previous one; the wizard always tries them in order and stops at
the first that succeeds.  The full text of the cascade is intended
to ship verbatim on the meta-disc at
`recovery/docs/TIERS.txt` (**[gap]**).

| Tier | Source                          | What runs                                                 | Cost                |
|------|---------------------------------|-----------------------------------------------------------|---------------------|
| 1    | `recovery/prebuilt/<arch>/`     | The static C `recover` binary shipped on the meta-disc    | seconds             |
| 2    | Vendored `recovery/src/` C tree | Same logic, but linked against the host's libc            | minutes (needs gcc) |
| 3    | `recovery/scripts/rebuild.sh`   | Rebuild the C reader from `recovery/src/` for this host   | minutes             |
| 4    | `recovery/scripts/rebuild_rustic.sh` | Rebuild upstream rustic from vendored source         | tens of minutes     |

**Steps (live-restore wizard chooses a tier):**

1. The wizard calls `recovery/scripts/detect_arch.sh`
   (`recovery/scripts/detect_arch.sh:LINE` — **[gap]**) to
   identify the host triple.
2. **Tier 1** — wizard checks for
   `recovery/prebuilt/<arch>/recover`; if present and
   `lcsas recovery verify` agrees, it runs that.  This is the
   common path and the only path that does not require a host
   compiler.
3. **Tier 2** — if no prebuilt for the detected arch exists (e.g.
   the operator booted on hardware not in the prebuilt matrix),
   the wizard greps the prebuilt tree for *any* binary that the
   host happens to run (e.g. an x86_64 prebuilt under qemu-user).
   This tier is opportunistic and may fail silently.
4. **Tier 3** — wizard runs `recovery/scripts/rebuild.sh`
   (`recovery/scripts/rebuild.sh:LINE` — **[gap]**), which
   shells out to whatever C compiler is on `$PATH`, links
   against the host libc (not musl — Tier 3 is the "host
   toolchain is whatever it is" tier), and produces a fresh
   `recover` binary in a scratch directory.
5. **Tier 4** — wizard runs
   `recovery/scripts/rebuild_rustic.sh` (**[gap]**), which
   rebuilds the upstream Rust `rustic` from vendored source.
   This is the "last resort" tier; it requires `cargo` on the
   host and takes tens of minutes.  It exists so that an
   operator with **only Rust** can still recover, because every
   prior tier requires a C toolchain.

**Expected outcome:** The first tier that succeeds yields a working
binary the wizard uses to extract packs.  If all four fail, the
pure-Python fallback (`src/lcsas/restore/restic_fallback.py`) is
invoked — that path is documented elsewhere and is the absolute
last line of defence (`CLAUDE.md:71`).

**Variant axes that apply:**

- Target architecture: cascade is invoked **per arch detected by
  the host**.
- Recovery tier: this section *is* the tier definition.
- Reproducibility: only **Tier 1** is reproducible; Tier 3 and 4
  produce a binary that is by construction host-specific.

**Test coverage:**

- Tier 1 (prebuilt happy path): **[gap]**.
- Tier 2 (cross-arch opportunistic run): **[gap]**.
- Tier 3 (rebuild C from source): **[gap]**.
- Tier 4 (rebuild rustic from upstream): **[gap]**.
- A planned integration test at
  `tests/integration/test_recovery_orchestration.py` is **[gap]**
  and should exercise Tiers 1 and 3 at minimum (Tier 2 requires
  qemu-user, Tier 4 requires cargo).

**Source refs:** `recovery/docs/TIERS.txt` **[gap]**,
`recovery/scripts/detect_arch.sh` **[gap]**,
`recovery/scripts/rebuild.sh` **[gap]**,
`src/lcsas/restore/restic_fallback.py` (analog: pure-Python last
resort).

---

## Architecture detection (`detect_arch.sh`)

**Purpose:** Map the host's `uname` output to one of the four
canonical target triples the rest of the toolchain understands.
Every other workflow in this document depends on this script
returning a known string.

**Prerequisites:** POSIX `sh` and `uname`.  No Python, no
coreutils-isms.

**Steps:**

1. Script runs `uname -s` and `uname -m`
   (`recovery/scripts/detect_arch.sh:LINE` — **[gap]**).
2. Maps the pair to one of:
   - `Linux  x86_64`  → `x86_64-linux`
   - `Linux  aarch64` → `aarch64-linux`
   - `Linux  arm64`   → `aarch64-linux` (Darwin spelling on
     Asahi-style installs that report arm64).
   - `Linux  riscv64` → `riscv64-linux`
   - `MINGW*`, `MSYS*`, `CYGWIN*` with `x86_64` → `x86_64-windows`
3. Unknown pairs exit non-zero with a diagnostic on stderr so the
   wizard can fall through to Tier 2.

**Expected outcome:** A single target-triple line on stdout and
exit 0; or exit non-zero with a diagnostic.

**Variant axes that apply:**

- Target architecture: this is **the** dispatcher — every arch
  the toolchain supports must be recognised here.
- Recovery tier: invoked at Tier 1 selection time; also invoked
  by Tier 3 to pick the right `CC`.
- Reproducibility: pure shell — output is deterministic given
  the input.

**Test coverage:**

- x86_64-linux: **[gap]**
- aarch64-linux: **[gap]**
- riscv64-linux: **[gap]**
- x86_64-windows: **[gap]** (would need a MinGW-ish harness or a
  stubbed `uname`).
- Recommended approach: a unit test that monkeypatches `uname`'s
  output via a wrapper script under `PATH` and asserts the
  resulting triple.

**Source refs:** `recovery/scripts/detect_arch.sh` **[gap]**.

---

## Cross-cutting variant matrix

The variant axes from the assignment apply to every workflow above
as follows:

| Workflow                              | Arch axis     | Tier axis    | Reproducibility |
|---------------------------------------|---------------|--------------|-----------------|
| `lcsas recovery build`                | all 4         | produces T1  | yes             |
| `lcsas recovery build --target`       | one           | T3 op tool   | yes             |
| `lcsas recovery test`                 | host arch     | T1/T3 gate   | n/a (behaviour) |
| `lcsas recovery manifest`             | all artefacts | produces T1  | yes             |
| `lcsas recovery verify`               | all artefacts | T1 gate      | indirect        |
| Reproducible build verification       | all 4         | makes T1 safe| yes (the test)  |
| Tier cascade                          | per host      | T1→T2→T3→T4  | only T1         |
| `detect_arch.sh`                      | dispatcher    | feeds all    | yes (pure sh)   |

---

## Test coverage summary

Per-architecture coverage status against the planned integration
test `tests/integration/test_recovery_orchestration.py`
(**[gap]** — file does not exist):

| Architecture     | Prebuilt build | Reproducibility | Smoke test | Tier 3 rebuild |
|------------------|----------------|-----------------|------------|----------------|
| x86_64-linux     | **[gap]**      | **[gap]**       | **[gap]**  | **[gap]**      |
| aarch64-linux    | **[gap]**      | **[gap]**       | **[gap]**  | **[gap]**      |
| riscv64-linux    | **[gap]**      | **[gap]**       | **[gap]**  | **[gap]**      |
| x86_64-windows   | **[gap]**      | **[gap]**       | **[gap]**  | n/a            |

Every cell is a gap because the feature is not yet implemented.
The minimum viable test set, in priority order:

1. A pure-Python unit test of `verify_manifest` against a synthetic
   `prebuilt/` tree — runnable without any C toolchain and a hard
   prerequisite for `make test-unit`.
2. A unit test of `write_manifest` against a golden output —
   ditto.
3. An integration test (gated on `musl-gcc` availability, mirroring
   the existing `rustic`/`xorriso` gates from `CLAUDE.md:21`) that
   exercises `lcsas recovery build --target x86_64-linux` end-to-end
   on the host arch and asserts the produced binary passes the
   smoke script.
4. A reproducibility integration test that runs `recovery build`
   twice and asserts the artefacts are byte-identical.

---

## Consolidated source refs

**Planned (do not yet exist) — all [gap]:**

- `src/lcsas/cli/main.py:LINE` — `recovery` subparser + four
  `cmd_recovery_*` handlers.
- `src/lcsas/recovery/build.py` — orchestrator
  (`build_all`, `build_one`, `run_tests`, `write_manifest`,
  `verify_manifest`).
- `recovery/Makefile` — per-arch build rules.
- `recovery/scripts/detect_arch.sh` — host detection.
- `recovery/scripts/rebuild.sh` — Tier 3 fallback.
- `recovery/scripts/rebuild_rustic.sh` — Tier 4 fallback.
- `recovery/MANIFEST.sha256` — canonical artefact hashes.
- `recovery/docs/TIERS.txt` — cascade doc.
- `recovery/docs/BUILD.txt` — toolchain prerequisites.
- `tests/integration/test_recovery_orchestration.py` — full
  orchestration test.

**Existing analogs cited as reference:**

- `CLAUDE.md:71` — zero runtime dependencies rule.
- `CLAUDE.md:73` — meta-volume rationale.
- `src/lcsas/cli/main.py:377` — `meta` subparser (analog for the
  `recovery` subparser slot).
- `src/lcsas/cli/main.py:1698` — `cmd_meta_build` (analog for
  `cmd_recovery_build`).
- `src/lcsas/meta/builder.py:168` — static rustic strategy.
- `src/lcsas/meta/builder.py:1754` — static rustic bundling
  (the bit the recovery toolchain replaces with a verifiable,
  multi-arch story).
- `src/lcsas/restore/restic_fallback.py` — pure-Python last-resort
  fallback, the floor below Tier 4.
- `Makefile:15`, `Makefile:18` — test target conventions
  (`test-unit`, `test-integration`) that the recovery tests must
  slot into.
