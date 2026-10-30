# ADR 0002 — "Tier" means only a recovery tier

- Status: Accepted
- Date: 2026-10-30
- Issue: none (owner decision Q5 of the 2026-10-30 review; glossary in
  [`CONTEXT.md`](../../CONTEXT.md))

## Context

Before the glossary, the word "tier" had four meanings in the docs, the
on-disc text and the code comments:

1. **Recovery tier**: a step in the ordered fallback list of ways to read
   the archive. Tier 1 is the C reader (`lcsas-restore`), tier 2 is rustic,
   tier 2b is stock restic, tier 3 is the Python reader
   (`recovery/docs/TIERS.txt`, `recovery/scripts/restore.sh`).
2. **Storage tier**: where data sits on its way to the archive. "Tier 0 —
   HOT" was the mirror, "Tier 1 — WARM" the staging area and "Tier 2 —
   COLD" the burned discs (`CLAUDE.md`, `docs/architecture.md`).
3. **Storage tier, numbered one higher**: `src/lcsas/config/settings.py`
   called the mirror "Tier 1" and the staging area "Tier 2".
4. **Media tier**: the disc sizes and kinds, such as "M-Disc tiers" or
   the "BDXL tier".

On top of that, the "Recovery tier:" header field of several workflow
docs described a storage stage, not a recovery tier. So "tier 1" meant
the C reader, the mirror or the staging area, and "tier 2" meant rustic,
the staging area or the discs themselves. The owner can tell these apart from
context. An heir cannot. The heir reads "tier 2" in `TIERS.txt`,
`RECOVER.txt` and the other on-disc guides, and has to act on it while
restoring alone. A sentence like "tier 2 failed, try tier 3" must
not leave the heir wondering whether something is wrong with the discs.

## Decision

1. **"Tier" means only a recovery tier.** The tiers are the C reader
   (tier 1), rustic (tier 2), stock restic (tier 2b) and the Python reader
   (tier 3).
2. **Storage stages are called by their names, never numbered:** the
   **mirror** (the owner's live on-disk copy of a repository), the
   **staging area** (the temporary space where ISO images are assembled)
   and the **discs**. A doc header that described storage as "Recovery
   tier:" now reads "Storage stage:".
3. **Disc sizes are media types** (`config/media.py`), never "media tiers".

Only human-readable text changes. Identifiers, file names, flags and
Make targets that contain "tier" (`TIER1_FROZEN_SURFACE`,
`LCSAS_TIER1_SCHEMA_MAX`, `TIERS.txt`, blind-restore variant names
such as `tier1-missing`) keep their names: each of them already refers
to a recovery tier.

## Consequences

- Every "tier" an heir meets names one of four ways to read the discs.
  The heir-facing docs can say "if tier 1 does not run, use tier 2"
  without a second meaning.
- Writers have to name the storage stage in full ("the mirror", "the
  staging area"), which is a little longer than "Tier 0".
- Point-in-time reports such as `docs/READINESS_ASSESSMENT_2026-06.md`
  are preserved as written and may still say "hot tier"; they are
  history, not guidance.
- New text is reviewed against [`CONTEXT.md`](../../CONTEXT.md), which
  lists "storage tier" and "media tier" under _Avoid_.
