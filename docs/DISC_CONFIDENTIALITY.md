# Disc confidentiality — the stolen-disc threat model

> **One-line version:** A thief holding any single LCSAS disc gets the entire
> archive *catalog* in plaintext and every repository's *password-locked* key
> files. They do **not** get your file contents unless they also guess your
> password. The only thing standing between a stolen disc and your data is the
> strength of that password — so make it long and random, and choose escrow
> locations as if every disc carries this inventory, because it does.

This document is the deliberate, written threat model for **confidentiality**
of a burned LCSAS disc (restorability findings live elsewhere). It exists so
that an owner choosing where to store discs and key cards can weigh the
tradeoff with eyes open. It is referenced from `docs/ESTATE_PLANNING.md`,
`docs/guides/survivability.md`, and `recovery/docs/TIERS.txt`, and is bundled on
every meta-volume.

---

## 1. What someone holding ONE disc learns (plaintext)

Every data disc *and* the meta disc carry, for **every tenant** in a
multi-tenant archive, the following in the clear:

- **The complete archive catalog** (`catalog.db`, a SQLite database burned
  verbatim onto every disc). That includes:
  - every snapshot's **hostname, backup paths, tags, and description**;
  - every repository's **name and mirror path**;
  - every **location name** and the full **volume-copies map** — i.e. where
    every other copy of every other disc is stored;
  - the **volume_events audit trail**.
- **Every repository's rustic `keys/` directory** — the scrypt-wrapped master
  keys. The key-file JSON additionally carries the **plaintext `username` and
  `hostname`** of the machine that created the repo (a content-addressed
  artifact; see §6).
- **The owner's `key_storage_hints`**, printed verbatim into `START_HERE.txt`
  and `KEY_INFO.txt` on every disc.

In short: a single disc reveals your whole backup *topology and metadata*, and
hands the attacker the locked vault door (the wrapped keys) to attack offline
at leisure.

## 2. What they do NOT get

- **Your actual file contents.** Pack data is encrypted with AES-256 and
  authenticated (Poly1305 / HMAC-SHA-256); the audited crypto is sound. The
  packs are useless without the repository password.
- **The password itself.** It is never written to any disc.

So the entire confidentiality of your *data* reduces to one question: can the
attacker recover the password?

## 3. The single defense: password entropy vs. offline guessing

Because the wrapped key files are on every disc, an attacker can run an
**unlimited, offline, parallel brute-force** against the password — forever,
on hardware that only gets faster. There is no rate limit, no lockout, no
server to take offline. The Key Derivation Function is the only speed bump:

```
scrypt(password, salt, N=2^15 (32768), r=8, p=1, dkLen=64)
```

(See `docs/RESTIC_FORMAT_SPEC.md` §3.1; the live key files record `N=32768`.)
scrypt at these parameters costs on the order of tens of milliseconds per
guess on a CPU and is memory-hard, which blunts GPU/ASIC speedups — but it is
*not* a substitute for password entropy over a multi-decade horizon.

### Entropy guidance (order-of-magnitude; see §7 for measured numbers)

| Password entropy | Practical resistance to offline guessing |
|---|---|
| ~40 bits (e.g. a short human-chosen password) | hours-to-days on a 2026 GPU rig — treat as **broken** |
| ~60 bits | resists casual attack today; erodes with hardware over decades |
| **≥ 90 bits** (≈ 7+ EFF-diceware words) | **out of reach through the 2060s** under any plausible scaling — recommended |

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
destruction of every copy of every disc** that carries the affected key files.
Plan accordingly: the password you burn is, in practice, the password those
discs will carry for their entire physical lifetime (100+ years for M-Disc).

## 5. Escrow and card-placement guidance

- **Treat every disc as carrying the full inventory in §1** when choosing
  offsite or escrow locations. A disc in a given place exposes that place to
  whatever §1 reveals.
- **Never store a key-share card (or the password, or a recovery card) *with*
  a disc.** Key/data separation is the whole point: a thief who gets both a
  disc and a card (above threshold) has everything.
- **`key_storage_hints` is printed on every disc.** Write it so an heir can
  *locate* the password without a thief being *handed* it — name a custodian,
  not a findable cache. Good: "Sealed envelope with the family attorney (see
  my will)." Bad: "USB in safe deposit box #1234 at First National Bank."

## 6. Why the keys are on every disc anyway (a documented decision)

The wrapped key files and the full catalog ride on every disc **on purpose**:
it is the *holographic single-disc-recovery* property. An heir who finds any
one disc — plus the password — can restore, with no central server and no
other disc required. For this system, **restorability is the bar**, and it is
deliberately chosen over confidentiality of the metadata and the wrapped
(not bare) keys. This is a tradeoff made with intent, not an accident:

- The bare key is *never* exposed; only the scrypt-wrapped form is, and it is
  only as weak as the password protecting it (§3).
- The plaintext catalog is metadata, not file contents.

Any future "sanitized on-disc catalog" option would affect **newly burned
discs only** — every already-burned disc carries the full plaintext catalog
forever. See the follow-up audit charter in the FUP-03 plan
(`plans/audit-2026-06/FUP-03-disc-confidentiality-threat-model.md`).

The key files' plaintext `username`/`hostname` are part of restic's
content-addressed key artifact; scrubbing them at inject time is examined in
the charter (likely infeasible without breaking content addressing).

## 7. Verified numbers

> _Reserved for the FUP-03 Part 2 charter._ The KDF parameters quoted above
> are from `docs/RESTIC_FORMAT_SPEC.md`; the entropy table in §3 is
> order-of-magnitude guidance. A measured pass (read the KDF params the live
> rustic writer actually records in fresh key files, benchmark guesses/sec on
> current hardware, and derive the table from data) will replace the guidance
> table here. Charter benchmark tooling will land under `tools/`.
