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
  - Volume label (printed on the disc by LCSAS, e.g. `LCSAS_BD25_001`)
  - Date burned
  - "META" on the meta-volume disc (this is the rescue disc)

- [ ] **Store discs in a binder or case**
  - Use a disc binder with individual sleeves (not spindle stacking)
  - Store vertically (like books on a shelf), not flat
  - Keep in a cool, dry, dark place (ideally 15-25°C, 30-50% humidity)
  - Avoid attics, basements, and areas with temperature swings

- [ ] **Maintain a paper manifest**
  - Print a list of all disc labels and what they contain
  - Store the manifest WITH the disc binder
  - Update it each time you burn new discs

### 2. Encryption Key Management

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

#### Option: Split the key into share cards (Shamir / SLIP-0039)

A single key copy is a single point of failure: lose it and the archive is
gone forever; let the wrong person find it and the archive is exposed. LCSAS
can instead **split the password into N share cards such that any K
reconstruct it and any K−1 reveal nothing** (default **2-of-5**). This biases
toward recoverability — the dominant risk for a backup is *loss*, not theft.

- [ ] **Split the password** once your archive is configured:

  ```
  lcsas key split --repo REPO --config lcsas.toml
  ```

  This writes `N` share files plus a plain-language **card** for each. Hand
  the cards to separate trusted holders / locations (e.g. three relatives,
  one safe deposit box, one attorney). No single holder can read the archive.

- [ ] **Mark the archive as split** so the discs print share instructions —
  set `key_split = true` (and your `key_threshold` / `key_shares`) under
  `[defaults]` in `lcsas.toml` (see §4). When split, every disc's
  `KEY_INFO.txt` and `START_HERE.txt` tell the heir to **first reconstruct
  the password, then restore normally**.

- [ ] **Tell heirs the reconstruction is a two-step pre-step**, in your
  letter (template below):

  1. Gather any **K** share cards and run the combiner from the META disc:

     ```
     python3 keyshare_combine.py <card1> <card2>
     ```

     It prints the password (and nothing else).

  2. Run the normal restore (`restore.sh`) and enter that password at the
     `Password:` prompt — exactly the single-key flow.

  The share format and a from-scratch re-implementation guide are in
  `docs/KEY_SHARE_FORMAT.md`, bundled on every meta-volume.

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

3. Insert the META disc into a computer running Linux.  Open the
   file called START_HERE.txt — it has step-by-step instructions.

4. For a complete walkthrough (including how to get Linux if you
   don't have it), see RECOVERY_GUIDE.md on the META disc, or the
   printed copy in this binder.

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
key_storage_hints = "Paper copy in the home safe; USB copy at First National Bank safe deposit box #1234"
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
- [ ] **Verify existing discs** periodically:
  ```
  lcsas verify --isos /path/to/your/disc/images/
  ```
- [ ] **Update your letter** when you burn new discs or change key storage
- [ ] **Tell someone trusted** that these discs exist and where to find them
- [ ] **Keep a Blu-ray drive available** — as optical drives disappear from
  consumer hardware, you may need to buy a USB Blu-ray drive separately

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
