# LCSAS — Estate Planning for Digital Archives

> A guide for archivists who want their data to survive them.

---

## Why This Matters

If you are creating long-term backups, you need to plan for the
possibility that *you* will not be the one restoring them.  A family
member, executor, or heir may need to access these files decades from
now — possibly without any technical knowledge.

This document provides a checklist and templates to make that possible.

---

## Checklist

### 1. Physical Disc Management

- [ ] **Label every disc** with a permanent marker or printed label:
  - Archive name (e.g. "Smith Family Archive")
  - Volume label (printed on the disc by LCSAS, e.g. `LCSAS_BD25_2026_0001`)
  - Date burned
  - "META" on the meta-volume disc (this is the rescue disc)

- [ ] **Store discs in a binder or case**
  - Use a disc binder with individual sleeves (not spindle stacking)
  - Store vertically (like books on a shelf), not flat
  - Keep in a cool, dry, dark place (ideally 15-25°C, 30-50% humidity)
  - Avoid attics, basements, and areas with temperature swings

- [ ] **Maintain a paper manifest**
  - Generate a one-page whole-archive **Recovery Card** with
    `lcsas --config lcsas.toml estate card --output card.txt` — it fills in
    the owner, repositories, disc count (from the catalog), key scheme, and
    the literal first command an heir runs per operating system. No password
    is printed. Store this sheet WITH the disc binder.
  - Or print a list of all disc labels and what they contain by hand.
  - Update it each time you burn new discs.

### 2. Encryption Key Management

- [ ] **Choose the password like it WILL be brute-forced.** Every burned disc
  carries the password-locked key files forever, so a thief who steals any one
  disc can guess your password offline, in parallel, with no rate limit, on
  hardware that only gets faster. The only defense is entropy. **Generate** the
  password — do not invent it: use a diceware passphrase of **7+ words**
  (≈ 90 bits) or an equivalent random string from a password manager. You are
  betting against scrypt `N=2^15, r=8, p=1` (tens of ms/guess). See
  [DISC_CONFIDENTIALITY.md](DISC_CONFIDENTIALITY.md) for the full stolen-disc
  threat model, the entropy table, and why changing the password later does
  nothing for discs already burned.

- [ ] **Store the encryption key in multiple locations**:
  - Paper printout in a fireproof safe at home
  - USB drive in a bank safe deposit box
  - Password manager entry (shared vault or with beneficiary access)
  - Sealed envelope with your attorney or executor

- [ ] **Label the key clearly**:
  - "LCSAS Archive Encryption Key"
  - "Required to access backup discs labeled LCSAS_*"
  - Include the date and which repositories it unlocks

- [ ] **NEVER store the key on an archive disc**
  (The whole point of encryption is separation of key and data)

- [ ] **Print a Recovery Card per storage location**:
  - Generate one with `lcsas --config lcsas.toml key card --repo <name>`
    (the password is never printed; the card carries a transcription
    check code so a hand-copied password can be verified later), or
  - Fill in `docs/RECOVERY_CARD.txt` by hand.
  - Store each card with that location's physical records — never with
    the discs (key/data separation).

#### Option: Split the key into share cards (Shamir / SLIP-0039)

A single key copy is a single point of failure: lose it and the archive is
gone forever; let the wrong person find it and the archive is exposed. LCSAS
can instead **split the password into N share cards such that any K
reconstruct it and any K−1 reveal nothing** (default **2-of-5**). This biases
toward recoverability — the dominant risk for a backup is *loss*, not theft.

- [ ] **Split the password** once your archive is configured:

  ```
  lcsas --config lcsas.toml key split --repo REPO
  ```

  This writes `N` share files plus a plain-language **card** for each. Hand
  the cards to separate trusted holders / locations (e.g. three relatives,
  one safe deposit box, one attorney). No single holder can read the archive.
  With `--config`, the split's **K/N and identifier are recorded in the
  catalog** so disc instructions can be derived from the split you actually
  performed (and the next-step reminder is printed).

- [ ] **Mark the archive as split** so the discs print share instructions —
  set `key_split = true` (and your `key_threshold` / `key_shares`) under
  `[defaults]` in `lcsas.toml` (see §4). When split, every disc's
  `KEY_INFO.txt` and `START_HERE.txt` tell the heir to **first reconstruct
  the password, then restore normally**.

  > **Drift guard (KEY-08).** `lcsas stage` aborts if `key_split` / `key_threshold`
  > / `key_shares` in `lcsas.toml` disagree with the recorded split — so discs
  > can never print instructions that contradict the real split (e.g. config
  > says "any 2" while the split was 3-of-5, or `key_split=true` with no split
  > ever performed). The disc's K/N is taken from the recorded split when one
  > exists. `--allow-escrow-drift` overrides the abort for multi-config edge
  > cases (logged as a volume event).

- [ ] **Tell heirs the reconstruction is a two-step pre-step**, in your
  letter (template below):

  1. Gather any **K** share cards and run the combiner from the META disc.
     The primary combiner is the static, python-free `lcsas-keyshare`
     binary (one per platform under `recovery/bin/<machine>/`; Windows
     heirs run `lcsas-keyshare.exe`):

     ```
     recovery/bin/<machine>/lcsas-keyshare <card1> <card2>
     ```

     If that binary won't run on the host, the pure-Python fallback is
     equivalent:

     ```
     python3 keyshare_combine.py <card1> <card2>
     ```

     Either one prints the password (and nothing else).

  2. Run the normal restore (`restore.sh`) and enter that password at the
     `Password:` prompt — exactly the single-key flow.

  The share format and a from-scratch re-implementation guide are in
  `docs/KEY_SHARE_FORMAT.md`, bundled on every meta-volume.

#### ROTATION — when you change the repository password

Re-keying the repository (changing the password, or `rustic key` rotation)
**invalidates every distributed share card immediately.** The shares
reconstruct the *old* password, which no longer unlocks the repo — and
nothing in the cards themselves says so. A holder cannot tell a current card
from a void one by looking at it.

`lcsas key split` defaults to verifying the password against the repo's real
key files before writing any card, and re-checks the written cards round-trip,
so it will refuse to escrow a password that does not unlock the repo. But
that protects the *moment of splitting* — it cannot reach cards already in the
field after a later re-key.

Procedure after any re-key:

1. **Re-split** with the new password: `lcsas --config ... key split --repo R`.
   Each card is stamped with `Split on : <date>` and a `Split ID : NNNNN`
   (the SLIP-0039 identifier, shared by all cards of one split, different
   between splits) so you can tell card generations apart in the binder.
2. **Redistribute** the new cards to the same holders/locations.
3. **Recall and destroy** the superseded cards (shred / incinerate). Note the
   superseded `Split ID` in your estate notes so a stray old card is
   recognisable as void.
4. **Confirm** the new cards work end-to-end:
   `lcsas --config ... key verify --repo R --share-file CARD1 --share-file CARD2`
   (any K). Exit 0 + an `OK:` line means the new escrow is good.

The annual READINESS drill (`recovery/docs/READINESS_CHECKLIST.txt`,
"ENCRYPTION KEY REDUNDANCY") runs that same `lcsas key verify` so a silent
rotation drift is caught within a year, not at recovery.

### 3. Letter to Heirs

- [ ] **Write a letter** and store it with the disc binder:

---

### Template: Letter to Heirs

```
Dear [Name],

In this binder you will find backup discs containing [describe your
files — family photos, financial records, creative work, etc.].

To access the files on these discs:

1. Find the encryption key:
   [Describe WHERE you stored it — "the blue USB drive in the home
   safe", "sealed envelope at Smith & Jones Law Firm", etc.]

2. Find the META disc in this binder.  It contains all the software
   needed to restore the files.

3. Insert the META disc into any computer — Windows, macOS, or Linux
   all work.  Open the file called START_HERE.txt — it has
   step-by-step instructions for each system.

4. For a complete walkthrough (including how to boot a free Linux USB
   stick if that turns out to be easiest), see RECOVERY_GUIDE.md on
   the META disc, or the printed copy in this binder.

5. If you are not comfortable doing this yourself, take ALL the discs
   AND the encryption key to a computer professional.  The instructions
   are on the discs — they don't need to know this system.

Important: WITHOUT the encryption key, the data CANNOT be recovered.
Keep the key safe but accessible to someone you trust.

With love,
[Your name]
[Date]
```

---

### 4. Configuration

- [ ] **Fill in the [survivability] section** of your `lcsas.toml`:

```toml
[survivability]
archive_owner = "Your Full Name"
archive_description = "Family photos, videos, and documents 2000-2025"
# NOTE: key_storage_hints is printed VERBATIM on every disc. Write it to point
# an heir to the password WITHOUT handing it to a disc thief — name a custodian,
# not a findable cache. See DISC_CONFIDENTIALITY.md §5.
key_storage_hints = "Sealed envelope with the family attorney (see my will)"
technical_contact = "Jane Doe (jane@example.com) or any Linux IT professional"

[defaults]
# Only if you split the password into share cards (see §2):
key_split = true       # mark this archive as split — prints share instructions
key_threshold = 2      # K: share cards needed to reconstruct
key_shares = 5         # N: share cards produced
```

This information is automatically written to `START_HERE.txt` on every
disc you burn — so your heirs can read it even without this document.
When `key_split = true`, each disc's `START_HERE.txt` and `KEY_INFO.txt`
also include the two-step share-reconstruction pre-step.

### 5. Periodic Maintenance

- [ ] **Re-burn discs every 5-10 years** (even M-Disc degrades eventually)
- [ ] **Verify existing discs** periodically.  To re-check every burned
  volume in one pass (against its catalog hashes):
  ```
  lcsas verify --all
  ```
  Add `--disc` to read the physical discs from the drive instead of the
  staged ISO files, or verify one volume at a time with
  `lcsas verify <LABEL> --iso /path/to/<LABEL>.iso`.
- [ ] **Update your letter** when you burn new discs or change key storage
- [ ] **Tell someone trusted** that these discs exist and where to find them
- [ ] **Keep a Blu-ray drive available** — as optical drives disappear from
  consumer hardware, you may need to buy a USB Blu-ray drive separately
- [ ] **If your archive uses 100 GB BDXL media, verify the stored drive is
  BDXL-capable** — many ordinary BD drives are NOT, and a non-BDXL drive
  reads none of the 100 GB discs

---

## Quick Reference: What's on Each Disc

| File | Purpose |
|------|---------|
| `START_HERE.txt` | Plain-language guide for non-technical users |
| `RESTORE_INSTRUCTIONS.txt` | Step-by-step technical restore procedure |
| `KEY_INFO.txt` | Which encryption key(s) are needed |
| `volume_info.json` | Machine-readable disc identity |
| `catalog.db` | SQLite database of all pack locations |
| `data/` | Encrypted backup data (pack files) |
| `metadata/` | Repository metadata (index, snapshots, keys) |

The **meta-volume** additionally contains:

| File | Purpose |
|------|---------|
| `restore.sh` | Automated restore script |
| `README_RESTORE.md` | Detailed restore guide (Markdown) |
| `README_RESTORE.txt` | Same content, plain text |
| `tools/` | Portable Linux binaries (rustic, xorriso, python3) |
| `lcsas/` | Full LCSAS source code |
| `docs/` | Architecture docs, format specifications, recovery guide |

> **Print and include** `docs/RECOVERY_GUIDE.md` in your disc binder.
> It covers all restore scenarios step by step, including how to get
> Linux and what to do if something goes wrong.
