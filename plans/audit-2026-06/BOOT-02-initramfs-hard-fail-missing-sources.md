# BOOT-02: build_initramfs.sh must hard-fail on missing sources, not ship zero-byte placeholders

**Priority:** P1 · **Severity:** high · **Dimension:** boot-live-distro · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Hard-fail initramfs build on missing sources; fix false Phase-2 claims

## Problem

`recovery/boot/initramfs/build_initramfs.sh` assembles the recovery initramfs from
`manifest.txt`. When a manifest source file is missing it prints a WARN to stderr,
writes a **zero-byte placeholder** at the target path, and continues — exiting 0
and printing "wrote <out>". The manifest requires `bin/{{ARCH}}/busybox`, which
exists for **no architecture** (`recovery/bin/<arch>/` ships only
lcsas-restore/iso9660/init/keyshare). The audit reproduced this live: a 3.4 MB
initramfs containing a 0-byte `/bin/busybox` with 15 symlinks pointing at it,
produced with rc=0. If that image were ever booted, `lcsas-init` (PID 1) would fail
every `execl("/bin/busybox", ...)`, fall through to `/bin/sh` (a symlink to the
same empty file), log "FATAL: no shell available" and spin in an infinite sleep —
a black screen for the heir. For an unbuilt arch like riscv64 the script would
placeholder *every* binary including `/init` itself (kernel panic).

This is the "silent success" anti-pattern in the one place the project's doctrine
forbids it: a build step that cannot produce a working artifact must fail loud.
Compounding it, `recovery/docs/README.txt` claims "Userland: BusyBox static
(Phase 2)" and "Phase 2 (Hardening): COMPLETE" — a reader auditing recovery
readiness is told a userland exists that was never vendored. The boot path is
being dropped (BOOT-01), but the script and manifest survive in
`experimental/boot/` as the revival starting point, and a revival that begins from
a silently-lying build script reproduces the entire failure class.

## Evidence

(Re-checked 2026-06-10. Paths are pre-BOOT-01-move; after the move, prefix
`experimental/` instead of `recovery/`.)

- `recovery/boot/initramfs/build_initramfs.sh:42-48` — the `f` case:
  ```sh
  if [ ! -f "$src" ]; then
      printf 'WARN: missing source %s; placeholder zero-byte file\n' "$src" >&2
      mkdir -p "$STAGING$(dirname "$target")"
      : > "$STAGING$target"
  ```
  then `set -eu` never trips; script ends `printf 'wrote %s (%s bytes)\n' ...`.
- `recovery/boot/initramfs/manifest.txt:32` — `f bin/{{ARCH}}/busybox  /bin/busybox  0755`;
  `ls recovery/bin/x86_64/` → `lcsas-init lcsas-iso9660 lcsas-keyshare lcsas-restore`
  (no busybox under any arch dir).
- `recovery/src/lcsas-init/init.c:136-155` — handoff execs `/bin/busybox` then
  `/bin/sh`; on failure logs FATAL and `for(;;) sleep(60);`.
- `src/lcsas/meta/bootable.py:116-134` — `_validate_inputs` only checks
  `initramfs-{arch}.cpio.gz` `is_file()`; a placeholder-riddled archive passes.
- `recovery/docs/README.txt:55` — "Userland: BusyBox static (Phase 2)"; `:67` —
  "Phase 2 (Hardening): COMPLETE".

## Fix design

**A. Hard-fail in `build_initramfs.sh`** (file lives at
`experimental/boot/initramfs/build_initramfs.sh` after BOOT-01). Replace the
placeholder branch:

```sh
f)
    src="$ROOT/$1"; target="$2"; mode="$3"
    if [ ! -f "$src" ] || [ ! -s "$src" ]; then
        printf 'ERROR: manifest source missing or empty: %s (for %s)\n' \
            "$src" "$target" >&2
        printf 'ERROR: refusing to build an initramfs with placeholder files.\n' >&2
        exit 1
    fi
    ...
```

Note `! -s` also rejects present-but-empty sources. Because the trap already
removes `$STAGING` and `$OUT` is only written at the very end, exiting before the
cpio step guarantees no output file appears. Add a final guard anyway (defense in
depth): after writing `$OUT`, verify the cpio contains no zero-byte regular file
(`cpio -tv` listing, or skip if cpio lacks `-tv` — the Python test below is the
authoritative check).

**B. Reject placeholder archives in `BootableISOBuilder._validate_inputs`**
(module location per BOOT-07; the check goes wherever the class lives). Add helper:

```python
def _assert_no_empty_regular_files(cpio_gz: Path) -> None:
    """Parse the gzipped newc cpio; raise ValueError on any 0-byte regular file."""
```

Pure-stdlib: `gzip.decompress`, walk 110-byte `070701` newc headers, read
`c_filesize`/`c_mode`; raise listing offending paths. ~40 lines, no subprocess.
Call it for the recovery-mode initramfs in `_validate_inputs`.

**C. Fix the false claims in `recovery/docs/README.txt`.** `:55` →
"Userland: none vendored (BusyBox was planned, never added; boot stack is
experimental — see experimental/boot/)". `:67` Phase-2 block: strike or annotate
the lcsas-init bullet ("C89 init exists; no userland or kernel was ever built").
Same PR as A — the audit recommendation explicitly pairs them.

No catalog/schema impact.

## Tests & gates

- `tests/recovery_hardening/test_initramfs_manifest_sources.py` (new, always-on;
  `pytest.mark.skipif` only on missing `cpio`/`gzip` binaries):
  - `test_missing_source_fails_loud` — run the script as-is against the real repo
    tree (busybox absent for every arch): assert rc != 0, stderr contains
    `ERROR: manifest source missing`, and OUT was not created. This is the live
    repro inverted; it fails on the pre-fix script (rc=0).
  - `test_complete_tree_builds_clean` — synthesize a `RECOVERY_ROOT` in tmp_path
    with every manifest `f` source as a 1-byte file; run the script; assert rc=0
    and (parsing the cpio with the same newc walker as Fix B) no zero-byte
    regular file exists in the archive.
  - `test_validate_inputs_rejects_placeholder_cpio` — build a cpio.gz containing
    one 0-byte regular file; assert `_assert_no_empty_regular_files` raises (skip
    if BOOT-07 deleted the builder class; keep the parser-level test regardless).
- Wire a `make -C recovery initramfs-check` target invoking the first test's
  script-level check only if the script remains under `recovery/`; after the
  BOOT-01 move, the pytest gate is the single home (avoid two drifting harnesses).
- Runs in `make test-recovery-hardening`; CI wiring per GATE-02.

## Acceptance criteria

- [ ] `bash experimental/boot/initramfs/build_initramfs.sh x86_64 /tmp/out.cpio.gz`
      exits non-zero on the current tree and creates no `/tmp/out.cpio.gz`.
- [ ] With a complete synthetic source tree, the script exits 0 and the archive
      contains no zero-byte regular file.
- [ ] `grep -n 'placeholder' experimental/boot/initramfs/build_initramfs.sh` shows
      no placeholder-creation branch.
- [ ] `recovery/docs/README.txt` no longer claims a BusyBox userland or an
      unqualified "Phase 2 COMPLETE" for the boot stack.
- [ ] `pytest tests/recovery_hardening/test_initramfs_manifest_sources.py -v`
      passes post-fix and fails pre-fix.

## Dependencies & related plans

- **BOOT-01** — moves the script to `experimental/boot/`; land BOOT-01 first and
  write tests against the post-move path (the test should resolve the script via
  repo-root glob so it survives either layout during transition).
- **BOOT-08** — the quarantine README's defect index links here.
- **BOOT-07** — decides where `BootableISOBuilder` lives (affects Fix B call site).

## Effort

1 day: 0.25 script fix + README claims, 0.25 newc parser helper, 0.5 tests.
No special environment (cpio + gzip only).
