/*
 * gf256.h -- GF(2^8) arithmetic for dvdisaster RS03 error correction.
 *
 * dvdisaster's RS03 codec works in GF(2^8) built from the primitive
 * polynomial 0x187 (x^8 + x^7 + x^2 + x + 1), NOT the more common
 * 0x11D used by most Reed-Solomon libraries.  The log/exp tables MUST
 * be generated from 0x187 or the parity will not match the discs that
 * dvdisaster wrote.  See docs/DVDISASTER_RS03_FORMAT.md sec 4.4.
 *
 * C89; no dependencies beyond the standard library.
 */
#ifndef LCSAS_ECC_GF256_H
#define LCSAS_ECC_GF256_H

/* Primitive polynomial used by dvdisaster RS03 (RS_GENERATOR_POLY). */
#define GF_PRIM_POLY 0x187
#define GF_FIELDSIZE 256
#define GF_FIELDMAX  255

/*
 * Initialise the global log/exp tables.  Idempotent; safe to call more
 * than once.  Must be called before any other gf_* function.
 */
void gf_init(void);

/* Multiply two field elements. */
unsigned char gf_mul(unsigned char a, unsigned char b);

/* Divide a by b (b must be non-zero; returns 0 if b == 0). */
unsigned char gf_div(unsigned char a, unsigned char b);

/* Multiplicative inverse of a (a must be non-zero; returns 0 if a==0). */
unsigned char gf_inv(unsigned char a);

/* alpha^e where alpha is the field generator (e taken mod 255). */
unsigned char gf_exp(int e);

/* Discrete log base alpha of a (a must be non-zero; returns 0 if a==0). */
int gf_log(unsigned char a);

#endif /* LCSAS_ECC_GF256_H */
