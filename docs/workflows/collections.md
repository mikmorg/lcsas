# Collections

A **collection** is the set of discs kept at one location: which
repositories go there, on which media type, and how full a disc must be
before it is worth burning. You describe each collection once, in
`lcsas.toml`, and `lcsas stage --collection <name>` applies it (#701).

Without collections, an archive that sends different repositories to
different places on different media needs a wrapper script and side
files that must agree with `lcsas.toml`. Nothing checks that they do.
With collections, `lcsas config check` checks them.

Two collections that hold the same repositories make two full copies of
the archive, at two locations, possibly on two media types.

A collection chooses which repositories' **packs** go to a location. It
does not choose what each disc describes: like every LCSAS disc, a
collection's disc also carries the whole disc catalog and the
repository metadata (encrypted index, key and snapshot files) of every
repository in the archive, as `HolographicInjector` copies them. Each
repository stays encrypted under its own password, but anyone holding
the disc can see the names of all the archive's repositories. See
[DISC_CONFIDENTIALITY.md](../DISC_CONFIDENTIALITY.md).

## Table of contents

1. [The config](#the-config)
2. [`lcsas config check`](#lcsas-config-check)
3. [`lcsas stage --collection`](#lcsas-stage---collection)
4. [Minimum fill: `--min-fill` and `--partial`](#minimum-fill---min-fill-and---partial)
5. [`status --json`](#status---json)
6. [A routine](#a-routine)
7. [Test coverage](#test-coverage)
8. [Source refs](#source-refs)

---

## The config

```toml
[repos.family]
mirror_path   = "/mnt/mirror/family"
password_file = "/root/keys/family.key"
sources       = ["/srv/photos", "/srv/letters"]

[repos.work]
mirror_path   = "/mnt/mirror/work"
password_file = "/root/keys/work.key"

[collections.home]
location     = "Home_Shelf"
media_type   = "BD25"
min_fill_pct = 90
repos        = ["family", "work"]

[collections.bank]
location     = "Bank_Box"
media_type   = "MDISC100"
min_fill_pct = 90
repos        = ["family"]
```

`[collections.<name>]` keys:

| Key | Required | Meaning |
|-----|----------|---------|
| `location` | yes | The location the discs are kept at. It must be registered with `lcsas location add`. |
| `repos` | yes | A non-empty list of `[repos.<name>]` names. |
| `media_type` | no | The media type of the volumes. Default: `[defaults] media_type`. |
| `min_fill_pct` | no | 0 to 100. A last volume filled less than this is not staged. Default: 0 (no minimum). |

`sources` in a `[repos.<name>]` table is optional: the folders backed up
into that repository, as a list of strings. **It is descriptive only.**
LCSAS does not run backups; you run `rustic backup` yourself (see
[backup-and-scan.md](backup-and-scan.md)). LCSAS shows the list in
`lcsas repo list`, so one file describes each repository fully. It is
never written to a disc.

Every command loads the config, so every command refuses one in which a
collection has no location or no repositories, names a repository with
no `[repos.<name>]` table, has an unknown media type, or a minimum fill
outside 0 to 100. The error names the collection and the key.

**Source refs:** `load_config`, `_load_collections`, `CollectionConfig`
(`src/lcsas/config/settings.py`).

---

## `lcsas config check`

```bash
lcsas --config lcsas.toml config check
```

Besides the usual checks ([init-and-config.md](init-and-config.md)), for
every collection it checks that:

- each repository has a readable `password_file` (not needed when
  `allow_unverified_repo_format = true`);
- the location is registered in the catalog;
- each repository is registered in the catalog and active (not retired).

The catalog checks need a catalog. Before `lcsas init` the check says
they were not done, and still passes. Run it again after `lcsas init`,
`lcsas repo add` and `lcsas location add`.

An unknown location or repository fails the check (exit 1). An unknown
location gets a "did you mean" suggestion.

**Source refs:** `cmd_config_check`, `_config_check_collections`,
`_collection_catalog_check` (`src/lcsas/cli/commands/config.py` and `src/lcsas/cli/commands/stage.py`);
`collection_password_errors` (`src/lcsas/config/settings.py`).

---

## `lcsas stage --collection`

```bash
lcsas --config lcsas.toml stage --collection bank --dry-run
lcsas --config lcsas.toml stage --collection bank
```

This is `stage --for-location Bank_Box --repo family --media MDISC100
--min-fill 90`, with the values read from the config:

- Only the collection's repositories are staged.
- It stages the packs that are not on a disc at the collection's
  location, as `--for-location` does
  ([location-management.md](location-management.md)). The new volumes
  are recorded as staged for that location. Packs already on a volume
  waiting to be burned for that location are not staged again.
- The volumes use the collection's media type.
- The collection's minimum fill applies (see the next section).
  `--min-fill` overrides it; `--partial` turns it off for one run.

`--collection` cannot be combined with `--repo`, `--for-location` or
`--media`: the collection sets them. Stage stops, before it does
anything, when:

- the config has no such collection (the message lists the collections
  it has);
- the location is not registered;
- a repository is not registered, or is retired. Plain `--repo` only
  warns about a name it cannot find; a collection does not, so a
  repository is never left off the collection's discs without a word.

Then burn the session for the same location:

```bash
lcsas --config lcsas.toml burn --session latest --location Bank_Box
```

**Source refs:** `cmd_stage`, `_stage_collection_and_fill`,
`_collection_catalog_check` (`src/lcsas/cli/commands/stage.py`).

---

## Minimum fill: `--min-fill` and `--partial`

```bash
lcsas --config lcsas.toml stage --min-fill 90
lcsas --config lcsas.toml stage --min-fill 90 --partial
```

These work with or without a collection.

Stage packs the packs into volumes, one after another. Every volume but
the last is as full as the packs allow. The last one holds what is left
over, and can be nearly empty. With `--min-fill PCT`, the last volume is
not staged when its packs fill less than PCT percent of its **pack
space**. The volumes before it are staged. Its packs wait for a later
stage, when more data has come in.

The pack space is what one volume can hold: the medium's usable bytes,
after its 15 % error-correction share, less `metadata_reserve_mb`. A
volume packed as full as it can be is at 100 %. (`--dry-run` shows both
numbers per volume: the share of the disc's capacity, and the share of
its pack space.)

Stage says what it held back and why, for example:

```text
Not staging the last volume: it would hold 3 pack(s), 2,100,000,000 bytes: 9.9% of a BD25 volume's pack space (21,166,659,993 bytes), below the minimum fill of 90%. Its packs wait for a later stage; pass --partial to stage it now.
```

When the held-back volume is the only one, nothing is staged: no burn
session, no volume. Stage prints `Nothing staged: ...` and exits 0, since
waiting for more data is the normal case for a scheduled run.

`--partial` stages the last volume whatever its fill. Use it for the
final disc before a drill, or when a backup must reach a location now.

**Source refs:** `BurnOrchestrator.stage`, `_trim_underfilled`,
`_pack_space_bytes`, `UnderfilledVolume`
(`src/lcsas/burn/orchestrator.py`).

---

## `status --json`

```bash
lcsas --config lcsas.toml status --json
```

The status summary as one JSON document on stdout. Messages and
warnings go to stderr, so a script can read stdout as it is. The text
output of plain `lcsas status` is unchanged.

```json
{
  "format": 1,
  "locations": {
    "Bank_Box": {
      "discs": 4,
      "needs_reburn": [],
      "waiting": [
        {
          "data_bytes": 84200000000,
          "label": "LCSAS_MD100_2026_0007",
          "media_type": "MDISC100",
          "session_id": "2026-11-11T14:02:51.123456+00:00-3f9c2a1b"
        }
      ]
    },
    "Home_Shelf": {"discs": 6, "needs_reburn": [], "waiting": []}
  },
  "packs": {"archived": 120, "pruned": 0, "staged": 4, "total": 130, "unarchived": 6},
  "volumes": [
    {"discs_at": [], "label": "LCSAS_MD100_2026_0007", "media_type": "MDISC100", "staged_for": "Bank_Box", "status": "STAGING"}
  ],
  "waiting": ["LCSAS_MD100_2026_0007"],
  "incomplete": {"labels": [], "sessions": []},
  "ghosts": [],
  "burned_unrecorded": [],
  "burn_unknown": [],
  "partial_sessions": [],
  "needs_reburn": [],
  "single_disc_packs": 0,
  "stale_discs": {"count": 0, "older_than_days": 365}
}
```

(Shortened; a real document lists every volume.)

- `locations` has an entry for every registered location: its number of
  discs in service (`discs`), the volumes staged for it and waiting to
  be burned (`waiting`), and the volumes that need a re-burn there.
- `waiting` lists the same volumes as the text "waiting to be burned"
  warning: never burned, staging finished, ISO still in the staging
  area, and no failed verify.
- `incomplete`, `ghosts`, `partial_sessions` and `needs_reburn` match
  the other warnings of the text output. `ghosts` lists only volumes
  that were never burned and whose ISO is gone: the ones `lcsas catalog
  reconcile --fix` deletes.
- `burned_unrecorded` lists volumes that were burned but have no disc
  recorded: `BURNED` or `VERIFIED` with no disc (a catalog rebuilt from
  discs records the newest discs like this), or `STAGING` with an event
  that records a good disc (#751). Record each disc with `lcsas verify
  <label> --disc --location <where it is kept>` or by importing its burn
  receipt.
- `burn_unknown` lists volumes a catalog rebuild brought in with no
  record of whether they were burned (#758). Record each disc you hold
  the same way; `lcsas session abort --volume <label> --never-burned`
  deletes one that was never burned.
- `format` is 1. It changes only when a field changes meaning or is
  removed. New fields can appear without a change.

`--json` gives the summary only; it cannot be combined with
`--stale-copies` or `--redundancy`.

**Source refs:** `cmd_status`, `_status_json`, `STATUS_JSON_FORMAT`
(`src/lcsas/cli/commands/status.py`).

---

## A routine

After each backup and `lcsas scan`, for each collection:

```bash
lcsas --config lcsas.toml stage --collection bank
lcsas --config lcsas.toml status --json
```

If the document shows a volume waiting at `Bank_Box`, burn its session
with `lcsas burn --session <id> --location Bank_Box`, or burn the ISOs
elsewhere with `lcsas burn-iso --emit-receipt` and import the receipts
([burn-iso-portable.md](burn-iso-portable.md)). Until a volume is burned
and its disc recorded, the next stage for that location leaves its
packs out.

Exit codes of `stage --collection`, for a script:

- 0: volumes were staged, or the only volume was below the minimum fill
  and nothing was staged (`Nothing staged: ...`);
- 1: an error, or `No packs need staging.`: every pack of the
  collection's repositories is already on a disc at the location, or
  on a volume waiting to be burned for it.

---

## Test coverage

- `[covered]` Config loading, value types, unknown keys and the
  collection checks: `tests/unit/test_collections.py::TestConfig`.
- `[covered]` `config check` against the catalog (unknown location,
  unregistered or retired repository, missing password file, no
  catalog): `::TestConfigCheckCatalog`.
- `[covered]` `stage --collection` (repositories, location, media type,
  refused flags, unknown collection/location/repository):
  `::TestStageCollection`.
- `[covered]` Minimum fill, `--partial`, nothing staged, dry run, a full
  volume, fill against pack space: `::TestMinFill`.
- `[covered]` `status --json` shape, stdout only JSON, text unchanged:
  `::TestStatusJson`.
- `[gap]` A real burn of a collection's session with production media.

## Source refs

- `src/lcsas/config/settings.py`: `CollectionConfig`,
  `_load_collections`, `collection_password_errors`, `unknown_toml_keys`,
  `_type_errors`.
- `src/lcsas/burn/orchestrator.py`: `BurnOrchestrator.stage`,
  `_trim_underfilled`, `_pack_space_bytes`, `UnderfilledVolume`.
- `src/lcsas/cli/commands/stage.py`: `cmd_stage`, `_stage_collection_and_fill`,
  `_collection_catalog_check`; `src/lcsas/cli/commands/config.py`:
  `cmd_config_check`, `_config_check_collections`;
  `src/lcsas/cli/commands/status.py`: `cmd_status`, `_status_json`;
  `src/lcsas/cli/commands/repo.py`: `cmd_repo_list`.
