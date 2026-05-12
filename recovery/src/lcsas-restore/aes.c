/*
 * aes.c -- FIPS 197 AES-128 and AES-256.
 *
 * Strict C89.  Tables are precomputed at compile time (sbox / Rcon).
 * No timing side-channel mitigations: this is restore-only code that runs
 * once with no concurrent adversary.  An attacker who can measure the
 * timing of a recovery run can also just steal the disc.
 *
 * Spec: FIPS PUB 197 sections 5.1 (cipher), 5.2 (key expansion).
 */
#include "aes.h"

/* S-box (FIPS 197 Figure 7). */
static const unsigned char sbox[256] = {
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16
};

/* Round constants (FIPS 197 §5.2).  Only Rcon[1..10] for AES-128;
 * AES-256 also uses up to Rcon[7] in its expansion. */
static const unsigned char rcon[11] = {
    0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36
};

/* GF(2^8) multiplication by 2 (xtime, FIPS 197 §4.2.1). */
static unsigned char
xtime(unsigned char x)
{
    return (unsigned char)((x << 1) ^ ((x & 0x80) ? 0x1b : 0x00));
}

static void
sub_bytes(unsigned char s[16])
{
    int i;
    for (i = 0; i < 16; i++) s[i] = sbox[s[i]];
}

static void
shift_rows(unsigned char s[16])
{
    unsigned char t;
    /* row 1: shift left by 1 */
    t = s[1];  s[1]  = s[5];  s[5]  = s[9];  s[9]  = s[13]; s[13] = t;
    /* row 2: shift left by 2 */
    t = s[2];  s[2]  = s[10]; s[10] = t;
    t = s[6];  s[6]  = s[14]; s[14] = t;
    /* row 3: shift left by 3 (= right by 1) */
    t = s[15]; s[15] = s[11]; s[11] = s[7];  s[7]  = s[3];  s[3]  = t;
}

static void
mix_columns(unsigned char s[16])
{
    int c;
    for (c = 0; c < 4; c++) {
        unsigned char a0 = s[c * 4 + 0];
        unsigned char a1 = s[c * 4 + 1];
        unsigned char a2 = s[c * 4 + 2];
        unsigned char a3 = s[c * 4 + 3];
        unsigned char t = a0 ^ a1 ^ a2 ^ a3;
        s[c * 4 + 0] ^= t ^ xtime((unsigned char)(a0 ^ a1));
        s[c * 4 + 1] ^= t ^ xtime((unsigned char)(a1 ^ a2));
        s[c * 4 + 2] ^= t ^ xtime((unsigned char)(a2 ^ a3));
        s[c * 4 + 3] ^= t ^ xtime((unsigned char)(a3 ^ a0));
    }
}

static void
add_round_key(unsigned char s[16], const unsigned char *rk)
{
    int i;
    for (i = 0; i < 16; i++) s[i] ^= rk[i];
}

static void
encrypt_block(const unsigned char *rk, int nr,
              const unsigned char in[16], unsigned char out[16])
{
    unsigned char state[16];
    int round;
    int i;

    for (i = 0; i < 16; i++) state[i] = in[i];
    add_round_key(state, rk);

    for (round = 1; round < nr; round++) {
        sub_bytes(state);
        shift_rows(state);
        mix_columns(state);
        add_round_key(state, rk + round * 16);
    }

    sub_bytes(state);
    shift_rows(state);
    add_round_key(state, rk + nr * 16);

    for (i = 0; i < 16; i++) out[i] = state[i];
}

void
lcsas_aes128_set_key(lcsas_aes128_key *k, const unsigned char key[16])
{
    /* FIPS 197 §5.2 KeyExpansion for Nk=4, Nr=10. */
    unsigned char *rk = k->rk;
    int i;
    unsigned char t[4];

    for (i = 0; i < 16; i++) rk[i] = key[i];
    for (i = 4; i < 44; i++) {
        t[0] = rk[(i - 1) * 4 + 0];
        t[1] = rk[(i - 1) * 4 + 1];
        t[2] = rk[(i - 1) * 4 + 2];
        t[3] = rk[(i - 1) * 4 + 3];
        if ((i % 4) == 0) {
            unsigned char tmp = t[0];
            t[0] = (unsigned char)(sbox[t[1]] ^ rcon[i / 4]);
            t[1] = sbox[t[2]];
            t[2] = sbox[t[3]];
            t[3] = sbox[tmp];
        }
        rk[i * 4 + 0] = (unsigned char)(rk[(i - 4) * 4 + 0] ^ t[0]);
        rk[i * 4 + 1] = (unsigned char)(rk[(i - 4) * 4 + 1] ^ t[1]);
        rk[i * 4 + 2] = (unsigned char)(rk[(i - 4) * 4 + 2] ^ t[2]);
        rk[i * 4 + 3] = (unsigned char)(rk[(i - 4) * 4 + 3] ^ t[3]);
    }
}

void
lcsas_aes128_encrypt(const lcsas_aes128_key *k,
                     const unsigned char in[16],
                     unsigned char out[16])
{
    encrypt_block(k->rk, 10, in, out);
}

void
lcsas_aes256_set_key(lcsas_aes256_key *k, const unsigned char key[32])
{
    /* FIPS 197 §5.2 KeyExpansion for Nk=8, Nr=14.
     * 60 words total = 240 bytes. */
    unsigned char *rk = k->rk;
    int i;
    unsigned char t[4];

    for (i = 0; i < 32; i++) rk[i] = key[i];
    for (i = 8; i < 60; i++) {
        t[0] = rk[(i - 1) * 4 + 0];
        t[1] = rk[(i - 1) * 4 + 1];
        t[2] = rk[(i - 1) * 4 + 2];
        t[3] = rk[(i - 1) * 4 + 3];
        if ((i % 8) == 0) {
            unsigned char tmp = t[0];
            t[0] = (unsigned char)(sbox[t[1]] ^ rcon[i / 8]);
            t[1] = sbox[t[2]];
            t[2] = sbox[t[3]];
            t[3] = sbox[tmp];
        } else if ((i % 8) == 4) {
            t[0] = sbox[t[0]];
            t[1] = sbox[t[1]];
            t[2] = sbox[t[2]];
            t[3] = sbox[t[3]];
        }
        rk[i * 4 + 0] = (unsigned char)(rk[(i - 8) * 4 + 0] ^ t[0]);
        rk[i * 4 + 1] = (unsigned char)(rk[(i - 8) * 4 + 1] ^ t[1]);
        rk[i * 4 + 2] = (unsigned char)(rk[(i - 8) * 4 + 2] ^ t[2]);
        rk[i * 4 + 3] = (unsigned char)(rk[(i - 8) * 4 + 3] ^ t[3]);
    }
}

void
lcsas_aes256_encrypt(const lcsas_aes256_key *k,
                     const unsigned char in[16],
                     unsigned char out[16])
{
    encrypt_block(k->rk, 14, in, out);
}

/* Increment a 16-byte big-endian counter in place. */
static void
ctr_inc(unsigned char ctr[16])
{
    int i;
    for (i = 15; i >= 0; i--) {
        ctr[i] = (unsigned char)(ctr[i] + 1);
        if (ctr[i] != 0) break;
    }
}

void
lcsas_aes256_ctr(const lcsas_aes256_key *k,
                 const unsigned char iv[16],
                 const unsigned char *in,
                 unsigned char *out,
                 size_t len)
{
    unsigned char counter[16];
    unsigned char keystream[16];
    size_t off = 0;
    int i;

    for (i = 0; i < 16; i++) counter[i] = iv[i];

    while (off < len) {
        size_t n = len - off;
        if (n > 16) n = 16;
        lcsas_aes256_encrypt(k, counter, keystream);
        for (i = 0; i < (int)n; i++) {
            out[off + i] = (unsigned char)(in[off + i] ^ keystream[i]);
        }
        ctr_inc(counter);
        off += n;
    }
}
