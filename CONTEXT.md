# LCSAS

LCSAS turns backups into self-describing optical disc archives, so that an
heir can restore the data decades later with nothing but the discs and the
printed cards.

## Language

### Discs and volumes

**Volume**:
The logical unit of the archive: a set of packs mastered together under one
label. A volume exists as one ISO image and as zero or more discs.
_Avoid_: media volume, physical volume

**Disc**:
One physical burned instance of a volume. "Copy" appears only as a count, as
in "two copies of every volume".
_Avoid_: disk, volume copy

**In-service disc**:
A disc that is kept and relied on to hold its volume.

**Deprecated disc**:
A disc taken out of service that still exists, because it is damaged or its
volume has been deprecated.

**Destroyed disc**:
A disc deliberately destroyed by the owner.

**Lost disc**:
A disc whose whereabouts are unknown. Unlike a destroyed disc, it may be in a
stranger's hands.

**Verified volume**:
A volume at least one of whose discs has been read back in full and matched
against its ISO image. "Verify", unqualified, means reading a disc back.

**ISO image**:
The file a volume is mastered into before it is burned onto discs.

**Label**:
The unique name of a volume, also written on each of its discs. It ends in
the year the volume was made and a number that grows with each new volume,
so the labels show which volume is newest.

**Media type**:
The kind and capacity of blank disc a volume is sized for, such as 25 GB
BD-R or 100 GB M-DISC.
_Avoid_: media tier

**Meta disc**:
The disc that carries the recovery tools, instructions and source code needed
to read the archive on a computer with nothing installed. It is not a volume
and holds no catalog. Every generation of meta disc can read every disc; an
heir uses the newest one.
_Avoid_: meta-volume, rescue disc, recovery disc, recovery medium

**Deprecated volume**:
A volume that has been superseded: another volume holds every pack on it, so
its discs are no longer needed. A volume whose discs are gone is a lost
volume, not a deprecated one.

**Lost volume**:
A burned volume with no disc left in service. Its packs must be burned again
unless another volume already holds them. A surviving ISO image does not make
it less lost; it only makes burning it again cheap.

**Consolidation**:
Moving the packs of several volumes onto a new volume, so that the old ones
can be deprecated.

**Pack**:
One pack file from a repository: the unit that LCSAS places on volumes.
_Avoid_: chunk

**Pack ID**:
A pack's name: the hex SHA-256 of its contents. A catalog row number is not a
pack ID.
_Avoid_: pack hash, pack SHA

**Location**:
A named place where discs are kept, such as an offsite safe or a home shelf.
Only discs have a location; where a card or a password is kept is not one.
_Avoid_: site, vault, storage location

**Media batch**:
Blank discs from one manufacturing lot, which may age and fail together.
_Avoid_: batch, for a burn session

**Burn receipt**:
The record of one disc's burn: its volume, where the disc went, when, and the
ISO image's hash. It is printed and filed with the disc, and holds no secret.

### Backups and burning

**Repository**:
One backup repository, with its own password. Several repositories share the
same discs.
_Avoid_: tenant

**Retired repository**:
A repository that is no longer backed up but whose discs are kept. "Retire"
is used only for repositories.

**Mirror**:
The owner's live on-disk copy of a repository: backups write to it, and discs
are burned from it.
_Avoid_: hot tier, Tier 0

**Snapshot**:
One backup's point-in-time record of a repository's files. An heir restores a
snapshot, usually the latest.

**Repository metadata**:
A repository's index, snapshot and key files, copied onto every disc so that
any one disc describes its repository. "Meta" alone means only the meta disc.
_Avoid_: meta, rustic metadata

**Prune**:
Rustic deleting packs from a mirror that no snapshot needs any more. A pruned
pack may still exist on discs.

**Staging area**:
The temporary working space where volumes are assembled into ISO images
before burning.
_Avoid_: Tier 1, warm tier

**Burn session**:
The volumes staged and burned together in one run.
_Avoid_: batch, disc set, session (in heir-facing text)

**Catalog**:
The database that records every repository, pack, volume, disc and location
in the archive.
_Avoid_: database, db

**Master catalog**:
The owner's live catalog.
_Avoid_: hot db, archive db

**Disc catalog**:
The copy of the catalog burned onto a disc, current as of that disc's burn.
The newest volume's discs carry the fullest disc catalog, so a restore starts
from one of them. A disc burned later to replace a disc of an old volume
carries that old volume's catalog.
_Avoid_: holographic catalog, on-disc catalog

### Recovery

**Restore**:
The act of getting files back from the archive.
_Avoid_: recover, recovery (as the act)

**Recovery tier**:
One step in the ordered fallback list of ways to read the archive. "Tier"
means only this: a storage stage is called by its name, and a disc size is a
media type. "Recovery" otherwise only names the kit that makes a restore
possible, as in recovery tools. The tiers are the C reader (tier 1), rustic
(tier 2), stock restic (tier 2b) and the Python reader (tier 3).
_Avoid_: storage tier, media tier, pure-Python restorer, standalone restorer

### Secrets and cards

**Password**:
The secret that unlocks one repository's data. It is what an heir types to
restore.
_Avoid_: encryption key, key, key file

**Password file**:
A file containing a password.
_Avoid_: key file

**Split**:
Dividing a password into N shares, any K of which rebuild it. The catalog
records only that a split happened, with its K and N; it holds no share.
_Avoid_: key escrow, escrow

**Share**:
One of the N pieces a password is split into; any K of them rebuild it.
_Avoid_: key share

**Key**:
One of a repository's internal locked key files, which the password opens.
Heirs never handle keys, and "key" never means the password.

**Estate card**:
The printed overview of the whole archive. It holds no secret and is kept
with the discs.
_Avoid_: recovery card, archive card

**Password card**:
The printed card on which the owner writes one repository's password. It is
never kept with the discs.
_Avoid_: recovery card, key card

**Share card**:
The printed card carrying one share. It is never kept with the discs.
_Avoid_: key share card

### People

**Owner**:
The one person who builds and maintains an archive and holds every
repository's password. People who need privacy from each other keep separate
archives.
_Avoid_: operator, user

**Heir**:
Anyone restoring from the archive without the owner's help. An executor, in
the legal sense, is a separate role.
_Avoid_: inheritor, recipient, operator

**Share holder**:
A person who keeps one share card. A share holder need not be an heir.
