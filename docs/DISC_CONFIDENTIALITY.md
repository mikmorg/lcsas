# Disc confidentiality — the stolen-disc threat model

> **One-line version:** A thief holding any single LCSAS data disc gets the
> archive's whole *catalog* in plaintext and every repository's
> *password-locked* key files. They do **not** get your file contents unless
> they also guess your password. The only thing standing between a stolen disc
> and your data is the strength of that password — so make it long and random,
> and choose where discs and cards are kept as if every disc carries this
> inventory; every data disc does.

This document is the deliberate, written threat model for **confidentiality**
of a burned LCSAS disc (restorability findings live elsewhere). It exists so
that an owner choosing where to store discs and cards can weigh the
tradeoff with eyes open. It is referenced from `docs/ESTATE_PLANNING.md`,
`docs/guides/survivability.md`, and `recovery/docs/TIERS.txt`, and is bundled on
every meta disc.

---

## 1. What someone holding ONE disc learns (plaintext)

Every data disc carries, for **every repository** in the archive, the
following in the clear. The meta disc carries the same key files and
`key_storage_hints`, but no catalog.

- **The disc catalog** (`catalog.db`, a SQLite copy of the whole catalog
  burned verbatim onto every data disc, current as of that disc's burn). That
  includes:
  - every snapshot's **hostname, backup paths, tags, and description**;
  - every repository's **name and mirror path**;
  - every **location name** and the full map of **which disc is where** —
    i.e. where every other disc is kept;
  - the **volume_events audit trail**.
- **Every repository's rustic `keys/` directory** — the scrypt-wrapped master
  keys. The key-file JSON additionally carries the **plaintext `username` and
  `hostname`** of the machine that created the repository (a content-addressed
  artifact; see §6).
- **The owner's `key_storage_hints`**, printed verbatim into `START_HERE.txt`
  and `PASSWORD_INFO.txt` on every disc.

In short: a single disc reveals your whole backup *topology and metadata*, and
hands the attacker the locked door (the wrapped keys) to attack offline
at leisure.

## 2. What they do NOT get

- **Your actual file contents.** Pack data is encrypted with AES-256 and
  authenticated (Poly1305 / HMAC-SHA-256); the audited crypto is sound. The
  packs are useless without the repository password.
- **The password itself.** It is never written to any disc.

So the entire confidentiality of your *data* reduces to one question: can the
attacker guess the password?

## 3. The single defense: password entropy vs. offline guessing

Because the wrapped key files are on every disc, an attacker can run an
**unlimited, offline, parallel brute-force** against the password — forever,
on hardware that only gets faster. There is no rate limit, no lockout, no
server to take offline. The Key Derivation Function is the only speed bump:

```
scrypt(password, salt, N, r, p, dkLen=64)
```

Each key file records its own `N`, `r`, `p` (see `docs/RESTIC_FORMAT_SPEC.md`
§3). Measured 2026-10-20 (§7): **rustic 0.11.2, which writes LCSAS's
repositories, records `N=2^17` (131072), `r=8`, `p=1`**, i.e. 128 MiB of memory
and about 0.7 s of one CPU core per guess. restic 0.19 calibrates per host and
wrote `N=2^15`, `r=8` with `p=1` or `p=2` (32 MiB, 0.17–0.31 s). Being
memory-hard blunts GPU/ASIC speedups, but it is *not* a substitute for password
entropy over a multi-decade horizon.

### Entropy guidance (from the measurements in §7)

| Password entropy | Cost to search half the space at rustic's parameters (§7) |
|---|---|
| ~40 bits (e.g. a short human-chosen password) | ~10^4 core-years: about a year for a ten-thousand-core attacker today, and cheaper every decade. Treat as **broken** over the archive's lifetime |
| ~60 bits | ~10^10 core-years: out of reach today; erodes with hardware over decades |
| **≥ 90 bits** (≈ 7+ EFF-diceware words) | ~10^19 core-years: **out of reach through the 2060s** under any plausible scaling. Recommended |

**Recommendation:** generate the password — do not invent it. Use a
**diceware passphrase of 7 or more words** (≈ 90 bits) or an equivalent random
string from a password manager. `lcsas key split` prints a non-blocking
warning when the supplied password is under 16 characters or below ~60
estimated bits.

## 4. Immutability = no rotation

**A burned disc is permanent.** Changing your repository password later does
**nothing** for discs already in the wild: their old, password-locked key
files remain valid forever and reconstruct the *old* password. There is no
"revoke" for an already-burned disc.

The only true revocation for a leaked or weak password is the **physical
destruction of every disc** that carries the affected key files.
Plan accordingly: the password you burn is, in practice, the password those
discs will carry for their entire physical lifetime (100+ years for M-Disc).

## 5. Where to keep discs and cards

- **Treat every disc as carrying the full inventory in §1** when choosing
  offsite locations or places for cards. A disc in a given place exposes that
  place to whatever §1 reveals.
- **Never store a share card (or the password, or a password card) *with*
  a disc.** Password/data separation is the whole point: a thief who gets both
  a disc and the password (a password card, or K share cards) has everything.
- **`key_storage_hints` is printed on every disc.** Write it so an heir can
  *locate* the password without a thief being *handed* it — name a custodian,
  not a findable cache. Good: "Sealed envelope with the family attorney (see
  my will)." Bad: "USB in safe deposit box #1234 at First National Bank."

## 6. Why the keys are on every disc anyway (a documented decision)

The wrapped key files ride on every disc, and the full catalog on every data
disc, **on purpose**: it is the *single-disc restore* property. An heir who
finds any one data disc — plus the password — can restore, with no central
server and no other disc required. For this system, **restorability is the
bar**, and it is deliberately chosen over confidentiality of the metadata
and the wrapped (not bare) keys. This is a tradeoff made with intent, not an accident:

- The bare key is *never* exposed; only the scrypt-wrapped form is, and it is
  only as weak as the password protecting it (§3).
- The plaintext catalog is metadata, not file contents.

Any future "sanitized disc catalog" option would affect **newly burned
discs only**: every already-burned data disc carries the full plaintext
catalog forever. The decision not to offer one is recorded in §8.3.

Key files and their plaintext `username`/`hostname`: rustic 0.11.2 writes
**neither** field (measured, §7), so a repository LCSAS created carries none.
Only a repository first initialised by restic does (restic 0.19 writes
`username`, `hostname` and `created`). Rewriting those bytes would change the
file's SHA-256, which restic uses as the file's name, so a scrub means renaming
every key file on every future disc. It isn't done.

## 7. Measured numbers

Measured 2026-10-20 with `tools/kdf_bench.py` (FUP-03 Part 2, #489). The
parameters come from key files freshly written by `rustic init` (v0.11.2;
two repositories, both identical) and `restic init` (v0.19.0; three
repositories, which calibrated to `p=1` once and `p=2` twice). Each value
below is the fastest of seven guesses on **one core** of an Intel Westmere
E56xx (2010-era) VM, using
OpenSSL's scrypt through Python `hashlib`. A modern core is a few times
faster. An attacker multiplies by the cores, or the memory bandwidth, they can
afford.

| writer | N | r | p | memory/guess | s/guess | guesses/core-s |
|---|---|---|---|---|---|---|
| rustic 0.11 (LCSAS) | 131072 | 8 | 1 | 128 MiB | 0.706 | 1.4 |
| restic 0.19 (calibrated) | 32768 | 8 | 1 | 32 MiB | 0.170 | 5.9 |
| restic 0.19 (calibrated) | 32768 | 8 | 2 | 32 MiB | 0.312 | 3.2 |

Core-years to search **half** of a password space (the expected cost):

| entropy | guesses | rustic N=2^17 | restic N=2^15, p=1 |
|---|---|---|---|
| 40 bits | 5.5e11 | 1.2e4 | 3.0e3 |
| 50 bits | 5.6e14 | 1.3e7 | 3.0e6 |
| 60 bits | 5.8e17 | 1.3e10 | 3.1e9 |
| 70 bits | 5.9e20 | 1.3e13 | 3.2e12 |
| 80 bits | 6.0e23 | 1.4e16 | 3.3e15 |
| 90 bits | 6.2e26 | 1.4e19 | 3.3e18 |

Re-run with `tools/kdf_bench.py` (defaults), `--params N,r,p`, or
`--keys-dir <repo>/keys` for the parameters a given repository actually
records.

## 8. Findings from the Part 2 audit (2026-10-20, #489)

### 8.1 Repositories of different people

**Every data disc carries every repository's** catalog rows: repository names
and mirror paths, every snapshot's hostname, paths, tags and description,
locations, and the volume map. It also carries every repository's wrapped
keys. So whoever holds one data disc sees every repository's metadata, and can
brute-force any repository's password offline (§3). Repositories share
volumes, so no binpack setting changes that.

That is why an archive has one owner, who holds every repository's password.
People who need privacy from each other keep separate archives.

`PASSWORD_INFO.txt` tells the heir that a single password file likely works
for every repository (the common setup). That is true for one owner with one
password. Its wording is tracked on #491.

### 8.2 Shamir share cards and discs

SLIP-0039 is Shamir sharing: any **K−1 cards reveal nothing** about the
password. A disc adds only the wrapped key, which still needs that password.
So K−1 cards plus any number of discs leave an attacker exactly where §3 puts
them. `SHARE_FORMAT.md` on the meta disc documents a public standard, and
knowing the format weakens nothing. The one real risk is a card stored *with* a
disc; §5 and `ESTATE_PLANNING.md` already forbid that.

### 8.3 Decision: keep the full catalog on every data disc

Decided per the charter's default ("keep full catalog, document it"). A
redacted disc catalog would cost restorability:
- `db/rebuild.py` rebuilds a lost master catalog from discs by merging their
  `locations`, `volume_events` and snapshot rows (descriptions included).
  Redacting them means a rebuilt catalog no longer knows where the other discs
  are kept or what happened to each volume.
- The swap prompts and restore planning need the pack → volume map.
- Every data disc already burned keeps the full catalog anyway (§6).

The mitigations stay as they are: opaque repository and location names, and
custodian-style `key_storage_hints` (§5). Revisit only as an explicit opt-in if
an owner needs per-disc metadata confidentiality.
