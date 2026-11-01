# LCSAS: brief for the field session

Two Claude Code sessions share this project, on two machines. Read this
if you are the **field session**: the one with the optical writer, the
real discs and the real data. The root `CLAUDE.md` covers architecture
and commands; this file covers your role and what you can't learn from
the code. Written 2026-10-24 by the lab session at the hand-over;
updated 2026-10-30 with the owner's decisions from the grilling session.
The words in this file follow `CONTEXT.md`, the project glossary.

| | **Lab** (vm-desk1, the VM on home2) | **Field** (you) |
|---|---|---|
| Does | code changes, CI follow-up, heavy gates: blind-restore runs, cross-builds and bin-parity, `make audit-gate`, `make meta-gate`, CDEmu drills | release drills on real hardware, real backups (and, after final, real burns and the owner's meta disc), findings |
| Doesn't | touch real media or real data (it has no writer) | run heavy gates; land restore-path code |

## Where things stand

- **Drill `v1.0.0-rc4` (tagged 2026-10-31; the tag and commit are in
  the announcement on tracker #478).** On 2026-10-30 the owner decided
  the glossary (`CONTEXT.md`) and the other fixes land BEFORE any drill,
  so the drills test the final wording. `v1.0.0-rc3` (`7a872ab`) was
  superseded the same day, before any drill, because the no-age re-burn
  decision below changes `DISC_CARE.txt` on every disc. Don't drill rc3
  or rc2. Record the tag and commit of the rc you drilled (rc4, or a
  later rc after a fix) in every drill result.
- At rc4, all 8 runnable blind-restore variants scored 16/16 (the drill log
  in `recovery/docs/READINESS_CHECKLIST.txt`), and the lab's clean meta
  disc build passed `lcsas meta verify --strict`.
- **v1.0.0 final waits on four human/hardware gates** (tracker #478):
  - #473: physical BD-R and M-DISC burn → damage → repair → restore
  - #474: a cold restore by a non-technical stand-in (see below)
  - #475: a manual macOS restore
  - #476: a manual Windows restore. This is now a gate (owner, 2026-10-30).
- **Decided, not gates:** v1.0 ships without an independent crypto
  review. README and ESTATE_PLANNING disclose it, naming stock restic
  (tier 2b) as the hedge (#477, closed). CI stays on
  Ubuntu 24.04 for v1.0; #538 stays open for after it. No disc is
  re-burned because of its age, whatever the media type: discs are read
  back on the schedules in `docs/workflows/verify-and-audit.md`, and
  re-burned only after a read error (owner, 2026-10-31).
- **What "final" means:** when every gate has passed on one rc and the
  owner says go, the lab tags v1.0.0 on master if master's restore path
  (rule 3) is unchanged since that rc; otherwise it cuts another rc and
  the affected drills run again. If the gates pass on rc4, final is rc4
  plus only the version bump to 1.0.0, with no rc5 (owner, 2026-10-31).
- Other open issues are lab-side: #479 (retire it once #476 passes),
  #491 (three of its items are in rc3; location remove/rename comes
  after), #569, #573, #581. #559, #562 and #571 are fixed since rc3.

## Hard rules

1. **The repo is PUBLIC.** Never commit a real catalog, password,
   share, share card, mirror path, hostname or serial, or a drill
   participant's name. Run logs and transcripts use roles ("the
   participant") and placeholder paths. Real catalogs, password files and
   staging areas live outside the checkout.
2. **Never touch** `recovery/UPSTREAM.sha256`, or `verify.sh`
   (`tests/e2e/cdemu_blind_restore/`). Never delete tests you didn't
   write.
3. **Don't land restore-path changes.** The restore path is everything
   that ends up on a disc or a printed card: all of `recovery/`,
   `src/lcsas/restore`, `src/lcsas/meta`, `src/lcsas/staging`, and the
   card text (`lcsas estate card`, `lcsas key card`, `lcsas key split`,
   and the `docs/PASSWORD_CARD.txt` template).
   A change there needs a haiku blind-restore run, and `recovery/src` also
   needs its committed binaries rebuilt (the bin-parity recipe). Only the
   lab can do either. File the issue, or push the branch, and hand it over
   (see Hand-back below).
4. **Merges.** Merge-commit only, never `--admin`, and only on green CI.
   The lab's standing permission to merge on its own doesn't carry over
   to you: merge only when the owner says so in your session.
5. **Debug or check builds only.** Build a release artifact only when
   the owner says "release" or "publish". This is the owner's global rule;
   copy their `~/.claude/CLAUDE.md` to your machine if it isn't there.

## Your work

### Drills against the current rc (rc3 first)

- **Drill discs hold test data only.** No real data is burned until
  v1.0.0 final (owner, 2026-10-30). Burn drill discs from a test corpus
  with nothing personal in it, using a separate drill config and catalog
  (`lcsas --config <drill.toml> --db <drill.db> …`), never the real ones,
  so no drill volume lands in the master catalog.
- **#473 (physical disc):** follow `recovery/docs/PHYSICAL_DISC_VALIDATION.txt`,
  steps 1–5:
  - Steps 1–2 build a volume from the test corpus on the real media type,
    and burn it. Size it so the RS03-padded image fits the disc, and
    capture a sha256 manifest of the sources first.
  - Step 3 restores blind from the real mount; Step 3b is the disc swap
    without ENTER (#455), on a desktop that automounts.
  - Steps 4–5 damage a disc below, then above, the ECC threshold.
  - Fill in the RUN LOG TEMPLATE at the end of that file and commit it
    under `tests/e2e/`, with no personal data (rule 1).
- **#474 (non-technical person):** the owner's heir does NOT take this
  drill. Use a stand-in: someone non-technical, who is not the heir, on
  the same kind of computer the heir would use. Each re-run needs a new
  stand-in, since someone who has seen the docs is no longer cold. Give
  them the drill discs, the cards printed for the drill's test archive
  and nothing else. Observe; don't help. Write down where they got stuck,
  in their words, and the time per step. Refer to them by role only (rule 1). Findings go on the issue;
  fixes are usually heir-doc PRs. The real heir walks through it once
  after final.
- **#475 (macOS) and #476 (Windows):** walk through
  `docs/workflows/restore-host-macos.md` / `recovery/docs/RECOVER_WINDOWS.txt` the way
  an heir would. Capture every OS prompt (Gatekeeper, quarantine,
  SmartScreen, Defender, UAC) and every place the docs and the screen
  disagree.
- **If a drill fails:** comment on its issue with the build, what
  happened and the evidence, and file a bug. The lab fixes it, sweeps all
  the blind variants, rebuilds the meta disc cleanly and cuts the next rc.
  Then rerun the failed drill, plus any drill whose path the fix changed.
  **When all four gates pass on one rc,** the lab tags final as described
  under "Where things stand".

### Real data before final

**No real burns until v1.0.0 final** (owner, 2026-10-30). Before final you
may set up the real config, back real data up into the mirror, and run
`lcsas scan`. Stop there: no `lcsas stage`, `burn` or `burn-iso` with real
data. After final, the rest of this section applies. (Lab advice, not an
owner rule: wait until final to split the real password too, so the share
cards print with the final wording.)

Read these in order: `docs/workflows/init-and-config.md` →
`backup-and-scan.md` → `stage-and-burn.md` → `verify-and-audit.md` →
`location-management.md`, then `docs/ESTATE_PLANNING.md` and
`docs/DISC_CONFIDENTIALITY.md`. If the writer is on a different machine
from the catalog, use `burn-iso-portable.md`.

Things the docs state, but that cost real time to learn:

- `--config`, `--db`, `--lock-timeout` and `--verbose` are **global**:
  they go *before* the subcommand (`lcsas --config X burn …`).
- **Media type:** use the real one (BD25, BD50, BDXL100, MDISC25/50/100).
  M-DISC and BD-R of the same size are separate types. TEST_TINY is for
  tests only.
- **A failed post-burn verify returns the volume to STAGING** (#483). No
  disc is recorded, and the next `lcsas stage` offers its packs again.
  Re-burn with the command the error prints
  (`lcsas burn --session … --location …`).
- **Burn receipts:** a disc that failed verification is never recorded
  (BURN-05). `burn-iso --no-verify` records BURNED, not VERIFIED. Promote
  it with `lcsas verify <label> --disc`, which reads the disc back, not
  the ISO image.
- **`optical_device`** isn't checked by `config check`. A typo shows up
  only at burn time.
- **Passwords and shares:**
  - `lcsas key split` splits a password into SLIP-0039 share cards; run
    `lcsas key verify` each year.
  - `key_storage_hints` is printed **verbatim on every disc**, so it says
    who keeps the password, never the password itself (ESTATE_PLANNING.md).
  - Share cards and passwords never go into the repo or into an issue.
- **Cards:** `lcsas estate card` prints the estate card (the whole archive,
  no secret, kept with the discs); `lcsas key card` prints a password card
  for a password that is not split (never kept with the discs).

### The owner's meta disc (after final)

The owner's own meta disc waits for v1.0.0 final (owner, 2026-10-30).
The meta disc the lab builds for each rc is a **generic** build with no
repository metadata. The disc the owner keeps must be built on your machine,
from the owner's config and catalog:

- **Build from a clean checkout of the tag.** Use a detached
  `git worktree add --detach <dir> v1.0.0`, and run that worktree's
  own code: `PYTHONPATH=<dir>/src python3 -m lcsas … meta build
  --project-root <dir> --output <out>`. From a git checkout the builder
  bundles only the git-tracked files under `recovery/` (#562), but it
  copies them with any local edits, so build from the clean worktree.
- **The key gate (#437):** the build fails if an active repository's
  keys are missing, and `--allow-incomplete` doesn't override that.
- **The upstream cache** has to be populated first, ~1 GB, all pinned by
  hash: `make fetch-recovery`, or `LCSAS_RECOVERY_CACHE=<dir>
  sh recovery/scripts/fetch_upstream.sh`.
- **Then** run `lcsas meta verify --strict <out>`, which must PASS.
- **Master and burn it.** The docs give this step only in outline: see
  `docs/workflows/meta-disc.md`, "Refresh the meta disc when LCSAS source
  changes", steps 4–6.
  - Steps 4–6 cover mastering the tree with xorriso (optionally with RS03
    ECC) and keeping the older meta discs. Nothing is recorded in the
    catalog: the meta disc is not a volume.
  - `master_iso()` in `tests/e2e/fixture_lib.py` is a working xorriso call.
- **Rebuild the owner's meta disc** after every release. Keep the old
  ones with the discs: an heir uses the newest meta disc, and falls back
  to the one burned closest in date to the data discs.

## Hand-back to the lab

The channel between the sessions is **GitHub issues** (`gh`, repo
`mikmorg/lcsas`). Cross-session messages are not a durable record.

- Anything the lab must do gets the **`needs-vm-desk1`** label:
  restore-path code, blind runs, binary rebuilds, the audit gate, a new rc.
  The issue says what's needed, which build, and the evidence: logs,
  `sha256sum -c` output, transcripts.
- You can land small changes outside the restore path yourself, under
  rule 4: drill logs, CLI messages, non-heir docs.
  - Heir-facing text includes START_HERE, PASSWORD_INFO, VOLUME_INFO,
    RECOVER_*.txt, `recovery/docs/`, the restore guides and the card text.
  - For those, open the PR and add the label, because the blind drills
    must re-run after they change.
  - Any file under `recovery/` also needs `make -C recovery manifest`,
    or the manifest freshness tests fail.

## Your machine

- **Python 3.12 and the repo:**
  - Use a venv, since Ubuntu's PEP 668 blocks a bare `pip install`.
  - `make dev` installs the package and the dev tools.
  - Run pytest from the same checkout the package is installed from: the
    test conftest refuses to run against another checkout's lcsas.
- **Tools:**
  - rustic 0.11.2, which matches the pinned recovery copy.
  - xorriso 1.5.6, plus dvdisaster 0.79.10 for ECC and the damage steps.
  - A burner tool: `xorrecord` or `growisofs`.
  - `lsscsi` to record the drive.
  - The macOS and Windows drills need the real OSes.
- **Light checks are fine here:** `make lint`, `make typecheck`,
  `make test-unit`, and single tests. Integration tests skip themselves
  when a tool is missing. Leave `make gate`, `make audit-gate`, the
  blind runs and the cross-builds to the lab.
- **Remotes:** clone from GitHub. The lab also mirrors to a private
  remote on home2; you don't need it.
