# LCSAS — 50-Year Survivability Audit

> Created: 2026-02-21 | Mission-critical document for long-term archival integrity

---

## Mission Statement

LCSAS's most important goal is simple, full restoration of the most
recent snapshot by a **less-technical user**, in the case of the
**death of the author and archivist**.  The system must remain
recoverable over an estimated **50-year term**.

Everything in this document is evaluated against that single objective.

---

## 1. Human Documentation on Disc

These concerns relate to what a non-technical person sees when they
insert a disc.  The audience is a grieving family member who may not
know what Linux, encryption, or a "repository" is.

### 1.1 No plain-language START_HERE file — P0 ❌

**Current state:** Data discs have `RESTORE_INSTRUCTIONS.txt` which
opens with *"This disc is part of an LCSAS (Linux Cold Storage Archival
Suite) archive.  It contains encrypted, deduplicated backup pack
files..."* — technically accurate, humanly useless.  Meta-volumes have
`README_RESTORE.md` (Markdown, requires rendering for readability).
Neither file explains in plain terms what the data IS.

**Fix:** Add a configurable `START_HERE.txt` on every disc (data AND
meta) written in plain English:
- What these discs are ("backup copies of family files created by [NAME]")
- That an encryption key is required and where it might be found
- That a technical person can help with restoration
- What a "meta-volume" is and which disc it is
- Contact info for someone who can help

Requires: new config fields `archive_owner`, `archive_description`,
`key_storage_hints`, `technical_contact` on `LCSASConfig`.

### 1.2 No "what is this data" description — P0 ❌

**Current state:** `volume_info.json` has `"repositories": ["family",
"work"]` but no human-meaningful description.  Config has no field for
a human-readable archive description.

**Fix:** Add `archive_description` to config, embed in `START_HERE.txt`
and `volume_info.json`.

### 1.3 No "where is the key" hint on disc — P0 ❌

**Current state:** `RESTORE_INSTRUCTIONS.txt` says the key is *"NOT
stored on any disc for security"* but never says WHERE the archivist
put it.  Key storage strategy exists only in the archivist's head.

**Fix:** Add `key_storage_hints` to config (e.g. "Paper copy in the
home safe, USB copy in safe deposit box #1234 at First National Bank").
Embed in `START_HERE.txt`.

### 1.4 No "get a tech person" advice — P1 ❌

**Current state:** Neither `RESTORE_INSTRUCTIONS.txt` nor
`README_RESTORE.md` says *"If these instructions are confusing, take
all the discs plus the key to a computer professional."*

**Fix:** Add this guidance to `START_HERE.txt` and
`RESTORE_INSTRUCTIONS.txt`.

### 1.5 No key-to-repo mapping on disc — P1 ❌

**Current state:** If the archivist used different keys per repo,
nothing on disc tells the user which key goes with which repo.
The modern `restore.sh` prompts interactively for a single password per
invocation; the legacy `restore_legacy.sh` accepts a single `--key`
argument.  Neither path lets you specify per-repo keys in one run.

**Fix:** Write `KEY_INFO.txt` listing each repo, its human description,
and which key file it needs.  Derive from config.

### 1.6 Placeholder URL in RESTORE_INSTRUCTIONS.txt — P1 ❌

**Current state:** `staging/metadata.py` line 164 references
`https://github.com/your-org/lcsas` — a placeholder that will be a 404.

**Fix:** Replace with real URL or remove.

### 1.7 Markdown format for README_RESTORE — P2 ✅

**Current state:** `README_RESTORE.md` uses Markdown (headers, tables,
code blocks).  Viewed as raw text on a basic system, it's harder to
read than plain text.

**Fix:** Convert to `.txt` or keep both formats.

**Resolution:** `MetaVolumeBuilder._write_readme_txt()` writes a
plain-text `README_RESTORE.txt` alongside the Markdown version.
Uses `_strip_markdown()` to convert headings to UPPERCASE with
underlines, strip bold/italic/inline-code formatting, and convert
tables to aligned text.

### 1.8 No disc labeling / estate planning guidance — P2 ✅

**Current state:** No documentation tells the archivist to physically
label discs, maintain a paper manifest, or include a "letter to heirs"
in the disc binder.

**Fix:** Add `docs/ESTATE_PLANNING.md` with printable templates.

**Resolution:** Created `docs/ESTATE_PLANNING.md` with a printable
checklist, letter-to-heirs template, disc labeling conventions,
periodic verification schedule, and config examples.  Bundled on
meta-volume automatically via `_DOC_ITEMS`.

---

## 2. Bundled Tools Architecture

These concerns relate to whether the meta-volume's bundled tools will
still function in 10, 25, or 50 years.

### 2.1 ELF binaries are architecture-locked — P0 🔧 (in progress)

**Current state:** Bundled `rustic`, `xorriso`, `python3` are Linux
x86_64 ELF binaries.  They will not run natively on ARM64, macOS, or
Windows.

**Mitigation:** x86_64 will remain runnable via emulation (QEMU,
Rosetta 2) for decades.  The installed base guarantees VM/emulation
support indefinitely.

**Fix:** Bundle a statically-linked musl `rustic` binary (`rustic-static`)
that eliminates glibc dependency entirely.  Add `--static-rustic` option
to meta-volume builder.

### 2.2 glibc ABI dependency — P0 🔧 (in progress)

**Current state:** `bundler.py` explicitly does NOT bundle glibc family
libs (`libc.so`, `libpthread.so`, `ld-linux`, etc.).  The bundled
binaries depend on the host's glibc being ABI-compatible.  A future
Linux with a different libc or glibc major version will fail to load
them.

**Fix:** The static musl `rustic` binary (§2.1) eliminates this for
the critical-path tool.  xorriso dependency is being eliminated from
`restore.sh` (§2.4).

### 2.3 No tool version recording — P1 🔧 (in progress)

**Current state:** `volume_info.json` records `python_version` but not
`rustic` or `xorriso` versions.

**Fix:** Run `rustic --version`, `xorriso --version`, `dvdisaster
--version` during meta-volume build.  Record in `volume_info.json`
under `tool_versions`.

### 2.4 xorriso dependency in restore.sh — P1 🔧 (in progress)

**Current state:** `restore.sh` calls `$XORRISO -indev "$iso" -osirrox
on -extract / "$dest"` to extract ISOs.  This is the ONLY use of
xorriso in the restore path.

**Fix:** Use kernel-native `mount -o loop,ro` (requires root) as
primary method, `7z x` as fallback, bundled `xorriso` as last resort.
ISO 9660 is kernel-native on every Linux — no userspace tool needed.

### 2.5 dvdisaster is abandoned — P2 ✅

**Current state:** Last release 2020.  RS03 ECC format is specific to
dvdisaster.  If ECC repair is needed in 50 years, the format would need
reverse-engineering unless the bundled binary still works.

**Fix:** Bundle the RS03 format documentation on the meta-volume.
Long-term: consider a pure-Python RS03 decoder.

**Resolution:** Created `docs/DVDISASTER_RS03_FORMAT.md` — covers
RS03 binary layout (header, CRC sectors, parity sectors), GF(2^8)
arithmetic (primitive polynomial 0x11D), Reed-Solomon interleaving,
verify/repair/augment operations, and re-implementation guidance
with reference libraries.  Bundled via `_DOC_ITEMS`.

### 2.6 No restic format specification on disc — P0 🔧 (in progress)

**Current state:** The project treats pack files as opaque blobs,
delegating all format understanding to the `rustic` binary.  If rustic
can't run, there is no documentation on disc explaining the pack file
binary format, encryption scheme, or key derivation.

**Fix:** Create `docs/RESTIC_FORMAT_SPEC.md` documenting the repository
format (directory structure, key file JSON + scrypt KDF, AES-256-CTR
encryption, pack binary format, index JSON, snapshot JSON).  Bundle on
every meta-volume.  Long-term: implement pure-Python restore fallback.

### 2.7 No pure-Python restore fallback — P2 ✅

**Current state:** If both `rustic` and `rustic-static` fail to
execute, there is no way to decrypt and restore data without finding a
compatible binary.

**Fix:** Implement `src/lcsas/restore/restic_fallback.py` — a minimal
pure-Python implementation that can parse key files, derive the master
key (scrypt + AES-256-CTR), decrypt pack files, reconstruct files from
blobs.  Uses vendored pure-Python AES (no C extensions).  Long-term
effort (~500-1000 lines).

**Resolution:** Implemented `PurePythonRestorer` in
`src/lcsas/restore/restic_fallback.py` (~450 lines) with companion
`_aes_pure.py` (~220 lines).  Self-contained crypto stack: AES-256-CTR
(pure Python), Poly1305-AES MAC, scrypt (stdlib `hashlib.scrypt`),
SHA-256 (stdlib).  Supports key derivation, index parsing, snapshot
resolution, recursive tree traversal, file extraction, symlinks,
metadata restoration.  Optional zstd via `zstandard` package.  39 tests
including NIST AES vectors, RFC 8439 Poly1305 vector, and full
end-to-end restore of a synthetic repository.

---

## 3. Key Management

### 3.1 Key loss = total data loss — P0 (documented, not solvable)

**Current state:** Acknowledged in README.md: *"Your encryption keys
are the single point of total failure."*  But this warning is NOT on
any disc.

**Partial fix:** `START_HERE.txt` (§1.1) will include this warning and
key location hints.

### 3.2 Multi-repo key confusion — P1 ❌

**Current state:** Different repos may use different key files.
The modern `restore.sh` prompts for one password per invocation
(re-run once per repo); the legacy `restore_legacy.sh` accepts a
single `--key` flag.  Neither maps keys to repos on disc.

**Fix:** `KEY_INFO.txt` (§1.5) and enhance the entry-point scripts to
loop over repos with per-repo keys.

### 3.3 Config file not backed up to disc — P2 ✅

**Current state:** `config.toml` contains repo-to-key-file mappings
and archive configuration but is never written to any disc.

**Fix:** Include a sanitized config summary (without filesystem paths)
on each disc.

**Resolution:** `HolographicInjector.write_config_summary()` writes
`CONFIG_SUMMARY.txt` with media type, ECC redundancy, label prefix,
survivability fields, and repo names + key IDs.  Filesystem paths
are intentionally omitted (host-specific, useless on a standalone
disc).  Called from both orchestrator paths and the meta-volume
builder.

---

## 4. Media & Format Risks

### 4.1 ISO 9660 format — ✅ Low risk

ISO 9660 Level 3 with Rock Ridge + Joliet.  38-year-old standard,
universally supported.  Good choice.

### 4.2 Filename compatibility — ✅ Low risk

Pack files named by SHA-256 hex (64 lowercase hex chars).  Volume
labels use `[A-Z0-9_]`.  Both are valid on all OSes.

### 4.3 No media storage guidance on disc — P3 ✅

**Current state:** No guidance about storage conditions (cool,
dark, vertical orientation) on any disc or in user docs.

**Fix:** Add to `START_HERE.txt` or `DISC_CARE.txt`.

**Resolution:** `HolographicInjector.write_disc_care()` writes a
standalone `DISC_CARE.txt` covering handling, storage (vertical,
binder sleeves), environment (15–25 °C, 30–50 % RH, dark),
media longevity (M-DISC 1000+ yr, BD-R HTL 50–100 yr, DVD-R
10–50 yr), periodic verification schedule, and drive availability
advice.  Included on both data and meta volumes.

---

## 5. Single Points of Failure

| Failure Mode | Risk | Impact | Current Mitigation | Gap? |
|---|---|---|---|---|
| Key file lost | Medium | Total loss | README advises backup | Key hints not on disc |
| Family doesn't know discs exist | High | Total loss | None | No estate guidance |
| Meta-volume lost | Low | Harder restore | Manual procedure in RESTORE_INSTRUCTIONS | Requires expertise |
| x86_64 obsolete | Low (50yr) | Meta-volume ELF fails | None → static musl binary | In progress |
| glibc ABI break | Medium (50yr) | Dynamic ELF fails | None → static musl binary | In progress |
| Rustic project abandoned | Possible | Must find compatible tool | None → format spec on disc | In progress |
| Blu-ray drive unavailable | Medium | Can't read disc | None | No guidance |
| Wrong key for repo | Medium | Confusing error | None | No mapping on disc |
| Data disc bit-rot | Low (M-DISC) | Pack loss | RS03 ECC + SHA-256 | dvdisaster binary |

---

## 6. Implementation Status

| § | Concern | Priority | Status |
|---|---------|----------|--------|
| 1.1 | START_HERE.txt on every disc | P0 | ✅ Done |
| 1.2 | Archive description field | P0 | ✅ Done (config.archive_description) |
| 1.3 | Key storage hints on disc | P0 | ✅ Done (config.key_storage_hints) |
| 1.4 | "Get a tech person" advice | P1 | ✅ Done (in START_HERE + RESTORE_INSTRUCTIONS) |
| 1.5 | Key-to-repo mapping (KEY_INFO.txt) | P1 | ✅ Done |
| 1.6 | Fix placeholder URL | P1 | ✅ Done (removed placeholder) |
| 1.7 | README_RESTORE as plain text | P2 | ✅ Done |
| 1.8 | Estate planning guidance | P2 | ✅ Done |
| 2.1 | Static musl rustic binary | P0 | ✅ Builder support done (needs musl binary at burn time) |
| 2.2 | glibc ABI elimination | P0 | ✅ Via 2.1 |
| 2.3 | Tool version recording | P1 | ✅ Done (volume_info.json tool_versions) |
| 2.4 | Eliminate xorriso from restore.sh | P1 | ✅ Done (mount → 7z → xorriso cascade) |
| 2.5 | dvdisaster RS03 format docs | P2 | ✅ Done |
| 2.6 | Restic format spec on disc | P0 | ✅ Done (docs/RESTIC_FORMAT_SPEC.md) |
| 2.7 | Pure-Python restore fallback | P2 | ✅ Done |
| 3.1 | Key loss warning on disc | P0 | ✅ Done (via 1.1) |
| 3.2 | Multi-repo key mapping | P1 | ✅ Done (via 1.5) |
| 3.3 | Config backup to disc | P2 | ✅ Done |
| 4.3 | Media storage guidance | P3 | ✅ Done |
