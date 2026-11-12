# LCSAS: brief for the field session

Two Claude Code sessions share this project, on two machines. Read this
if you are the **field session**: the one with the optical writer, the
real discs and the real data. The root `CLAUDE.md` covers architecture
and commands; this file covers your role and what you can't learn from
the code. Written 2026-10-24 by the lab session at the hand-over;
updated 2026-10-30 and 2026-11-02 with the owner's decisions from the
grilling sessions.
The words in this file follow `CONTEXT.md`, the project glossary.

| | **Lab** (vm-desk1, the VM on home2) | **Field** (you) |
|---|---|---|
| Does | code changes, CI follow-up, heavy gates: blind-restore runs, cross-builds and bin-parity, `make audit-gate`, `make meta-gate`, CDEmu drills | release drills on real hardware, real backups (and, after final, real burns and the owner's meta disc), findings |
| Doesn't | touch real media or real data (it has no writer) | run heavy gates; land restore-path code |

## Where things stand

- **The current drill build is the newest `v1.0.0-rc*` tag**
  (`git tag -l 'v1.0.0-rc*' --sort=-v:refname | head -1`). Tracker #478
  announces each rc with its tag, commit and what changed. Drill only
  that rc, never an older one, and record the tag and commit of the rc
  you drilled in every drill result.
- Before each rc tag the lab runs the full haiku blind sweep (every
  runnable variant, split-key-docs twice) and builds a clean meta disc
  that must pass `lcsas meta verify --strict`. The results are in that
  rc's #478 announcement and the drill log.
- **Found by the sweep, #602:** tier 3 has no pack cache, so on a
  multi-disc archive it asks for a disc about once per restored file.
  It is still open: the rcs shipped without a fix, which the
  owner put after v1.0.0 final.
  The drills run tier 1, so they're not affected.
- **v1.0.0 final waits on three human/hardware gates** (tracker #478):
  - #473: physical BD-R and M-DISC burn → damage → repair → restore
  - #474: a cold restore by a non-technical stand-in (see below)
  - #476: a manual Windows restore
- **#475 (macOS) moved to after final** (owner, 2026-11-02): the owner has
  no Mac, and the heir will most likely use Windows. CI covers macOS
  `restore.sh` and the Mach-O tier-1 binaries; the Finder/Gatekeeper/
  quarantine path stays untested until #475 runs. The v1.0.0 tag and
  release notes say so.
- **Decided, not gates:** v1.0 ships without an independent crypto
  review. README and ESTATE_PLANNING disclose it, naming stock restic
  (tier 2b) as the hedge (#477, closed). CI stays on
  Ubuntu 24.04 for v1.0; #538 stays open for after it. No disc is
  re-burned because of its age, whatever the media type: discs are read
  back on the schedules in `docs/workflows/verify-and-audit.md`, and
  re-burned only after a read error (owner, 2026-10-31).
- **What "final" means:** when every gate has passed on one rc and the
  owner says go, the lab tags v1.0.0 on master, provided master's restore
  path (rule 3, plus the heir-facing text and `README.md`) is unchanged
  since that rc apart from the version bump. Owner-side code and docs
  (`cli/`, `db/`, the owner workflow guides) may still change. Otherwise
  the lab cuts another rc and the affected drills run again (owner,
  2026-11-02). At final the lab also builds a generic v1.0.0 meta disc,
  verifies it strictly and diffs it against the rc's.
- Other open issues are lab-side: #479 (retire it once #476 passes),
  #491 item 8 (location remove/rename), #573 (catalog rebuild keeps the
  split records), #581.

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

### Drills against the current rc

- **Order** (owner, 2026-11-02): #476 first, then #474 last, because each
  re-run of #474 needs a fresh stand-in. #473 runs whenever you start: it
  is the only gate that needs your machine, since the lab host's drive
  can't burn BD.
- **The drill archive** (owner, 2026-11-02): one archive with the default
  label prefix and two repositories, one whose password is split 2-of-3
  into share cards and one with an ordinary password. Two data discs plus
  a meta disc built from the drill config and catalog. The kit that
  builds it is `tests/e2e/drill_kit/` (#598): a fixed non-personal
  corpus, the drill config, and `verify_restore.py` to check a restore.
  The lab builds it from the current rc tag for #476 and #474; its README has
  the steps.

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
- **#476 (Windows) and #474 run in the lab**, on a Windows 10 VM on the
  lab host (owner, 2026-11-02). The lab builds the drill discs and
  inserts each one as virtual CD media, never as a copied-in ISO file:
  a copied file carries Mark-of-the-Web, so SmartScreen would show
  prompts a real disc never does. For #476 the owner clicks through; for
  #474 the owner plays the disc drive and inserts each disc the stand-in
  asks for. You don't need to do anything for these two.
- **#474 (non-technical person):** the owner's heir does NOT take this
  drill. Use a stand-in: someone non-technical, who is not the heir, on
  the same kind of computer the heir would use. Each re-run needs a new
  stand-in, since someone who has seen the docs is no longer cold. Give
  them the drill discs, the cards printed for the drill's test archive
  (the estate card, 2 of the 3 share cards, the password card) and
  nothing else. Observe; don't help. Write down where they got stuck,
  in their words, and the time per step. Refer to them by role only (rule 1). Findings go on the issue;
  fixes are usually heir-doc PRs. The real heir walks through it once
  after final.
- **#476 (Windows), and #475 (macOS) after final:** walk through
  `recovery/docs/RECOVER_WINDOWS.txt` / `docs/workflows/restore-host-macos.md`
  the way an heir would. Capture every OS prompt (SmartScreen, Defender,
  UAC, Gatekeeper, quarantine) and every place the docs and the screen
  disagree.
- **If a drill fails:** comment on its issue with the build, what
  happened and the evidence, and file a bug. The lab fixes it, sweeps all
  the blind variants, rebuilds the meta disc cleanly and cuts the next rc.
  Then rerun the failed drill, plus any drill whose path the fix changed.
  **When all three gates pass on one rc,** the lab tags final as described
  under "Where things stand".

### Real data before final

**No real burns until v1.0.0 final** (owner, 2026-10-30). Before final you
may set up the real config, back real data up into the mirror, and run
`lcsas scan`. Stop there: no `lcsas stage`, `burn` or `burn-iso` with real
data. After final, the rest of this section applies. (Lab advice, not an
owner rule: wait until final to split the real password too, so the share
cards print with the final wording.)

**The real archive is ONE archive** (owner, 2026-11-02): one repository's
password split into share cards, the others on ordinary passwords. Set
the split in that repository's own `[repos.<name>]` table, not under
`[defaults]` (`docs/ESTATE_PLANNING.md` §4). Keep the default label
prefix.

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
from the owner's config and catalog (owner, 2026-11-02): that is what puts
the owner's details and the split instructions on its `START_HERE.txt`. Its
volume label is always `LCSAS_META` (#588):

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
  - The macOS drill (#475, after final) needs a real Mac. The Windows
    drills run in the lab.
- **Light checks are fine here:** `make lint`, `make typecheck`,
  `make test-unit`, and single tests. Integration tests skip themselves
  when a tool is missing. Leave `make gate`, `make audit-gate`, the
  blind runs and the cross-builds to the lab.
- **Remotes:** clone from GitHub. The lab also mirrors to a private
  remote on home2; you don't need it.
