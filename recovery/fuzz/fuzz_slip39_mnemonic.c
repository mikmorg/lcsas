/*
 * fuzz_slip39_mnemonic.c -- LibFuzzer harness for the SLIP-0039 combiner.
 *
 * The lcsas-keyshare combiner parses untrusted, heir-typed text — bare
 * mnemonics and (via KEY-01) whole printed share-card files — on the
 * 50-year critical path.  This harness exercises every public entry of
 * slip39.h with arbitrary bytes:
 *
 *   1. lcsas_keyshare_extract on the raw buffer (the card-file parser),
 *      then lcsas_keyshare_check_share on whatever it extracts.
 *   2. The buffer split into up to MAX_M newline-delimited "mnemonics",
 *      each run through lcsas_keyshare_check_share, then the whole set
 *      fed to lcsas_keyshare_recover_password under a small passphrase
 *      matrix.
 *
 * Invariants asserted (abort => libFuzzer records a crash):
 *   - no read/write OOB, leak, or UB (ASan/UBSan/LSan catch these).
 *   - recover NEVER reports success with pwlen > LCSAS_KEYSHARE_MAX_PW.
 *   - extract NEVER writes an unterminated / over-capacity mnemonic
 *     (success implies a NUL within the buffer).
 *
 * Compile:
 *   make -C recovery fuzz-keyshare-smoke   # 60 s
 *   make -C recovery fuzz-keyshare         # 30 CPU-minutes
 */
#include "slip39.h"

#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#define MAX_M 64

static const struct {
    const unsigned char *pp;
    size_t plen;
} PASSPHRASES[] = {
    { (const unsigned char *)"", 0 },
    { (const unsigned char *)"TREZOR", 6 },
    { (const unsigned char *)"\xff\x00\x01", 3 }
};

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    char *buf;
    char extracted[4096];
    char errbuf[128];
    char *storage;
    const char *mnemonics[MAX_M];
    size_t n = 0;
    size_t i, start;
    unsigned char pw[LCSAS_KEYSHARE_MAX_PW];
    size_t pwlen;

    /* Cap the input: an heir transcribes a bounded number of cards, and
     * an unbounded buffer just slows the fuzzer without new coverage. */
    if (size > 65536) size = 65536;

    buf = (char *)malloc(size + 1);
    if (buf == NULL) return 0;
    memcpy(buf, data, size);
    buf[size] = '\0';

    /* (1) Card-file parser on the raw buffer. */
    errbuf[0] = '\0';
    if (lcsas_keyshare_extract(buf, extracted, sizeof(extracted),
                               errbuf, sizeof(errbuf)) == 0) {
        /* Success must yield a NUL-terminated mnemonic within capacity. */
        if (memchr(extracted, '\0', sizeof(extracted)) == NULL) abort();
        lcsas_keyshare_check_share(extracted, errbuf, sizeof(errbuf));
    }

    /* (2) Split into up to MAX_M newline-delimited candidate mnemonics.
     * Work on a second copy so the NUL-splitting does not disturb buf. */
    storage = (char *)malloc(size + 1);
    if (storage == NULL) { free(buf); return 0; }
    memcpy(storage, data, size);
    storage[size] = '\0';

    start = 0;
    for (i = 0; i <= size && n < MAX_M; i++) {
        if (i == size || storage[i] == '\n') {
            storage[i] = '\0';
            if (storage[start] != '\0') {
                /* Per-share diagnostic path (KEY-01). */
                lcsas_keyshare_check_share(storage + start,
                                           errbuf, sizeof(errbuf));
                mnemonics[n++] = storage + start;
            }
            start = i + 1;
        }
    }

    /* (3) Set-level recovery under the passphrase matrix. */
    if (n > 0) {
        size_t k;
        for (k = 0; k < sizeof(PASSPHRASES) / sizeof(PASSPHRASES[0]); k++) {
            pwlen = 0;
            if (lcsas_keyshare_recover_password(
                    mnemonics, n, PASSPHRASES[k].pp, PASSPHRASES[k].plen,
                    pw, &pwlen) == 0) {
                /* A reported success must respect the codec length cap. */
                if (pwlen > LCSAS_KEYSHARE_MAX_PW) abort();
            }
        }
    }

    free(storage);
    free(buf);
    return 0;
}
