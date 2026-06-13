# RST-02: zstd-magic sniff falsely "decompresses" uncompressed blobs (tiers 1 + 3)

**Priority:** P1 · **Severity:** high · **Dimension:** restore-python · **Audit status:** confirmed (high confidence) · **Ledger:** untracked
**Suggested GH issue title:** Gate blob decompression on index uncompressed_length, not magic sniff

## Problem

restic/rustic repo-v2 "auto" compression stores incompressible blobs **uncompressed**, and the
repository index marks compression solely via the presence of `uncompressed_length` on the blob
entry. Our readers ignore that contract: tier-3 (`restic_fallback._read_blob`) and tier-1
(`repo.c read_blob`) both sniff the decrypted plaintext for the 4-byte zstd magic and decompress
unconditionally when it matches.

Any archived file that is *itself* zstd data — `.zst`, `.tar.zst`, zstd-framed game/app assets —
has a first chunk starting with `0x28B52FFD`. Being already-compressed, restic stores that blob
uncompressed; our readers then falsely decompress it, the SHA-256 content check fails, and
`IntegrityError` is raised on perfectly valid data. Under today's no-skip behavior (RST-03) that
single false rejection aborts the **entire** tier-1 or tier-3 restore. Only tier-2 (upstream
rustic) handles such archives. The heir restoring a family archive that happens to contain one
`.zst` file loses tiers 1 and 3 entirely. The project's own on-disc format spec
(`docs/RESTIC_FORMAT_SPEC.md`) encodes the wrong rule, so a future re-implementer would
re-introduce the bug. The audit reproduced the false `IntegrityError` live.

## Evidence

Re-verified against current code (2026-06-10):

- `src/lcsas/restore/restic_fallback.py:958-963` — the sniff; `uncompressed_length` used only
  as a size hint:
  ```python
  if plaintext[:4] == _ZSTD_MAGIC:
      max_out = loc.uncompressed_length or (len(plaintext) * 20)
      plaintext = _decompress_zstd(plaintext, max_output_size=max_out)
  ```
- `src/lcsas/restore/restic_fallback.py:577` — the index parser DOES record the correct
  discriminator: `uncompressed_length=blob.get("uncompressed_length")` — available but unused.
- `recovery/src/lcsas-restore/repo.c:927-939` — identical sniff in C: comment "Inline pack blobs
  in restic v2 are zstd-compressed (no prefix byte)"; decompresses whenever
  `pt[0..3] == ZSTD_MAGIC`, using `loc->uncompressed_length` only when `> 0` (line 935).
- `docs/RESTIC_FORMAT_SPEC.md:179-180` — wrong rule shipped on every disc: "After decrypting a
  blob, check if it starts with the zstd magic bytes (`0x28 0xB5 0x2F 0xFD`); if so, decompress".
- `tests/unit/test_restic_fallback.py:~1470-1510` — covers only the compressed case
  (`uncompressed_length=len(original_content)` set); no fixture for an uncompressed blob whose
  content starts with the magic. No such fixture in `recovery/tests/` either.

## Fix design

Adopt the restic index contract — `uncompressed_length` present ⇔ blob is compressed — with a
raw-hash-first fallback for index entries that are silent (defensive; SHA-256 of the raw
plaintext is authoritative). Chosen over pure magic-sniff-with-rehash because the index contract
is the documented format rule and avoids double-hashing on every blob in the common case.

**Tier-3** — `src/lcsas/restore/restic_fallback.py::_read_blob` (replace lines 958-963):

```python
if loc.uncompressed_length is not None:
    # Index says compressed (restic v2 contract).
    plaintext = _decompress_zstd(
        plaintext, max_output_size=loc.uncompressed_length
    )
elif plaintext[:4] == _ZSTD_MAGIC:
    # Index silent (v1 repo / legacy index): raw hash is authoritative.
    if hashlib.sha256(plaintext).hexdigest() != blob_id:
        plaintext = _decompress_zstd(
            plaintext, max_output_size=len(plaintext) * 20
        )
```

The existing content-hash check after this block stays as the final arbiter.
`standalone_restorer.py` is generated from this file at meta/stage build time, so the fix
propagates to future discs automatically.

**Tier-1** — `recovery/src/lcsas-restore/repo.c` `read_blob` (lines 927-956): restructure to:
`if (loc->uncompressed_length > 0) { decompress to that exact size; }` else if magic matches:
compute `lcsas_sha256` over raw `pt`; if it equals `loc->id`, skip decompression; otherwise
attempt `lcsas_zstd_decode` as today. Verify the C index parser preserves
absent-`uncompressed_length` as 0 (it does — `loc->uncompressed_length > 0` is already the
sentinel at line 935). Keep the existing 256 MiB cap and final hash check.

**Spec** — correct `docs/RESTIC_FORMAT_SPEC.md` §4.5: "A pack blob is zstd-compressed if and
only if its index entry carries `uncompressed_length`. Do NOT infer compression from leading
bytes — uncompressed blobs may legitimately begin with the zstd magic."

No catalog/schema impact. Old discs ship the wrong spec text and the old
`standalone_restorer.py` forever; the corrected spec on *new* discs plus the tier-2 binary is
the mitigation for the back-catalog (note this in the spec changelog).

## Tests & gates

- `tests/unit/test_restic_fallback.py::test_uncompressed_blob_with_zstd_magic_roundtrips` —
  store a blob whose content is a real zstd frame, index entry with `uncompressed_length=None`;
  assert `_read_blob` returns it verbatim (today: raises IntegrityError). Always-on
  (`make test-unit`).
- `tests/unit/test_restic_fallback.py::test_compressed_blob_decompresses_via_index_hint` —
  regression guard: compressed blob with `uncompressed_length` set still decompresses (extends
  the existing test at ~1470).
- `tests/unit/test_restic_fallback.py::test_legacy_index_no_hint_magic_fallback` — compressed
  blob with `uncompressed_length=None` (index-silent path): raw hash mismatches → decompress →
  hash matches.
- `recovery/tests/test_repo.c` — C unit case mirroring the first two: craft a pack whose blob
  plaintext is a zstd frame with `uncompressed_length == 0` in the index; assert byte-identical
  readout. Runs under `make -C recovery test` (local + `.github/workflows/audit-gate.yml` on
  recovery/ paths).
- End-to-end fixture: add a `.zst` file to the fixture repo used by `recovery/tests/test_e2e.py`
  (rustic auto-compression stores it uncompressed); assert tier-1 `lcsas-restore` restores it
  byte-identical. Same lane as above.
- Doc gate: extend the docs-contract test family (see FMT/GATE plans) with a grep asserting
  RESTIC_FORMAT_SPEC.md §4.5 mentions `uncompressed_length` as the discriminator.

## Acceptance criteria

- [ ] A repo containing a `.zst` file (stored uncompressed by restic auto-compression) restores
      byte-identical via tier-3 `PurePythonRestorer` and via tier-1 `lcsas-restore`.
- [ ] Compressed-blob restores are unchanged (existing unit + e2e suites green).
- [ ] `docs/RESTIC_FORMAT_SPEC.md` §4.5 states the index-presence rule and warns against
      magic-sniffing.
- [ ] `make test-unit`, `make -C recovery test`, `make lint`, `make typecheck` pass;
      `make -C recovery audit-gate` run before merging the repo.c change (per recovery/CLAUDE.md).

## Dependencies & related plans

- RST-03 (skip-and-continue) turns this from restore-aborting into file-skipping; both should
  land — this plan removes the false trigger, RST-03 removes the blast radius.
- FMT plans own the broader writer-drift gate (live rustic ↔ pinned readers); this fixture
  (a stored-uncompressed `.zst`) should be folded into that gate's corpus when it lands.

## Effort

2 days: 0.5 Python + spec, 0.5 C (incl. audit-gate run), 1 tests/fixtures. Needs the local
recovery toolchain (clang/gcovr for audit-gate); no cross-arch environment required.

---
**Implemented:** 2026-06-13. As planned, with these deviations: (1) the doc-contract
grep gate was NOT added — that test family is owned by the FMT/GATE plans and does not yet
exist to extend; the spec §4.5 rewrite (index-presence rule + magic-sniff warning + changelog)
is in place. (2) The C unit + e2e fixtures were folded into the existing generators
(`gen_fixture.py` adds a zstd-magic uncompressed data blob; `test_e2e.py` gains an
`incompressible=` knob and a `v2-zst-file` case) rather than new standalone files. Both red-first
proven against the unfixed readers (tier-3 IntegrityError; tier-1 exit=1). All 5 committed
per-target `lcsas-restore` bins (x86_64, aarch64, armv7, x86_64-windows, aarch64-macos)
rebuilt via `zig cc`; unrelated `lcsas-iso9660` bins left untouched. qemu/wine cross-arch
hardening tests + audit-gate run.
