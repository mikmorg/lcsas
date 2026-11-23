# LCSAS Share Format (SLIP-0039)

> **Purpose of this document.** If, decades from now, the LCSAS encryption
> password was split into *shares* (so no single lost copy is fatal), this file
> tells a competent engineer how to reconstruct the password from those shares
> **using nothing but this spec** — no surviving LCSAS tool, no internet. It is
> the password-splitting sibling of `RESTIC_FORMAT_SPEC.md` and
> `DVDISASTER_RS03_FORMAT.md`, and it is bundled on every meta disc.

---

## 1. What was split, and why

LCSAS encrypts each repository with a password that seeds restic's scrypt KDF
(see `RESTIC_FORMAT_SPEC.md` §"Key derivation"). **Losing that password = losing
the data, permanently** — there is no way back in through the ciphertext.

To remove that single point of failure, the password may be split with **Shamir
Secret Sharing (SSS)** into **N shares** such that **any K reconstruct it** and
**any K−1 reveal nothing**. The default is **2-of-5**. The shares are encoded
using **SLIP-0039** (SatoshiLabs Improvement Proposal 39), a published,
checksummed, word-mnemonic share format.

The **secret being split is the repository password itself** (the "master
secret" in SLIP-0039 terms). SLIP-0039 also has a separate optional
*passphrase*: LCSAS always leaves it empty (`lcsas password split` has no
option for one), and its combiners read none, so the heir needs only the
cards.

If you are holding **share cards** (20 words or more each), you have
SLIP-0039 shares — read on. If you are holding a single password (no shares),
this document does not apply; use the password directly.

**How many words a card has.** A share is 7 words of header and checksum
plus one word per 10 bits of the framed password (§5 "Secret constraints":
a 2-byte length, the password, zero-padded to an even length of at least 16
bytes). So a password of up to 14 bytes gives 20 words, 30 bytes 33 words, a
64-character password 60 words, an 88-character one 79 words. Every card of
one split has the same number of words, and the card prints it: "THE SHARE
WORDS (60 words; ...)".

**The card's layout.** A card printed since rc12 (#955) shows its words in
numbered rows of four:

```
THE SHARE WORDS (44 words; keep every word, in order)
    1 change      2 marathon    3 academic    4 acid
    5 curly       6 frozen      7 likely      8 advance
  ...
```

The numbers only keep the reader's place: every LCSAS combiner skips a token
that is only digits with an optional `.` or `)`, so the words may be typed
with or without their numbers, and "word N" in an error is the word numbered
N on the card. Older cards print the words on one line, unnumbered; they are
read the same way.

## 2. How to reconstruct (summary)

The restore scripts on the meta disc (`restore.sh`, `restore.bat`) ask for
the share cards themselves when a backup set's password is split: type the
words of one card at each question. The steps below are the fallback, for a
restore that does not ask (discs made by an older LCSAS) or for a person
doing it by hand.

1. Gather **any K** shares (default K=2). Each share is an ordered list of
   words from the LCSAS/SLIP-0039 wordlist (1024 words, each uniquely
   identified by its first 4 letters; shipped inside the bundled
   `lcsas.keyshare` python package on the meta disc, and compiled into
   `lcsas-shares` as `wordlist.c`). You may type just the first 4
   letters of each word — the bundled combiners (`lcsas-shares`,
   `combine_shares.py`, and `lcsas password combine`) expand any unambiguous
   prefix to the full word — and the card's numbers may be left in.
2. Put each card's words in a text file of its own (or use the printed
   card's text as it is) and run the combiner bundled on the meta disc:
   `recovery/bin/<machine>/lcsas-shares <card1> <card2> ...`
   If that binary won't run, use the Python fallback:
   `python3 combine_shares.py <card1> <card2> ...`
   Any SLIP-0039-compatible tool also works, **or** re-implement §4 below.
3. The output bytes are the repository password. Type it when the restore
   asks for the password; if the restore asks for share cards instead, type
   `P` at the share-card question and then the password.

A wrong or incomplete share set **fails loudly** (checksum/digest mismatch or
"insufficient shares") rather than returning garbage — see §4.6. Before
combining, the bundled combiners run a per-share pre-pass: each
share is validated on its own and a named verdict is printed (e.g.
`share 2 (alpha-share-2-card.txt): word 7 'buidling' is not a share word`),
so a single mistyped card is pinpointed instead of failing generically.

## 3. Share anatomy (SLIP-0039)

Each share mnemonic encodes (most-significant first), as 10-bit symbols mapped
through the 1024-word list:

| Field | Bits | Meaning |
|---|---|---|
| Identifier | 15 | Random; ties a share set together |
| Extendable flag | 1 | Salt-derivation variant |
| Iteration exponent | 4 | PBKDF2 work factor = 10000·2^e |
| Group index | 4 | Which group this share belongs to |
| Group threshold − 1 | 4 | Groups needed to rebuild the secret |
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
   the last 3 words carry it. Check it before trusting any field.
3. **Shamir over GF(256).** Each share value is a point on K−1-degree polynomials
   (one per secret byte) over the Rijndael field (reducing polynomial
   `0x11B`). Rebuild the secret bytes by **Lagrange interpolation** at x=255
   (the secret index); indices 254 and 255 are reserved for the digest and
   secret respectively.
4. **Digest integrity.** Index 254 holds `HMAC-SHA256(R, secret)[:4] || R` where
   R is random padding; recompute and compare to detect a corrupt/forged set.
5. **Passphrase decryption.** The rebuilt value is decrypted with a **4-round
   Feistel network** keyed by `PBKDF2-HMAC-SHA256(passphrase, salt, iterations)`,
   where `iterations = 2500·2^e` per round and `salt = "shamir" || identifier`
   (or empty when extendable). The result is the master secret = the password.
6. **Fail-closed.** Under-threshold share count, RS1024 mismatch, digest
   mismatch, mismatched set parameters, or an unknown word → hard error. Never
   return a "best effort" secret.

## 5. LCSAS specifics

- **Choose the password like it will be brute-forced.** Splitting protects you
  from *losing* a card; it does **not** strengthen the password itself.
  Reconstruct K cards and you get the same password a disc thief is guessing
  against — every burned disc carries the password-locked key files forever and
  faces unlimited offline scrypt guessing (`N=2^15, r=8, p=1`). **Generate** the
  password — a diceware passphrase of **7+ words** (≈ 90 bits) or an equivalent
  random string. `lcsas password split` prints a non-blocking warning when the
  password is under 16 chars or below ~60 estimated bits. Full threat model and
  entropy table: `docs/DISC_CONFIDENTIALITY.md`.
- **Default split: 2-of-5.** A backup's dominant risk is *loss*, so K is kept
  low for restorability; raise it for more privacy at the cost of more ways to
  lose the data.
- **Secret constraints.** SLIP-0039 master secrets are even-length and ≥128
  bits. LCSAS encodes the (possibly shorter) repository password into a valid
  master secret at the CLI layer (`lcsas password split`); the inverse decoding is
  applied after reconstruction. See `PASSWORD_INFO.txt` on the disc for whether an
  archive's password was split and the K/N in effect.
- **Checked at split time.** `lcsas password split` authenticates the password
  against the repository's real `keys/` files (pure-Python scrypt + Poly1305)
  and round-trips the freshly written cards *before* declaring success, so it
  never splits a password that does not unlock the repository or writes a card
  that does not reconstruct. Pass `--no-verify-repo` only when the mirror's
  `keys/` is unreachable at split time. Re-check the split any time with
  `lcsas --config ... password verify --repo R --share-file CARD …` (or
  `--password-file FILE`); exit code is the contract.
- **Card stamps & rotation.** Each printed card carries `Split on : <date>` and
  `Split ID : NNNNN` — the 15-bit SLIP-0039 identifier, identical across all
  cards of one split and (almost certainly) different between splits. After a
  password change, *all* old cards are void; re-split, redistribute, destroy
  superseded cards, and `lcsas password verify` the new set. See `docs/ESTATE_PLANNING.md`
  "ROTATION". Cards from before this stamping (no `Split on`/`Split ID` lines)
  remain valid combiner input — the extractor ignores non-wordlist lines.
- **Where shares live.** Off-disc, by design — printed share cards in separate
  places / with separate share holders, per `docs/ESTATE_PLANNING.md`. Shares
  are **never** written to any LCSAS disc (that would defeat the split).
- **Wordlist provenance.** The bundled `wordlist.txt` is the official 1024-word
  SLIP-0039 list (unique 4-letter prefixes). It is git-pinned in this
  repository and guarded by the 45 official SLIP-0039 test vectors
  (`recovery/tests/test_keyshare.c`, `tests/unit/test_keyshare.py`).
  SHA-256 of `wordlist.txt` (1024 LF-terminated words):
  `bcc4555340332d169718aed8bf31dd9d5248cb7da6e5d355140ef4f1e601eec3`
  — check with `sha256sum wordlist.txt`. The C combiner's `wordlist.c` is
  generated from the same list.

## 6. Minimal reconstruction, no tools

If neither the LCSAS combiner nor any SLIP-0039 tool runs, §4 is implementable
in well under 300 lines against this repo's vendored implementation
(`src/lcsas/keyshare/slip39.py`) and the 45 vectors as a conformance gate. The
only primitives required — SHA-256, HMAC, PBKDF2 — are in every language's
standard library and are specified by FIPS 180-4 / RFC 2104 / RFC 8018, which
have not changed since publication.
