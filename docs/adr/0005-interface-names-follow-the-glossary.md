# ADR 0005 — Interface names follow the glossary

- Status: Accepted
- Date: 2026-11-20
- Issues: #934 (renames), #933 (heir-facing text and its check), #926
  (the restore-folder question); owner grilling session of 2026-11-20

## Decision

Every name a person types or reads follows `CONTEXT.md`. That covers the
restore scripts' flags and environment variables, the recovery tiers'
flags, the owner's `lcsas` commands and config keys, the files on the
discs and in the tools folder, and the card text. Before v1.0.0 the old
names were renamed outright, with no aliases. An old name fails with a
one-line message naming the new one and exits 2 (usage). It is never
accepted silently.

A new flag, variable, command or file name is named from the glossary.
When the glossary has no term for the concept, the term is settled in
`CONTEXT.md` first.

## Why

Nothing was in production, and the only discs that existed were drill
discs, rebuilt for every release candidate. The archive's readers are
heirs decades from now, who follow the printed cards and the RESTORE
manuals literally. Each interface name that disagreed with those words
was a place where the instructions and the program could drift apart:
`--key` meaning the password, `--target` and `LCSAS_TARGET` meaning two
different things, `RECOVER.txt` naming an act the glossary calls
restore. Breaking them once, before the first real archive is burned,
costs one release candidate. Keeping them would have cost the archive's
lifetime.

## Considered options

- **Aliases for the old names.** Rejected. Two names for one thing is
  what the glossary exists to prevent, and there was nothing to stay
  compatible with.
- **A plain "unknown flag" for old names.** Rejected in favour of a
  message naming the new flag. Someone reading an old note years later
  deserves to be told what the flag became.
- **Renaming the catalog schema and code identifiers too.** Rejected.
  No heir or owner reads them, the glossary lets code say "repository",
  and a schema migration before final adds risk with no reader gaining
  anything. `key_escrow` stays a table name, documented as such in
  FORMAT.txt.

## Consequences

- Discs burned before the rename (drill discs up to rc10) are
  unsupported by the renamed readers, with no fallback.
- An owner config with an old key (for example `key_split`) is refused,
  naming the new key, because a silently ignored split setting could
  leave a password unsplit without the owner knowing.
- The #933 check fails on any retired name in heir-facing text, so old
  names cannot creep back into the manuals.
