# Recovery Toolchain Build & Manifest

The **recovery toolchain** is the in-house C89 + POSIX-sh tier-1
restore path, plus the cross-build, manifest, and verify tooling that
keeps it durable.  It is driven by `lcsas recovery {build,test,manifest,verify}`
(`cmd_recovery` in `src/lcsas/cli/main.py`) over the on-disk
`recovery/` build tree, orchestrated by `RecoveryBuilder`
(`src/lcsas/recovery/build.py`).  For the wider architecture and the
holographic-catalog model, see [`docs/architecture.md`](../architecture.md).

Sibling docs: [`docs/workflows/meta-volume.md`](meta-volume.md) (how
these binaries get bundled onto a rescue disc),
[`docs/workflows/verify-and-audit.md`](verify-and-audit.md) (the
disc-integrity / ECC verify flow), and the on-disc tier doc
`recovery/docs/TIERS.txt`.

## Why a recovery toolchain exists

LCSAS is built on a strict **zero runtime dependencies** rule
(`CLAUDE.md`): the Python codebase uses only the standard library
plus an optional `zstandard`, so a fresh machine can decrypt and
extract a Rustic repo with nothing but a Python 3 interpreter
(`src/lcsas/restore/restic_fallback.py`).  The meta-volume goes
further — it bundles static binaries (rustic, xorriso, python3, and
our own tier-1 binaries) so a bare machine with no network and no
package manager can still drive the full restore pipeline.

The **recovery toolchain** is the most durable layer of that story:
a minimal restore reader written in **C89**, compiled against
**vendored** sqlite + zstd C source we ship and audit ourselves
(`recovery/vendored/`, pinned in `recovery/MANIFEST.sha256`).  Its
only runtime dependency is a kernel + a static libc — no Rust, no
Python, no third-party shared library.  The `recovery/` tree gives us:

1. The **C89 source** for the restore reader (`recovery/src/`), which
   decrypts and decompresses Rustic packs and authenticates every
   blob (Poly1305 MAC + SHA-256 content hash), rejecting corrupt data
   rather than restoring it.
2. A small family of companion C89 binaries built by the same
   `recovery/Makefile`: `lcsas-restore` (the reader), `lcsas-iso9660`
   (a Linux ISO mounter for hosts without a loopback mount),
   `lcsas-init` (a Linux PID-1 for the no-OS path), `lcsas-keyshare`
   (SLIP-0039 share combiner), and **`lcsas-ecc`** — the in-house
   RS03 ECC verify/repair tool (FMT-01) that lets the disc-integrity
   layer repair bit-rot with *nothing* installed (no dvdisaster, no
   ddrescue).  Its parity format is dvdisaster's RS03, unchanged.
3. **Cross-built static binaries** for every supported target arch
   under `recovery/bin/<arch>/`, built with reproducible flags
   (`SOURCE_DATE_EPOCH` pinned) so each artefact's SHA-256 is stable
   across rebuilds.
4. A **SHA-256 manifest** (`recovery/MANIFEST.sha256`) covering the
   source + scripts that ships on every meta-disc, so an operator
   restoring decades from now can verify nothing has bit-rotted or
   been tampered with.

The full tier cascade is documented on-disc at
`recovery/docs/TIERS.txt` and summarised in
[the Tier cascade](#the-tier-123-fallback-cascade) section below.

## Table of contents

- [Why a recovery toolchain exists](#why-a-recovery-toolchain-exists)
- [Layout of the `recovery/` tree](#layout-of-the-recovery-tree)
- [Supported target arches](#supported-target-arches)
- [`lcsas recovery build` — build the recovery binaries](#lcsas-recovery-build--build-the-recovery-binaries)
- [`lcsas recovery test` — run the recovery test suite](#lcsas-recovery-test--run-the-recovery-test-suite)
- [`lcsas recovery manifest` — produce the SHA-256 manifest](#lcsas-recovery-manifest--produce-the-sha-256-manifest)
- [`lcsas recovery verify` — reproducible-build check](#lcsas-recovery-verify--reproducible-build-check)
- [The Tier 1→2→3 fallback cascade](#the-tier-123-fallback-cascade)
- [Architecture detection (`restore.sh` / `detect_arch.sh`)](#architecture-detection-restoresh--detect_archsh)
- [Cross-cutting variant matrix](#cross-cutting-variant-matrix)
- [Test coverage summary](#test-coverage-summary)
- [Consolidated source refs](#consolidated-source-refs)

## Layout of the `recovery/` tree

The tree exists and is the source of truth (`recovery/` in the repo
root):

```
recovery/
├── Makefile                       # C89 build rules + per-arch cross targets
├── MANIFEST.sha256                # checked-in hashes of source + scripts
├── UPSTREAM.sha256                # pinned hashes of upstream rustic + CPython
├── VERSION  TOOLCHAIN             # pinned toolchain provenance
├── src/                           # C89 source: lcsas-restore, lcsas-iso9660,
│   │                              #   lcsas-init, lcsas-keyshare, lcsas-ecc/
│   └── lcsas-ecc/                 # in-house RS03 verify/repair (FMT-01)
├── vendored/                      # sqlite + zstd C source we compile ourselves
├── bin/                           # cross-built static binaries, one dir per arch
│   ├── x86_64/  aarch64/  armv7/
│   ├── x86_64-windows/
│   └── x86_64-macos/  aarch64-macos/
├── build/                         # transient build objects (NOT in the manifest)
├── scripts/
│   ├── restore.sh  restore_auto.sh  restore.bat   # the tier dispatchers
│   ├── detect_arch.sh             # (uname -s, uname -m) → target arch
│   └── fetch_upstream.sh          # pinned rustic + CPython downloader
├── tests/                         # C unit tests + bare-path / fault-inject gates
└── docs/
    ├── TIERS.txt                  # the tier cascade (+ disc-integrity layer)
    ├── BUILD.txt                  # toolchain prerequisites
    └── RECOVER.txt / RECOVER_WINDOWS.txt / READINESS_CHECKLIST.txt / ...
```

The Python orchestrator lives at `src/lcsas/recovery/build.py`
(`RecoveryBuilder`); its CLI surface is the `recovery` subparser in
`build_parser()` (`src/lcsas/cli/main.py`), dispatched by
`cmd_recovery`.

## Supported target arches

The `--arch` choices are `host` plus the eight
`RecoveryBuilder.SUPPORTED_ARCHES` cross targets — nine in all:

| `--arch` value   | Toolchain | Notes |
|------------------|-----------|-------|
| `host`           | host `cc` | default; builds for the build machine's arch |
| `x86_64`         | `x86_64-linux-musl-gcc` or `zig cc` | Linux x86_64, static |
| `aarch64`        | `aarch64-linux-musl-gcc` or `zig cc` | Linux ARM64, static |
| `armv7`          | `armv7-linux-musleabihf-gcc` or `zig cc` | Linux 32-bit ARM hardfloat |
| `riscv64`        | `riscv64-linux-musl-gcc` or `zig cc` | Linux RISC-V, static |
| `x86_64-windows` | `zig cc -target x86_64-windows-gnu` | `.exe`; no `lcsas-init` |
| `aarch64-windows`| `zig cc -target aarch64-windows-gnu` | `.exe`; no `lcsas-init` |
| `x86_64-macos`   | `zig cc -target x86_64-macos` | Mach-O; no `-static`; no `lcsas-iso9660`/`lcsas-init` |
| `aarch64-macos`  | `zig cc -target aarch64-macos` | Mach-O; no `-static`; no `lcsas-iso9660`/`lcsas-init` |

Of these, **six are the APPROVED_TARGETS** the meta-volume's
required-contents contract demands a tier-1 binary for
(`src/lcsas/meta/required_contents.py`): the three Linux musl targets
(`x86_64`, `aarch64`, `armv7`), the two macOS targets, and
`x86_64-pc-windows-gnu`.  `riscv64` and `aarch64-windows` build but
are not (yet) part of the required-contents contract.  See
[`../CROSS_PLATFORM_META_RFC.md`](../CROSS_PLATFORM_META_RFC.md) §6 Q6.

Source refs: `RecoveryBuilder.SUPPORTED_ARCHES`,
`RecoveryBuilder._DEFAULT_CC`, `RecoveryBuilder.cross_build`
(`src/lcsas/recovery/build.py`); `APPROVED_TARGETS`
(`src/lcsas/meta/required_contents.py`); `recovery/Makefile`
(per-arch `bin/<arch>/lcsas-*` targets, incl. `lcsas-ecc`).

---

## `lcsas recovery build` — build the recovery binaries

**Purpose:** Compile the C89 recovery binaries (`lcsas-restore` and
its companions, incl. `lcsas-ecc`) for one target — the host arch by
default, or a cross-target via `--arch`.  This is what a release
engineer runs to populate `recovery/bin/<arch>/` before bundling a
meta-disc.  Recovery *operators* never rebuild at restore time: if a
bundled binary won't run, the on-disc cascade falls through to the
next tier.

**Synopsis:**

```bash
lcsas recovery build [--arch ARCH] [--cc CC] [--verbose]
```

- `--arch` — one of `RecoveryBuilder.SUPPORTED_ARCHES` (see
  [Supported target arches](#supported-target-arches)); default `host`.
- `--cc` — override the C compiler (default: `cc` for host,
  `<arch>-linux-musl-gcc` for Linux cross, or a `zig cc -target ...`
  invocation you supply for any target).
- `--verbose` / `-v` — show full build output.

**Prerequisites:**

- `make` + a host `cc` for `--arch host`.
- For Linux cross targets: the matching musl-cross-make prefix
  (`x86_64-linux-musl-gcc`, `aarch64-linux-musl-gcc`,
  `armv7-linux-musleabihf-gcc`, `riscv64-linux-musl-gcc`) **or**
  `zig cc` passed via `--cc`.
- For Windows / macOS targets: `zig cc` (the Makefile's dedicated
  per-target rules invoke `zig cc -target ...`; no mingw or Apple SDK
  required).
- Prerequisite detail is mirrored in `recovery/docs/BUILD.txt`.

**Steps:**

1. CLI parses `lcsas recovery build` and dispatches to `cmd_recovery`
   (`src/lcsas/cli/main.py`), which constructs a `RecoveryBuilder`
   rooted at `recovery/`.
2. For `--arch host`, `RecoveryBuilder.build_host()` runs
   `make -C recovery all` for the detected host arch.
3. For a cross `--arch`, `RecoveryBuilder.cross_build(arch, cc=...)`:
   - validates `arch` against `SUPPORTED_ARCHES`;
   - exports a pinned `SOURCE_DATE_EPOCH` (default `1735689600`) for
     reproducibility;
   - **Linux** targets: `make ... all CC=<cc> BUILD=build/<arch>
     LDFLAGS=-static`, then copies `lcsas-restore`, `lcsas-iso9660`,
     `lcsas-init` into `bin/<arch>/`;
   - **Windows / macOS** targets: invoke the Makefile's dedicated
     `bin/<arch>/lcsas-restore[.exe]` rule (which encodes the
     `zig cc -target ...` call); Windows binaries get `.exe` and no
     `lcsas-init`; macOS binaries are Mach-O, non-`-static`, with no
     `lcsas-iso9660`/`lcsas-init`.
4. The handler prints the produced `lcsas-restore` path plus any
   `lcsas-iso9660` / `lcsas-init` that the target produced, and
   returns 0.

The `recovery/Makefile` `all:` target builds the full family per arch:
`lcsas-restore`, `lcsas-iso9660`, `lcsas-init`, `lcsas-keyshare`, and
`lcsas-ecc` (the in-house RS03 verify/repair tool, FMT-01).

**Expected outcome:** Fresh binaries under `recovery/bin/<arch>/`
(`lcsas-restore` always; `lcsas-ecc` and the others where the target
supports them).  `lcsas recovery build` does **not** rewrite the
manifest — run `lcsas recovery manifest` for that.  Exit code 0.

**Variant axes that apply:**

- Target architecture: **one per invocation** (`--arch`); use
  `make build-recovery` / a CI matrix to fan out all targets.
- Recovery tier: produces the **Tier 1** binaries.
- Reproducibility: yes — `cross_build` pins `SOURCE_DATE_EPOCH`; the
  Makefile strips build-id and absolute paths.

**Test coverage:**

- The cross-build path is exercised in CI by the recovery-build /
  audit-gate workflows (zig cc, with qemu/wine verify for the
  non-host targets); see `recovery/docs/READINESS_CHECKLIST.txt`.
- C-side behaviour is covered by `make -C recovery test`
  (see [`recovery test`](#lcsas-recovery-test--run-the-recovery-test-suite)).

**Source refs:** `cmd_recovery` (`src/lcsas/cli/main.py`);
`RecoveryBuilder.build_host` / `RecoveryBuilder.cross_build`
(`src/lcsas/recovery/build.py`); `recovery/Makefile` (`all:` +
per-arch `bin/<arch>/lcsas-*` rules); `recovery/docs/BUILD.txt`.

---

## `lcsas recovery test` — run the recovery test suite

**Purpose:** Run the recovery toolchain's own test suite — the C unit
tests plus the round-trip gates that prove a freshly built
`lcsas-restore` decrypts a known fixture repo and emits the expected
plaintext, and that the bare (Python-free) path works.  This is the
"does this binary actually do what it claims" check the manifest alone
cannot provide.

**Prerequisites:** A built host-arch toolchain (run `lcsas recovery
build` first) and a host `make`/`cc`.

**Steps:**

1. CLI parses `lcsas recovery test` and dispatches to `cmd_recovery`
   (`src/lcsas/cli/main.py`).
2. The handler calls `RecoveryBuilder.run_tests(verbose=...)`
   (`src/lcsas/recovery/build.py`), which shells out to
   `make -C recovery test`.
3. The Makefile `test` target builds and runs the C unit tests and
   the round-trip / bare-path gates under `recovery/tests/`
   (e.g. `test_bare_path.sh`, which strips every `python*` from PATH
   and asserts recovery still succeeds via tiers 1–2).
4. A non-zero exit from `make test` propagates up; the CLI prints
   `recovery tests: OK` on success.

**Expected outcome:** Exit 0 and `recovery tests: OK`; exit 1 with the
failing test output otherwise.

**Variant axes that apply:**

- Target architecture: **host arch** (the C tests build and run
  locally; cross-arch behaviour is checked under qemu/wine in CI).
- Recovery tier: validates **Tier 1** (and the tier-1→2 bare path).
- Reproducibility: n/a (tests behaviour, not bytes).

**Test coverage:** the suite *is* the coverage — `make -C recovery
test`, plus the always-on no-dvdisaster ECC self-repair gate
(`tests/e2e/test_ecc_selfrepair_no_dvdisaster.py`, FMT-01) and the
bare-path gate referenced in `recovery/docs/TIERS.txt`.

**Source refs:** `cmd_recovery` (`src/lcsas/cli/main.py`);
`RecoveryBuilder.run_tests` (`src/lcsas/recovery/build.py`);
`recovery/Makefile` (`test:` target); `recovery/tests/`.

---

## `lcsas recovery manifest` — produce the SHA-256 manifest

**Purpose:** Hash every tracked file under `recovery/` and write the
result to `recovery/MANIFEST.sha256` (one `sha256sum`-format line per
file, sorted by path).  The manifest covers the **source + scripts**
we ship and audit — it deliberately **excludes** the transient
`build/` tree — so an operator decades from now can confirm the C
source they're about to compile hasn't bit-rotted or been tampered
with.

**Prerequisites:** The `recovery/` tree (no build required — the
manifest covers source, not binaries).

**Steps:**

1. CLI parses `lcsas recovery manifest [--output PATH]` and dispatches
   to `cmd_recovery` (`src/lcsas/cli/main.py`).
2. The handler calls `RecoveryBuilder.write_manifest(output)`
   (`src/lcsas/recovery/build.py`), defaulting to
   `recovery/MANIFEST.sha256`.
3. `write_manifest` walks `recovery/` with `os.walk`, skipping
   `build/`, `__pycache__/`, `.git/`, and the manifest file itself;
   hashes each file with stdlib `hashlib.sha256`; emits
   `<hex>  <relpath>` lines sorted by path for deterministic diffs.
4. The CLI prints `wrote <path> (<N> files)`.

**Expected outcome:** A `recovery/MANIFEST.sha256` covering every
source/script file under `recovery/` (not the cross-built binaries,
which are reproducible-but-rebuildable).  Exit 0.

**Variant axes that apply:**

- Target architecture: n/a — the manifest is arch-independent source.
- Recovery tier: produces the artefact the meta-disc ships so a future
  operator can audit the source before rebuilding **Tier 1**.
- Reproducibility: the manifest is itself deterministic (sorted walk).

**Note:** upstream opaque binaries (rustic, CPython) are pinned
separately in `recovery/UPSTREAM.sha256`; the meta-volume's bundled
copy is audited by `lcsas meta verify` against the merged
`recovery/MANIFEST.sha256` on the built volume (see
[`meta-volume.md`](meta-volume.md)).

**Source refs:** `cmd_recovery` (`src/lcsas/cli/main.py`);
`RecoveryBuilder.write_manifest` (`src/lcsas/recovery/build.py`);
`recovery/MANIFEST.sha256`; `recovery/UPSTREAM.sha256`.

---

## `lcsas recovery verify` — reproducible-build check

**Purpose:** Prove the recovery toolchain builds **reproducibly** —
build it twice and byte-compare the output.  Without this the SHA-256
manifest would be worthless: any innocuous variation (timestamp, debug
path, build-id) would invalidate the recorded hashes on every rebuild,
and an operator could not distinguish an expected rebuild from
tampering.

**Prerequisites:** A host toolchain (`make`, `cc`) able to build the
recovery tree twice.

**Steps:**

1. CLI parses `lcsas recovery verify` and dispatches to `cmd_recovery`
   (`src/lcsas/cli/main.py`).
2. The handler shells out to `make -C recovery repro-check`, whose
   exit code is returned verbatim.
3. The `repro-check` target builds the toolchain twice (pinning
   `SOURCE_DATE_EPOCH`, stripping build-id and absolute build paths)
   and byte-compares the two outputs; any difference is a hard fail.

**Expected outcome:** Exit 0 when the two builds are byte-identical;
non-zero (the make target's code) otherwise.

**Variant axes that apply:**

- Target architecture: the host build (the reproducibility property is
  required to hold for every target; cross-target repro is checked in
  CI).
- Recovery tier: this is what makes **Tier 1 trustworthy**.
- Reproducibility: this *is* the reproducibility check.

**Source refs:** `cmd_recovery` (`src/lcsas/cli/main.py`);
`recovery/Makefile` (`repro-check:` target).

---

## The Tier 1→2→3 fallback cascade

**Purpose:** Define how the on-disc dispatcher (`recovery/scripts/restore.sh`,
or `restore.bat` on Windows) chooses *which* tool reads the bytes.
There is **no bootable live environment** (the Alpine live stack was
removed — BOOT-07; LCSAS discs are NOT bootable).  The operator runs
`restore.sh` from a meta-disc mounted on an existing OS — or, on a
machine with no OS at all, from any other computer / a live Linux USB
(see `recovery/docs/BOOT.txt`).  The dispatcher tries the tiers in
order and stops at the first that succeeds.  The canonical text ships
on-disc at `recovery/docs/TIERS.txt`.

| Tier | Source                                          | What runs                                              | Runtime deps              |
|------|-------------------------------------------------|--------------------------------------------------------|---------------------------|
| 1 (primary)    | `recovery/bin/<arch>/lcsas-restore`   | Our in-house C89 reader (vendored sqlite+zstd)         | kernel + static libc      |
| 2 (fallback)   | `recovery/bin/<arch>/rustic-static`   | Pinned upstream static `rustic`                        | kernel + static libc      |
| — | **BARE-MINIMUM PATH ENDS HERE** (tiers 1–2 are Python-free) | | |
| 3 (last resort)| bundled CPython + `standalone_restorer.py` | Pure-Python AES/zstd restorer                     | python3 ≥ 3.7 (stdlib only)|

**Steps (`restore.sh` chooses a tier):**

1. `restore.sh` resolves the host target with the same
   `(uname -s, uname -m)` mapping as `detect_arch.sh`
   (overridable via `$LCSAS_TARGET`).
2. **Tier 1** — runs `recovery/bin/<arch>/lcsas-restore` if present.
   This is the durable path: C89, ABI-stable, depends only on a kernel
   + static libc.  It authenticates every blob (Poly1305 MAC + SHA-256
   content hash) and *rejects* corrupt data.
3. **Tier 2** — falls back to the pinned upstream `rustic-static` for
   the same arch (`recovery/UPSTREAM.sha256`).  Same kernel + libc
   baseline; a hedge in case tier 1 won't run on a given host.
4. **Tier 3** — last resort: the bundled CPython runs
   `standalone_restorer.py` (the pure-Python AES/zstd decoder).  Gated
   by `LCSAS_ALLOW_PYTHON_TIER` (default 1); set it to `0` to forbid
   the Python tier entirely so recovery is provably C-only.

Beneath the cascade sits the **disc-integrity layer**: DVDisaster RS03
ECC (or the in-house `lcsas-ecc`, FMT-01) repairs bit-rotted sectors
*before* any tier reads them, and tier 1 then authenticates and
rejects anything still corrupt — so disc damage is repaired-or-rejected,
never silently restored.  See
[`verify-and-audit.md`](verify-and-audit.md) and
`recovery/docs/TIERS.txt` "DISC-INTEGRITY LAYER".

**Expected outcome:** the first tier that succeeds yields a working
restore.  If all fail, the operator has a hardware or environment
problem (`recovery/docs/RECOVER.txt`).

**Variant axes that apply:**

- Target architecture: the dispatcher selects `bin/<arch>/` per the
  detected host.
- Recovery tier: this section *is* the tier definition.
- Reproducibility: only **Tier 1** is built by us and reproducible.

**Test coverage:**

- Bare path (tiers 1–2, Python-free): `recovery/tests/test_bare_path.sh`
  (`make -C recovery test-bare-path`).
- No-dvdisaster ECC self-repair (FMT-01):
  `tests/e2e/test_ecc_selfrepair_no_dvdisaster.py`.
- Tier 3 pure-Python fallback: `tests/unit/test_restic_fallback.py`
  and the standalone-restorer integration tests.
- Docs-vs-reality gate: `tests/recovery_hardening/test_boot_docs_reality.py`
  (no phantom `lcsas` subcommands/flags in `recovery/docs/`).

**Source refs:** `recovery/docs/TIERS.txt`;
`recovery/scripts/restore.sh` / `restore.bat`;
`standalone_restorer.py` (Tier 3, generated by
`src/lcsas/restore/standalone_builder.py`);
`src/lcsas/restore/restic_fallback.py`.

---

## Architecture detection (`restore.sh` / `detect_arch.sh`)

**Purpose:** Map the host's `uname` output to one of the supported
target arches the rest of the toolchain understands.  At restore time
this lives inside `restore.sh`'s dispatcher; `detect_arch.sh` is the
standalone equivalent used by tests and tooling.

**Prerequisites:** POSIX `sh` and `uname`.  No Python.

**Steps:**

1. The dispatcher runs `uname -s` and `uname -m`.
2. Maps the pair to a `bin/<arch>/` subtree, e.g.:
   - `Linux x86_64` → `x86_64`
   - `Linux aarch64` / `arm64` → `aarch64`
   - `Linux armv7l` → `armv7`
   - `Linux riscv64` → `riscv64`
   - `Darwin arm64` → `aarch64-macos`; `Darwin x86_64` → `x86_64-macos`
   - Windows (`restore.bat`) → `x86_64-windows`
3. Override with `$LCSAS_TARGET=<arch>` if auto-detection misfires
   (chroot, foreign-arch emulator, unusual `uname`).
4. Unknown pairs fail with a diagnostic so the operator can pick a
   target explicitly.

**Expected outcome:** a single target line and exit 0, or a diagnostic
non-zero exit.

**Test coverage:** `tests/unit/test_restore_sh_dispatcher.py` covers
the full `(OS, machine)` matrix and explicit rejections.

**Source refs:** `recovery/scripts/restore.sh` (dispatcher);
`recovery/scripts/detect_arch.sh`;
`tests/unit/test_restore_sh_dispatcher.py`.

---

## Cross-cutting variant matrix

| Workflow                  | Arch axis        | Tier axis      | Reproducibility |
|---------------------------|------------------|----------------|-----------------|
| `lcsas recovery build`    | one (`--arch`)   | builds T1      | yes             |
| `lcsas recovery test`     | host arch        | T1 + bare path | n/a (behaviour) |
| `lcsas recovery manifest` | arch-independent | audits T1 source | yes (sorted walk) |
| `lcsas recovery verify`   | host build       | makes T1 safe  | yes (the check) |
| Tier cascade (`restore.sh`)| per host        | T1→T2→T3       | only T1         |
| Arch detection            | dispatcher       | feeds all      | yes (pure sh)   |

---

## Test coverage summary

| Area | Test |
|------|------|
| Cross-build / per-arch binaries | recovery-build + audit-gate CI (zig cc + qemu/wine verify); `recovery/docs/READINESS_CHECKLIST.txt` |
| C-side behaviour | `make -C recovery test` (`RecoveryBuilder.run_tests`) |
| Bare (Python-free) path | `recovery/tests/test_bare_path.sh` (`make -C recovery test-bare-path`) |
| In-house RS03 ECC (FMT-01) | `tests/e2e/test_ecc_selfrepair_no_dvdisaster.py` + cross-conformance vs dvdisaster |
| Reproducibility | `make -C recovery repro-check` (`lcsas recovery verify`) |
| Tier-3 pure-Python | `tests/unit/test_restic_fallback.py` + standalone-restorer integration |
| Arch dispatcher | `tests/unit/test_restore_sh_dispatcher.py` |
| Docs-vs-reality | `tests/recovery_hardening/test_boot_docs_reality.py` |

---

## Consolidated source refs

- `cmd_recovery` (`src/lcsas/cli/main.py`) — `recovery` subparser +
  dispatch for `build`/`test`/`manifest`/`verify`.
- `RecoveryBuilder` (`src/lcsas/recovery/build.py`) — `build_host`,
  `cross_build`, `run_tests`, `write_manifest`, `SUPPORTED_ARCHES`,
  `_DEFAULT_CC`.
- `recovery/Makefile` — `all:` (per-arch `lcsas-restore` /
  `lcsas-iso9660` / `lcsas-init` / `lcsas-keyshare` / `lcsas-ecc`),
  `test:`, `test-bare-path:`, `repro-check:`, and per-arch
  `bin/<arch>/lcsas-*` rules.
- `recovery/MANIFEST.sha256` — source/script hashes (manifest output).
- `recovery/UPSTREAM.sha256` — pinned upstream rustic + CPython hashes.
- `recovery/scripts/restore.sh` / `restore.bat` / `detect_arch.sh` —
  the on-disc tier dispatchers.
- `recovery/docs/TIERS.txt`, `recovery/docs/BUILD.txt`,
  `recovery/docs/RECOVER.txt` — on-disc operator/tier docs.
- `APPROVED_TARGETS` (`src/lcsas/meta/required_contents.py`) — the six
  targets the meta-volume contract requires a tier-1 binary for.
- `src/lcsas/restore/restic_fallback.py` /
  `src/lcsas/restore/standalone_builder.py` — Tier 3 pure-Python path.
- [`docs/architecture.md`](../architecture.md) — system overview;
  [`../CROSS_PLATFORM_META_RFC.md`](../CROSS_PLATFORM_META_RFC.md) §6
  Q6 — cross-platform tier-1 rationale.
