LCSAS RECOVERY -- BUNDLED REFERENCE SPECS
===========================================

PURPOSE

Every dependency in the recovery toolchain is implemented from a
publicly available specification.  This directory bundles the
canonical specs so that a future implementer with no internet access
can audit, rebuild, or replace any component from primary sources
alone.

The intent is defense against "spec rot": even if every binary in
this directory tree fails to build, run, or pass tests in 50 years,
these documents are sufficient to rewrite the entire stack from
scratch.

CONTENTS

  Cryptographic primitives
  ------------------------
  fips180-4.{pdf,txt}   FIPS PUB 180-4 -- SHA-2 family (SHA-256, ...)
                         https://nvlpubs.nist.gov/nistpubs/FIPS/NIST.FIPS.180-4.pdf
  fips197.{pdf,txt}      FIPS PUB 197 -- Advanced Encryption Standard
                         https://nvlpubs.nist.gov/nistpubs/FIPS/NIST.FIPS.197.pdf
  rfc2104.txt            HMAC: Keyed-Hashing for Message Authentication
  rfc7914.txt            scrypt KDF (RFC 7914)
  rfc8018.txt            PKCS #5 v2.1 -- PBKDF2 (RFC 8018)
  rfc8439.txt            ChaCha20 and Poly1305 (Poly1305 spec)

  Encodings / data formats
  ------------------------
  rfc4648.txt            Base16, Base32, and Base64 Data Encodings
  rfc8259.txt            JSON (JavaScript Object Notation)
  rfc8878.txt            zstd Compression Format (RFC 8878)

  Storage / media
  ---------------
  ecma-119.{pdf,txt}     ISO 9660 / ECMA-119 -- CD-ROM filesystem

  Backup format
  -------------
  restic-format.md       Restic on-disc format (LCSAS-curated spec
                         derived from the upstream restic codebase
                         and aligned with rustic >= 0.10).

MAPPING: SPEC -> IMPLEMENTATION

  fips180-4              ->  src/lcsas-restore/sha256.c
  fips197                ->  src/lcsas-restore/aes.c
  rfc2104                ->  src/lcsas-restore/pbkdf2.c (HMAC inside)
  rfc7914                ->  src/lcsas-restore/scrypt.c
  rfc8018                ->  src/lcsas-restore/pbkdf2.c
  rfc8439                ->  src/lcsas-restore/poly1305.c
                              (note: we use the original Bernstein
                              Poly1305-AES construction, equivalent to
                              the §2.5 algorithm with s = AES_k(nonce))
  rfc4648                ->  src/lcsas-restore/b64.c
  rfc8259                ->  src/lcsas-restore/json_q.c
  rfc8878                ->  vendored/zstd/  (Facebook implementation)
  ecma-119               ->  src/lcsas-iso9660/iso9660.c
  restic-format          ->  src/lcsas-restore/{pack,repo,tree}.c

REBUILDING FROM SPEC ALONE

If every binary and every line of vendored C source becomes unusable,
the following are sufficient to recover an LCSAS archive:

  1. Read restic-format.md to understand the on-disc layout.
  2. Implement (or find) SHA-256 per fips180-4, AES-128/256 per
     fips197, scrypt per rfc7914, HMAC per rfc2104, Poly1305 per
     rfc8439.
  3. Implement base64 decoder per rfc4648 and a JSON tokenizer per
     rfc8259.
  4. (Optional, for v2 repos) Implement zstd decoder per rfc8878.
  5. Read ISO 9660 directories per ecma-119 (or use any UNIX-like
     OS's built-in iso9660 / cd9660 driver).
  6. Apply restic-format.md to extract files.

  Test vectors for each primitive are in CRYPTO.txt.

LICENSES

All documents in this directory are freely redistributable in their
original form:
  - NIST FIPS publications: U.S. government work, public domain.
  - IETF RFCs: BCP 78 / IETF Trust License, redistribution permitted.
  - ECMA-119 (4th ed.): "free of charge, in printed form and in
    electronic form" per ECMA's policy.
  - restic-format.md: LCSAS-curated, BSD-3 (matches rustic upstream).

SHA-256

  See SHA256.txt for digests of every file in this directory.
