# RST-04: Tier-3 zstd only works on the build host's arch + CPython minor

> **STATUS: RESOLVED** — landed in `b6d3a37` (test(restore): cover tier-3 no-zstandard import wiring [RST-04]); guarded by `tests/unit/test_restic_fallback.py`.

**Priority:** P1 · **Severity:** high · **Dimension:** restore-python · **Audit status:** confirmed (high confidence) · **Ledger:** tracked: partially — tests/recovery_hardening/test_tier3_pythonpath.py + test_standalone_zstandard_guard.py (host-arch / graceful-degrade only, local-only)
**Suggested GH issue title:** Vendor pure-Python zstd fallback; fail meta build loud when zstandard unbundled

## Problem

rustic v2 repos are zstd-compressed by default, and tier 3 — the bundled-CPython +
`standalone_restorer.py` last resort — has no pure-Python zstd path: without the `zstandard`
package it raises "pip install zstandard", useless advice on an air-gapped machine decades from
now. The meta builder copies the **build host's** `zstandard` package (with its
cpXY-arch-specific C-extension `.so`) into `tools/lib/pythonX.Y/`, and `restore.sh` only
re-exposes that same directory. The six per-target python-build-standalone CPython 3.12.13 trees
under `recovery/bin/<target>/python` ship no zstandard at all, and PBS 3.12 has no stdlib zstd
(`compression.zstd` landed in 3.14).

So tier-3 zstd works only when the recovery host matches the build host's arch AND CPython minor
— today x86_64-linux + 3.12, which is exactly why the blind-restore e2e passes and masks this.
On the other five approved targets (macOS arm/Intel, Windows, aarch64/armv7 Linux), or after the
build host moves to 3.13+, tier 3 cannot decompress any default-format repo. Worse, the gap is
silent at build time: `bundle_python_package` returns `None` when zstandard isn't installed on
the build host, the caller discards the return value, and the meta-volume builds "successfully"
with no zstd support and no warning.

## Evidence

Re-verified against current code (2026-06-10):

- `src/lcsas/restore/restic_fallback.py:101-110` — `except ImportError:` →
  `_decompress_zstd` raises `RuntimeError("... Install it with:\n  pip install zstandard ...")`.
- `src/lcsas/meta/builder.py:1801` — `bundler.bundle_python_package("zstandard")` — return
  value discarded inside `_bundle_tools`.
- `src/lcsas/meta/bundler.py:272-274` — `pkg_dir = self._find_installed_package(package_name);
  if pkg_dir is None: return None` — silent. `:283-296` copies host site-packages verbatim into
  `lib/python{sys.version_info...}/`, including C-extension `.so` files and their shared libs.
- `recovery/scripts/restore.sh:177-194` — issue #284 fix mirrors only
  `tools/lib/python*/zstandard` (the single host-arch/host-minor copy) into the RAM dir.
- `recovery/UPSTREAM.sha256:29-35` — pinned bare PBS CPython 3.12.13 per target; no third-party
  packages.
- Build host today: Python 3.12.x with x86_64 zstandard — exactly the masking configuration.
- Existing partial coverage: `tests/recovery_hardening/test_tier3_pythonpath.py` (host target
  only) and `test_standalone_zstandard_guard.py` (asserts graceful degrade + clear error only).
  Neither covers the other five targets; neither runs in CI (`make test-recovery-hardening` is
  local-only).

## Fix design

Two independent parts; ship both.

**Part 1 — make the gap loud (small, immediate).**
`MetaVolumeBuilder._bundle_tools` (`builder.py:~1801`): capture the return of
`bundle_python_package("zstandard")`; if `None`, raise
`MetaBuildError("zstandard is not installed on the build host — tier-3 restore of "
"zstd-compressed (default) repos would be impossible from this meta-volume. "
"Install it (pip install zstandard) or pass --allow-no-zstd to build anyway.")`.
Add `--allow-no-zstd` to the `meta build` parser (`cli/main.py:~387`). Record zstd capability
per target in the build summary / `volume_info.json` so `lcsas meta verify` (RST-05) can report
it.

**Part 2 — close the gap: vendor a pure-Python zstd decompressor.**
Port the project's own from-scratch C decoder (`recovery/src/lcsas-restore/zstd_dec.c`, already
spec-complete and coverage-gated) to a stdlib-only module
`src/lcsas/restore/_zstd_pure.py` exposing `decompress(data: bytes, max_output_size: int) ->
bytes`. Wire it as the fallback backend in `restic_fallback.py`: keep `import zstandard` as the
fast path; on ImportError set `_decompress_zstd` to the pure implementation instead of the
raise-RuntimeError stub (keep a clear log line: "using slow built-in zstd — install
'zstandard' for ~100x faster restore"). `standalone_builder.py` concatenates `_zstd_pure.py`
into the generated script the same way it does `_aes_pure.py`. Chosen over bundling six
per-target `zstandard` wheels because (a) upstream publishes no armv7 wheel, so the matrix can't
be completed that way, and (b) a pure-Python path also works under any *system* Python — which
is tier-3's entire pitch. Speed is acceptable: tier-3 is already ~1 MB/s pure-Python AES.

Decoder scope: decompression only, single-segment frames as written by restic/rustic; reject
unsupported features loudly (dictionaries, etc.). Validate against the same test vectors as
`zstd_dec.c` (`recovery/tests/test_zstd.c` corpus) plus round-trips against the `zstandard`
compressor.

No catalog/schema impact. Already-burned meta discs remain host-arch-only for tier-3 zstd; the
remedy is burning a new meta disc — note this in `recovery/docs/TIERS.txt` tier-3 section.

## Tests & gates

- `tests/unit/test_meta_builder.py::test_build_fails_when_zstandard_unbundleable` — monkeypatch
  `ToolBundler._find_installed_package` to return None; assert `MetaVolumeBuilder.build()`
  raises (and `--allow-no-zstd` suppresses). Always-on (`make test-unit`).
- `tests/unit/test_zstd_pure.py` — vector tests shared with the C decoder corpus; round-trip
  property tests vs `zstandard` compressor at several levels/sizes; reject-unsupported tests.
  Always-on.
- `tests/unit/test_restic_fallback.py::test_zstd_fallback_used_without_zstandard` — monkeypatch
  `_HAS_ZSTD=False` path to the pure backend; full snapshot restore of a compressed fixture
  succeeds. Update `test_standalone_zstandard_guard.py`: the guard now asserts degrade-to-slow,
  not raise.
- `tests/recovery_hardening/test_tier3_zstd_portability.py` — static check over a built
  meta-volume: for each `recovery/bin/<target>/python` tree, assert a zstd backend importable by
  THAT interpreter exists (pure-Python module counts for all; any bundled `.so` must match the
  target's cpXY/arch tags). Local lane now; CI wiring belongs to the GATE plans.
- Cross-arch proof (opt-in, mirrors the existing qemu verify loop in
  tests/recovery_hardening/test_tier1_aarch64_qemu.py): run `standalone_restorer.py` under the
  bundled aarch64 PBS python via qemu-user against a zstd-compressed fixture repo; assert
  restore succeeds. Makefile target `blind-restore`-adjacent or a new
  `test-tier3-qemu` target.

## Acceptance criteria

- [ ] `lcsas meta build` on a host without `zstandard` fails with the actionable message;
      `--allow-no-zstd` overrides and the gap is recorded in the build output.
- [ ] A zstd-compressed fixture repo restores via `standalone_restorer.py` with the `zstandard`
      package absent (pure backend), byte-identical to the source.
- [ ] Same restore succeeds under the bundled aarch64 PBS interpreter via qemu (opt-in test).
- [ ] `make test-unit && make lint && make typecheck` pass;
      `pytest tests/recovery_hardening/test_tier3_zstd_portability.py` passes on a built meta tree.

## Dependencies & related plans

- RST-05 (meta completeness gate) consumes Part 1's per-target capability record; land Part 1
  first or together.
- GATE plans own putting test_tier3_zstd_portability + the qemu variant into scheduled CI.
- T1C zstd decoder plans (if any) share the vector corpus — keep `_zstd_pure.py` and
  `zstd_dec.c` validated against the same fixtures.

## Effort

3 days: 0.5 Part 1 + tests, 2 decoder port + vector tests (the C decoder is the reference, so
this is translation + validation, not research), 0.5 qemu/portability wiring. Needs qemu-user
(already used by the tier-1 cross-arch tests on this machine).

---
**Implemented:** 2026-06-13. Both parts shipped, with one deviation: the plan's premise that a "from-scratch C decoder" (`zstd_dec.c`) existed to translate is wrong — that file is a 70-line wrapper around the full vendored upstream zstd v1.5.6 library, so `_zstd_pure.py` was written against RFC 8878 directly (predefined FSE DTables + OF_base extracted verbatim from `recovery/vendored/zstd` for self-consistency) and validated by ~12k round-trips against the `zstandard` compressor (all levels/sizes/checksums) plus the C decoder's vector corpus. Part 1 (loud meta-build failure + `--allow-no-zstd` + per-target `zstd_support` in volume_info.json) and Part 2 (pure decoder wired as the no-native-zstd fallback, inlined into standalone_restorer.py) both as planned. qemu cross-arch test is opt-in/self-skipping (needs an aarch64 CPython at `LCSAS_AARCH64_PYTHON`; none committed in-repo).
