/*
 * slip39.h -- C89 SLIP-0039 share combiner + LCSAS password codec.
 *
 * Tier-1-grade pure-C port of src/lcsas/keyshare/slip39.py and
 * src/lcsas/keyshare/codec.py.  Recovers an LCSAS repository password
 * from a set of SLIP-0039 mnemonic shares with no python3 dependency.
 *
 * Algorithm: SLIP-0039 (https://github.com/satoshilabs/slips/blob/
 * master/slip-0039.md).  RS1024 checksum over GF(1024); GF(256) Shamir
 * with Lagrange interpolation; a 4-round Feistel network keyed by
 * PBKDF2-HMAC-SHA256; an HMAC-SHA256 share-integrity digest; and the
 * extendable-backup-flag salt construction.  Cross-checked against the
 * 45 official SLIP-0039 test vectors.
 *
 * Reuses sha256.c / pbkdf2.c (lcsas_hmac_sha256, lcsas_pbkdf2_sha256)
 * from recovery/src/lcsas-restore/.  Strict C89, no dynamic allocation.
 */
#ifndef LCSAS_SLIP39_H
#define LCSAS_SLIP39_H

#include <stddef.h>

/*
 * The 1024-word SLIP-0039 wordlist, defined in wordlist.c (do not
 * regenerate).  Indexed 0..1023.
 */
extern const char *const lcsas_slip39_wordlist[1024];

/*
 * Buffer sizing.
 *
 * LCSAS_SLIP39_MAX_SECRET sizes the caller's out_secret buffer for the
 * official SLIP-0039 vectors, whose master secrets are at most 32 bytes;
 * 64 leaves headroom.  The LCSAS password path frames a password of up
 * to 65535 bytes into a master secret of up to 65538 bytes, but that
 * larger master secret is handled internally by
 * lcsas_keyshare_recover_password (it allocates its own buffer), so
 * callers of lcsas_slip39_recover supply LCSAS_SLIP39_MAX_SECRET bytes
 * only when they know the secret is vector-sized.  Per-share value
 * storage inside the combiner is heap-allocated to the actual length, so
 * a full-size password reconstructs correctly regardless of this define.
 *
 * LCSAS_KEYSHARE_MAX_PW is the password output cap (codec length prefix
 * is 2 bytes => 65535 max).
 */
#define LCSAS_SLIP39_MAX_SECRET 64
#define LCSAS_KEYSHARE_MAX_PW   65535

/*
 * Recover the SLIP-0039 master secret from `n` mnemonic strings.
 *
 *   mnemonics   array of `n` NUL-terminated, space-separated mnemonics.
 *   passphrase  the SLIP-0039 passphrase (printable ASCII; may be NULL
 *               when plen == 0 for the empty passphrase).
 *   out_secret  caller buffer of at least LCSAS_SLIP39_MAX_SECRET bytes.
 *   out_len     receives the master-secret length on success.
 *
 * Returns 0 on success.  Returns nonzero on ANY failure (unknown word,
 * bad length/padding, RS1024 mismatch, mismatched share parameters,
 * fewer than the threshold number of shares/groups, or a failed
 * integrity digest).  On failure the contents of out_secret are
 * unspecified and MUST NOT be used: a partial or wrong secret is never
 * reported as success.
 */
int lcsas_slip39_recover(const char *const *mnemonics, size_t n,
                         const unsigned char *passphrase, size_t plen,
                         unsigned char *out_secret, size_t *out_len);

/*
 * Decode an LCSAS-framed master secret back into the original password
 * (inverse of codec.py encode_master_secret): a 2-byte big-endian
 * length prefix followed by that many password bytes.
 *
 *   out_pw      caller buffer of at least LCSAS_KEYSHARE_MAX_PW bytes.
 *   out_pwlen   receives the password length on success.
 *
 * Returns 0 on success, nonzero if `ms` is too short to hold the prefix
 * or the recorded length runs past the end of the buffer (corrupt /
 * truncated master secret).
 */
int lcsas_keyshare_decode_master_secret(const unsigned char *ms, size_t mslen,
                                        unsigned char *out_pw, size_t *out_pwlen);

/*
 * Convenience: recover the master secret from the mnemonics, then decode
 * it into the LCSAS repository password.  Chains lcsas_slip39_recover and
 * lcsas_keyshare_decode_master_secret.
 *
 *   out_pw      caller buffer of at least LCSAS_KEYSHARE_MAX_PW bytes.
 *   out_pwlen   receives the password length on success.
 *
 * Returns 0 on success, nonzero on any failure.
 */
int lcsas_keyshare_recover_password(const char *const *mnemonics, size_t n,
                                    const unsigned char *passphrase, size_t plen,
                                    unsigned char *out_pw, size_t *out_pwlen);

/*
 * Extract the share mnemonic embedded in `text` (a bare mnemonic file OR
 * a printed share card with header lines and prose).  A line contributes
 * to the mnemonic iff it is non-blank and every whitespace-separated
 * token is a SLIP-0039 wordlist word; such lines are joined, in order,
 * into one space-separated, lowercased mnemonic written NUL-terminated
 * into `out` (capacity `cap`).
 *
 * Returns 0 on success.  Returns nonzero if the joined word count is not
 * a valid share length (20 or 33) or `out` is too small; on the
 * word-count failure a one-line reason (e.g. "found 7 share words,
 * expected 20 or 33") is written into `errbuf` (capacity `errcap`,
 * truncated to fit; may be NULL to discard).
 */
int lcsas_keyshare_extract(const char *text, char *out, size_t cap,
                           char *errbuf, size_t errcap);

/*
 * Validate ONE share mnemonic independently of the rest of the set, for
 * actionable per-share diagnostics before recovery is attempted.  Checks,
 * in order: every token resolves to a wordlist word (full word, or a
 * unique 4+-letter prefix); the word count is a valid share length; and
 * the per-share RS1024 checksum verifies.
 *
 * Returns 0 if the share is individually valid.  On any failure returns
 * nonzero and writes a one-line, human-readable reason into `errbuf`
 * (capacity `errcap`, truncated to fit; may be NULL to discard), e.g.:
 *   word 7 'buidling' is not a share word
 *   checksum FAILED - recheck your typing against the card
 *
 * A pass here means the share is internally consistent; it does NOT mean
 * the share belongs to the same split as the others — that mismatch only
 * surfaces during recovery.
 */
int lcsas_keyshare_check_share(const char *mnemonic,
                               char *errbuf, size_t errcap);

#endif
