# LCSAS Key-Share Format (SLIP-0039)

> **Purpose of this document.** If, decades from now, the LCSAS encryption
> password was split into *shares* (so no single lost copy is fatal), this file
> tells a competent engineer how to reconstruct the password from those shares
> **using nothing but this spec** — no surviving LCSAS tool, no internet. It is
> the key-management sibling of `RESTIC_FORMAT_SPEC.md` and
> `DVDISASTER_RS03_FORMAT.md`, and it is bundled on every meta-volume.

---

## 1. What was split, and why

LCSAS encrypts each repository with a password that seeds restic's scrypt KDF
(see `RESTIC_FORMAT_SPEC.md` §"Key derivation"). **Losing that password = losing
the data, permanently** — there is no recovery path through the ciphertext.

To remove that single point of failure, the password may be split with **Shamir
Secret Sharing (SSS)** into **N shares** such that **any K reconstruct it** and
**any K−1 reveal nothing**. The default is **2-of-5**. The shares are encoded
using **SLIP-0039** (SatoshiLabs Improvement Proposal 39), a published,
checksummed, word-mnemonic share format.

The **secret being split is the repository password itself** (the "master
secret" in SLIP-0039 terms). SLIP-0039 also has a separate optional
*passphrase* — LCSAS leaves that empty by default; if used, it is an extra
factor the heir must also supply.

If you are holding **word-list cards** (20 or 33 words each), you have SLIP-0039
shares — read on. If you are holding a single password (no shares), this
document does not apply; use the password directly.

## 2. How to reconstruct (operator summary)

1. Gather **any K** shares (default K=2). Each share is an ordered list of
   words from the LCSAS/SLIP-0039 wordlist (`recovery/.../keyshare/wordlist.txt`
   on the meta-volume; 1024 words, each uniquely identified by its first 4
   letters).
2. Run the bundled combiner (Phase 2 ships it on the meta-volume) or any
   SLIP-0039-compatible tool, **or** re-implement §4 below.
3. The output bytes are the repository password. Feed it to `restore.sh` at the
   `Password:` prompt exactly as a single-key archive would.

A wrong or incomplete share set **fails loudly** (checksum/digest mismatch or
"insufficient shares") rather than returning garbage — see §4.6.

## 3. Share anatomy (SLIP-0039)

Each share mnemonic encodes (most-significant first), as 10-bit symbols mapped
through the 1024-word list:

| Field | Bits | Meaning |
|---|---|---|
| Identifier | 15 | Random; ties a share set together |
| Extendable flag | 1 | Salt-derivation variant |
| Iteration exponent | 4 | PBKDF2 work factor = 10000·2^e |
| Group index | 4 | Which group this share belongs to |
| Group threshold − 1 | 4 | Groups needed to recover |
| Group count − 1 | 4 | Total groups |
| Member index | 4 | Which share within the group |
| Member threshold − 1 | 4 | Shares needed within the group (this is K) |
| Padded share value | n×10 | The GF(256) Shamir share bytes (zero-padded to a 10-bit multiple) |
| Checksum | 30 | RS1024 Reed-Solomon checksum over the whole mnemonic |

A single-level **K-of-N** LCSAS split uses **one group** (group threshold 1,
group count 1, member threshold K, member count N).

## 4. Algorithm (for re-implementation)

The canonical, authoritative spec is **SLIP-0039**
(`https://github.com/satoshilabs/slips/blob/master/slip-0039.md`); the reference
implementation is `trezor/python-shamir-mnemonic`. The 45 official test vectors
are checked into this repo at `tests/fixtures/keyshare/vectors.json` — a correct
re-implementation must pass all 45. The pieces:

1. **Wordlist → bits.** Map each word to its 10-bit index; concatenate.
2. **RS1024 checksum.** A Reed-Solomon checksum over GF(1024) with the customization
   string `"shamir"` (or `"shamir_extendable"` when the extendable bit is set);
   the last 3 words carry it. Verify before trusting any field.
3. **Shamir over GF(256).** Each share value is a point on K−1-degree polynomials
   (one per secret byte) over the Rijndael field (reducing polynomial
   `0x11B`). Recover the secret bytes by **Lagrange interpolation** at x=255
   (the secret index); indices 254 and 255 are reserved for the digest and
   secret respectively.
4. **Digest integrity.** Index 254 holds `HMAC-SHA256(R, secret)[:4] || R` where
   R is random padding; recompute and compare to detect a corrupt/forged set.
5. **Passphrase decryption.** The recovered value is decrypted with a **4-round
   Feistel network** keyed by `PBKDF2-HMAC-SHA256(passphrase, salt, iterations)`,
   where `iterations = 2500·2^e` per round and `salt = "shamir" || identifier`
   (or empty when extendable). The result is the master secret = the password.
6. **Fail-closed.** Under-threshold share count, RS1024 mismatch, digest
   mismatch, mismatched set parameters, or an unknown word → hard error. Never
   return a "best effort" secret.

## 5. LCSAS specifics

- **Default split: 2-of-5.** A backup's dominant risk is *loss*, so K is kept
  low for recoverability; raise it for more privacy at the cost of more ways to
  become unrecoverable.
- **Secret constraints.** SLIP-0039 master secrets are even-length and ≥128
  bits. LCSAS encodes the (possibly shorter) repository password into a valid
  master secret at the CLI layer (`lcsas key split`); the inverse decoding is
  applied after reconstruction. See `KEY_INFO.txt` on the disc for whether an
  archive used split keys and the K/N in effect.
- **Where shares live.** Off-disc, by design — printed share cards in separate
  locations / with separate holders, per `docs/ESTATE_PLANNING.md`. Shares are
  **never** written to any LCSAS volume (that would defeat the split).
- **Wordlist provenance.** The bundled `wordlist.txt` is the official 1024-word
  SLIP-0039 list (unique 4-letter prefixes); it is pinned in
  `recovery/MANIFEST.sha256` alongside the combiner.

## 6. Minimal recovery, no tools

If neither the LCSAS combiner nor any SLIP-0039 tool runs, §4 is implementable
in well under 300 lines against this repo's vendored implementation
(`src/lcsas/keyshare/slip39.py`) and the 45 vectors as a conformance gate. The
only primitives required — SHA-256, HMAC, PBKDF2 — are in every language's
standard library and are specified by FIPS 180-4 / RFC 2104 / RFC 8018, which
have not changed since publication.
