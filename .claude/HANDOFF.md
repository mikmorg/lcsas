# LCSAS: brief for the field session

Two Claude Code sessions share this project, on two machines. Read this
if you are the **field session**: the one with the optical writer, the
real discs and the real data. The root `CLAUDE.md` covers architecture
and commands; this file covers your role and what you can't learn from
the code. Written 2026-10-24 by the lab session at the hand-over.

| | **Lab** (vm-desk1, the VM on home2) | **Field** (you) |
|---|---|---|
| Does | code changes, CI follow-up, heavy gates: blind-restore runs, cross-builds and bin-parity, `make audit-gate`, `make meta-gate`, CDEmu drills | release drills on real hardware, real backups and burns, the owner's meta disc, findings |
| Doesn't | touch real media or real data (it has no writer) | run heavy gates; land restore-path code |

## Where things stand

- **`v1.0.0-rc2`** is tagged at `b033cb6`, an annotated tag; read it with
  `git tag -n99 v1.0.0-rc2`. It is the build every drill runs against.
  Record `v1.0.0-rc2 (b033cb6)` in every drill result.
- At rc2, all 8 runnable blind-restore variants scored 16/16 (the drill log
  in `recovery/docs/READINESS_CHECKLIST.txt`), and the release meta volume
  passed `meta verify --strict`. CI is green.
- **v1.0.0 final waits on three human/hardware gates** (tracker #478):
  - #473: physical BD-R and M-DISC burn → damage → repair → restore
  - #474: a cold restore by a real non-technical person
  - #475: a manual macOS restore
  - #476 (manual Windows) is related but doesn't gate the release.
  - #477 (an independent crypto review) is the owner's decision, not a gate.
- Other open issues are lab-side or owner decisions: #462, #479, #491,
  #538, #559, #562.

## Hard rules

1. **The repo is PUBLIC.** Never commit a real catalog, password, key
   share, share card, mirror path, hostname or serial, or a drill
   participant's name. Run logs and transcripts use roles ("the
   participant") and placeholder paths. Real catalogs, keys and staging
   trees live outside the checkout.
2. **Never touch** `recovery/UPSTREAM.sha256`, or `verify.sh`
   (`tests/e2e/cdemu_blind_restore/`). Never delete tests you didn't
   write.
3. **Don't land restore-path changes.** A change to `recovery/src`,
   `recovery/scripts`, `src/lcsas/restore` or `src/lcsas/meta` needs a
   haiku blind-restore run, and `recovery/src` also needs its committed
   binaries rebuilt (the bin-parity recipe). Only the lab can do either.
   File the issue, or push the branch, and hand it over (see Hand-back below).
4. **Merges.** Merge-commit only, never `--admin`, and only on green CI.
   The lab's standing permission to merge on its own doesn't carry over
   to you: merge only when the owner says so in your session.
5. **Debug or check builds only.** Build a release artifact only when
   the owner says "release" or "publish". This is the owner's global rule;
   copy their `~/.claude/CLAUDE.md` to your machine if it isn't there.

## Your work

### Drills against rc2

- **#473 (physical disc):** follow `recovery/docs/PHYSICAL_DISC_VALIDATION.txt`,
  steps 1–5:
  - Steps 1–2 burn a real volume. Size it so the RS03-padded image fits the
    disc, and capture a sha256 manifest of the sources first.
  - Step 3 restores blind from the real mount; Step 3b is the disc swap
    without ENTER (#455), on a desktop that automounts.
  - Steps 4–5 damage a disc below, then above, the ECC threshold.
  - Fill in the RUN LOG TEMPLATE at the end of that file and commit it
    under `tests/e2e/`, with no personal data (rule 1).
- **#474 (non-technical person):** give them the discs, the printed cards
  and nothing else. Observe; don't help. Write down, in their words, where
  they got stuck, and the time per step. Findings go on the issue. Fixes
  are usually heir-doc PRs.
- **#475 (macOS) and #476 (Windows):** walk through
  `docs/workflows/restore-host-macos.md` / `recovery/docs/RECOVER_WINDOWS.txt` the way
  an heir would. Capture every OS prompt (Gatekeeper, quarantine,
  SmartScreen, Defender, UAC) and every place the docs and the screen
  disagree.
- **If a drill fails:** comment on its issue with the build, what
  happened and the evidence, and file a bug. The lab fixes it, sweeps the
  blind variants and cuts rc3. Then rerun only the affected drills on rc3.
  **When all three gates pass on one rc,** the owner decides on v1.0.0
  final.

### Real backups

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
  copy is recorded, and the next `lcsas stage` offers its packs again.
  Re-burn with the command the error prints
  (`lcsas burn --session … --location …`).
- **Receipts:** a disc that failed verification is never a copy (BURN-05).
  `burn-iso --no-verify` records BURNED, not VERIFIED. Promote it with
  `lcsas verify <label> --disc`, which reads the disc, not the ISO file.
- **`optical_device`** isn't checked by `config check`. A typo shows up
  only at burn time.
- **Keys:**
  - `lcsas key split` makes SLIP-0039 share cards; run `lcsas key verify`
    each year.
  - `key_storage_hints` is printed **verbatim on every disc**, so it names a
    custodian, never the password (ESTATE_PLANNING.md).
  - Share cards and passwords never go into the repo or into an issue.
- **Recovery cards:** `lcsas estate card` covers the whole archive;
  `lcsas key card` is for an unsplit key.

### The owner's meta disc

The rc2 meta volume on vm-desk1 is a **generic** build, with no catalog
and no repo keys. The disc the owner keeps must be built on your machine,
from the owner's config and catalog:

- **Build from a clean checkout of the tag.** Use a detached
  `git worktree add --detach <dir> v1.0.0-rc2`, and run that worktree's
  own code: `PYTHONPATH=<dir>/src python3 -m lcsas … meta build
  --project-root <dir> --output <out>`. The builder copies `recovery/` as
  it stands, gitignored files included (#562).
- **The key gate (#437):** the build fails if a live repo's keys are
  missing, and `--allow-incomplete` doesn't override that.
- **The upstream cache** has to be populated first, ~1 GB, all pinned by
  hash: `make fetch-recovery`, or `LCSAS_RECOVERY_CACHE=<dir>
  sh recovery/scripts/fetch_upstream.sh`.
- **Then** run `lcsas meta verify --strict <out>`, which must PASS.
- **Master, burn and record it.** The docs give this step only in outline:
  see `docs/workflows/meta-volume.md`, "Refresh the meta-disc when LCSAS
  source changes", steps 4–6.
  - Steps 4–6 cover mastering the tree with xorriso (optionally with RS03
    ECC), recording the disc in the catalog by hand, and retiring the old
    disc.
  - `master_iso()` in `tests/e2e/fixture_lib.py` is a working xorriso call.
- **Rebuild the owner's meta disc** after every rc or release.

## Hand-back to the lab

The channel between the sessions is **GitHub issues** (`gh`, repo
`mikmorg/lcsas`). Cross-session messages are not a durable record.

- Anything the lab must do gets the **`needs-vm-desk1`** label:
  restore-path code, blind runs, binary rebuilds, the audit gate, a new rc.
  The issue says what's needed, which build, and the evidence: logs,
  `sha256sum -c` output, transcripts.
- You can land small changes outside the restore path yourself, under
  rule 4: drill logs, CLI messages, non-heir docs.
  - Heir-facing docs include START_HERE, KEY_INFO, RECOVER_*.txt,
    `recovery/docs/`, and the restore guides.
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
- **Bot commits on master are expected.** Once master has gone
  `KEEPALIVE_AFTER_DAYS` (about a month) without a commit,
  `.github/workflows/keepalive.yml` pushes an empty
  `chore: keepalive push ...` commit as github-actions[bot], so GitHub
  doesn't switch off the scheduled canaries (#462). `git pull --ff-only`
  takes it like any other commit.
