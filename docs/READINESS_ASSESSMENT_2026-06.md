# LCSAS Readiness Assessment — 2026-06

**Question asked:** *"Is this absolutely complete and ready for prime time?"*

**Short answer: No — and "absolutely complete" is the wrong bar to aim for.**

LCSAS is a genuinely impressive, unusually well-tested piece of engineering. But its
*entire purpose* is "bet your irreplaceable data on this surviving decades and being
restorable by a non-technical heir." That is the highest-stakes durability claim a
piece of software can make, and several of the load-bearing assumptions behind it are
**not yet proven** — some of them are *unprovable* by construction. This document is
deliberately adversarial. It is written by the same agent that did most of the recent
work, which is itself one of the concerns (see C8).

---

## Verdict by use case

| Use case | Ready? | Why |
|---|---|---|
| **Personal use by the author** (technical, owns the machine, understands the tiers) | **Yes, basically** | The Linux happy-path is heavily exercised and the author can debug. |
| **A technical heir restoring on Linux** | **Probably** | Blind-restore e2e proves this path with an AI proxy; tier-1/2/3 all exercised. |
| **A non-technical heir on Windows/macOS, years from now** | **Not proven** | The central promise — and the least-validated path. See C1, C2, C9. |
| **"Set and forget for 30 years"** | **An untested bet** | No accelerated-aging, no real-media rot cycle in automation. See C3, C4. |

---

## What is genuinely strong (not hand-waving)

- **Test discipline is real.** 543 recovery-hardening tests, doc-contract gates that
  fail when docs drift from code, fuzz harnesses, an ECC-repair proof against the real
  dvdisaster binary, differential tier-1↔tier-2 restore testing.
- **Defense in depth on the read path.** Three independent restore tiers + RS03 ECC
  beneath them; corrupt data is repaired-or-rejected, never silently restored.
- **Holographic catalog** means no central server is required to recover.
- **Coverage is high** (~98.5% on restore+meta+ecc) and the recent push found and fixed
  *real* correctness bugs (an RS03 decode divergence, a zstd Huffman bug, an FSE
  overflow) — evidence the tests have teeth.

Take the rest of this document as "what stands between 'impressive' and 'I would stake
a stranger's only copy of their family photos on it.'"

---

## Concerns & gaps (ranked by how much they undermine the core promise)

### C1 — The heir is a non-technical *human*, but the only end-to-end "heir" we test is an AI agent. **[highest]**
The blind-restore harness is excellent, but its "heir" is a Claude agent that reads docs,
runs CLI tools, mounts ISOs, swaps discs, and interprets tier-fallback. A grieving,
non-technical human under stress is a *categorically different* test subject. We have
**never watched a real non-technical person attempt a cold restore** with only the disc
and the printed cards. That is the single most important untested assumption in the
whole project, and no amount of AI-proxy passing rate substitutes for it.

### C2 — Cross-platform heir journeys are *built* but not *exercised end-to-end.
The blind e2e runs only on the host arch (x86_64-linux-musl). The other five approved
targets — aarch64/armv7 Linux, x86_64/aarch64 macOS, x86_64 Windows — are cross-compiled
and smoke-tested (wine `--check-disc`, qemu unit tests, doc contracts), but **the actual
full restore journey is never run on Windows or macOS.** For a system premised on "your
heir uses whatever machine they have," the two most likely heir platforms (Windows, Mac)
have the least end-to-end proof.

### C3 — No physical optical media in the loop. Everything is CDEmu/virtual.
The burn → real BD-R/M-DISC → drive read-back → physical bit-rot → RS03-repair cycle has
**never run end-to-end on real hardware in automation.** Real media has failure modes
virtual discs cannot reproduce: write errors, drive/media incompatibility, sector-level
rot, disc warping, reflective-layer degradation. There is a *manual* drill
(`PHYSICAL_DISC_VALIDATION.txt`), but a manual drill that is rarely run is close to no
drill. The RS03 math is proven against software-injected damage, not a real scratched disc.

### C4 — The durability claims are bets, not validated facts.
"C89 ABI-stable for 35 years," "M-DISC 100-year media," "restorable in 2050." These are
*reasonable* bets, but they are bets. No accelerated-aging test, no decades-scale ABI
validation, no proof that the 2050 toolchain still builds the source. The honest framing
is "designed to maximize the odds," not "will work."

### C5 — Committed tier-1 binaries can't be proven to match their source (#320).
zig's Mach-O/PE output is non-deterministic, so the macOS/Windows `lcsas-restore`
binaries are committed and *exempt* from the byte-identity gate. That means the binary an
heir actually runs cannot be reproducibly tied to the audited C source — a supply-chain /
trust gap exactly where trust matters most. (The Linux musl bins *are* reproducible; the
gap is macOS + Windows.)

### C6 — Custom crypto, single author, no external audit.
AES-CTR, Poly1305, scrypt, and SLIP-0039 are re-implemented in *both* pure Python and
C89. They pass published test vectors, which is necessary but not sufficient — constant-
time guarantees are effectively impossible in CPython, and a C89 parser of *untrusted*
input (restic packs, `catalog.db`, ISO9660) is one memory-safety bug away from
catastrophe. There is fuzzing, but no independent cryptographic or security review.

### C7 — Key escrow is a single point of *total, silent* failure.
Encryption means a lost/garbled K-of-N share set = data gone forever, with no partial
recovery and no warning until restore time. The weakest link is a human transcribing
20-word SLIP-0039 mnemonics off cards decades later — and that link is, again, only
"tested" by an AI proxy (C1). A typo'd share is recoverable; a *lost* share threshold is not.

### C8 — The audit was self-graded.
`DEEP_AUDIT_2026-06.md` found 78 issues; they were remediated and verified by the *same*
author/agent now assessing readiness. All 82 follow-up plans are marked RESOLVED — by the
party with the strongest incentive to mark them resolved. There has been **no independent
confirmation** that the fixes are correct and complete. Self-audit is valuable; it is not
the same as an audit.

### C9 — "Coverage ≥ 95%" is being treated as a proxy for "correct." It isn't.
This session hit 98.5% line coverage *and* found multiple latent correctness bugs in
fully-covered code. On a custom format/crypto decoder, line coverage is nearly orthogonal
to correctness. The real safety net is **differential testing** (tier-1 C vs tier-3 Python
vs upstream rustic vs real dvdisaster). That net exists but its breadth/corpus size is the
metric that actually matters, and it's not the headline number.

### C10 — The test apparatus itself is fragile.
In this session alone: the `/` partition hit 100% mid-run, worker agents repeatedly died
mid-`make gate`, a consolidated gate process died mid-integration, tests went flaky under
concurrent load (#327), and #320 dirtied the tree every gate until fixed. None of this is
"the product," but a fragile, single-machine, single-author CI pipeline is a poor
foundation for a multi-decade durability guarantee — and it makes "the gate is green"
less trustworthy than it sounds.

---

## Recommendations (in priority order)

1. **Run one real human, non-technical, cold restore.** Hand a real person the disc + the
   printed cards + the on-disc docs and *nothing else*. Watch silently. This single test
   will teach you more than the next 20 coverage points. (Addresses C1, C7, C9-UX.)
2. **Get the Windows and macOS blind journeys actually run end-to-end** — even once,
   manually, on real machines — and capture the transcript. (C2.)
3. **Do one real physical-media burn→rot→repair→restore** on actual BD-R *and* M-DISC,
   and automate as much of it as the hardware allows. Treat the manual drill as a release
   gate, not a someday. (C3.)
4. **Commission an independent security review** of the C89 tier-1 parser and the crypto
   implementations — the two places a single bug is unrecoverable. (C6.)
5. **Close the reproducible-build gap** for macOS/Windows bins, or stop committing them
   and document that those targets are build-from-source-only at recovery time. (C5.)
6. **Promote differential-testing breadth to a headline metric** (corpus size, % of
   restore decisions cross-checked across tiers), above line coverage. (C9.)
7. **Stabilize the CI substrate** (disk, concurrency, deterministic gates) before trusting
   "green" as a release signal. (C10.)
8. **Have someone who is not you re-audit a sample** of the 82 "RESOLVED" findings. (C8.)

---

## Questions I want you to answer (this is the "challenge")

1. **Who is the real heir, concretely?** Name the actual person who will restore this.
   What is their technical level? Have they *ever* seen the process? If the answer is
   "no one specific yet," the non-technical-heir requirement is aspirational, not designed-for.
2. **Have you, personally, done a full cold restore from real burned discs** — no shell
   history, no source tree, only the disc and the cards — within the last 6 months? If
   not, the system is unvalidated where it counts.
3. **What happens when the heir loses one share below the K threshold?** Is that an
   acceptable, understood, *communicated* failure mode — or a surprise that destroys the
   archive? Who is holding the shares, and have they been told what they're holding?
4. **If you died tomorrow,** does the person inheriting this know the archive exists, where
   the discs are, where the cards are, and that they must act before the media or the
   shares degrade? The best restore tooling is worthless if step 0 (the heir knows to try)
   fails.
5. **Are you willing to bet someone else's only copy of irreplaceable data on this?** Not
   your own — someone else's, who can't debug it. If yes, on which platform and why. If no,
   what specifically would have to be true first?
6. **What is your actual threat model?** House fire? Bit-rot over 30 years? A motivated
   adversary who has a disc and wants the contents? Ransomware on the hot tier? The design
   answers some of these well and others not at all — which ones actually matter to you?
7. **Why custom crypto and a custom C89 restorer at all,** versus age/restic/par2 with a
   one-page runbook? The in-house path buys durability independence; it also buys
   single-author, unaudited crypto. Is that trade still the right one, honestly?
8. **What is "done"?** "Absolutely complete" is unreachable for a multi-decade durability
   system. What is the *specific, falsifiable* bar at which you'd say "I trust this with
   the real thing"? If you can't state it, you can't hit it.

---

## Bottom line

LCSAS is **ready for the author to use on Linux today**, and is unusually well-built for a
single-author project. It is **not yet ready to be handed to a non-technical heir as a
"this will definitely work" guarantee**, because the three things that matter most for
that promise — a real human doing a real cold restore, on a real non-Linux machine, from
real physical media — are exactly the three things that have never actually been done.

The engineering is not the gap. The *validation against reality* is the gap.
