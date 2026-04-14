#!/usr/bin/env python3
"""LCSAS single-drive disc-swap restore helper.

Bundled on the meta volume by ``lcsas meta build``. Drives the
three-phase restore loop that lets a single optical drive restore
a repository by physically swapping discs between invocations.

Pure stdlib. Does NOT import the ``lcsas`` package, so it still runs
if the bundled source tree is incomplete or this script is copied off
the meta volume onto an unrelated system.

Phases:
    bootstrap
        Read the catalog on a currently-mounted disc, build a pick
        list of every live pack belonging to the target repository
        (grouped by its primary volume), seed the restore cache with
        repo metadata from this disc, and write ``pick-list.json``
        into the cache. Emits the same JSON on stdout for the caller.

    ingest
        Copy every pack the pick list assigns to ``--disc-label``
        from the currently-mounted disc into the cache, verifying
        SHA-256 on each copy.

    finalize
        Walk the pick list and confirm every expected pack is now
        present in the cache. Exit 1 if anything is missing.

This helper does not run ``rustic``. The caller is responsible for
invoking ``rustic restore`` against the assembled cache once
``finalize`` returns 0.

Pack selection is conservative: every non-pruned pack belonging to
the repo is included. This may ingest more packs than a specific
snapshot strictly needs, but it guarantees correctness without
depending on a rustic binary during the planning phase.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

METADATA_SUBDIRS = ("index", "snapshots", "keys")
_LIVE_VOLUME_STATUSES = ("STAGING", "BURNING", "BURNED", "VERIFIED")


def pack_dest_path(data_dir: Path, sha256: str) -> Path:
    if len(sha256) >= 2:
        return data_dir / sha256[:2] / sha256
    return data_dir / sha256


def find_pack_file(data_dir: Path, sha256: str) -> Path | None:
    if len(sha256) >= 2:
        p = data_dir / sha256[:2] / sha256
        if p.is_file():
            return p
    p = data_dir / sha256
    return p if p.is_file() else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_state(cache: Path) -> dict[str, object]:
    path = cache / "restore-state.json"
    if path.is_file():
        return json.loads(path.read_text())
    return {}


def _write_state(cache: Path, updates: dict[str, object]) -> None:
    state = _read_state(cache)
    state.update(updates)
    state["last_updated"] = datetime.now(UTC).isoformat()
    if "started_at" not in state:
        state["started_at"] = state["last_updated"]
    (cache / "restore-state.json").write_text(json.dumps(state, indent=2))


def _list_repos(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return list(conn.execute("SELECT repo_id, name FROM repositories ORDER BY name"))


def _resolve_repo(conn: sqlite3.Connection, repo_name: str) -> str:
    row = conn.execute(
        "SELECT repo_id FROM repositories WHERE name = ?", (repo_name,),
    ).fetchone()
    if row is None:
        avail = ", ".join(name for _rid, name in _list_repos(conn)) or "(none)"
        raise SystemExit(
            f"ERROR: repository '{repo_name}' not found in catalog. "
            f"Available: {avail}"
        )
    return row[0]


def _build_pick_list(conn: sqlite3.Connection, repo_id: str) -> dict:
    """Group every live pack for *repo_id* under its primary volume.

    A pack may live on multiple discs. We assign it to the
    lexicographically-smallest volume label and record the rest as
    alternates. The caller walks volumes in sorted order; the agent
    only has to visit each primary disc once.
    """
    placeholders = ",".join("?" for _ in _LIVE_VOLUME_STATUSES)
    rows = conn.execute(
        f"""
        SELECT p.sha256, p.size_bytes, v.label
        FROM packs p
        JOIN volume_packs vp ON p.pack_id = vp.pack_id
        JOIN volumes v       ON vp.volume_id = v.volume_id
        WHERE p.repo_id = ?
          AND p.is_pruned = 0
          AND v.status IN ({placeholders})
        ORDER BY p.sha256, v.label
        """,
        (repo_id, *_LIVE_VOLUME_STATUSES),
    ).fetchall()

    pack_labels: dict[str, list[str]] = {}
    pack_sizes: dict[str, int] = {}
    for sha256, size, label in rows:
        pack_labels.setdefault(sha256, []).append(label)
        pack_sizes[sha256] = size

    volumes: dict[str, dict] = {}
    for sha256, labels in pack_labels.items():
        primary = labels[0]
        entry = volumes.setdefault(
            primary, {"label": primary, "packs": [], "bytes": 0},
        )
        entry["packs"].append(sha256)
        entry["bytes"] += pack_sizes[sha256]

    ordered = []
    for label in sorted(volumes):
        v = volumes[label]
        v["packs"] = sorted(v["packs"])
        ordered.append(v)

    # Per-pack alternates (for resilience if a primary disc is corrupt).
    alternates: dict[str, list[str]] = {
        sha256: labels[1:] for sha256, labels in pack_labels.items() if len(labels) > 1
    }

    return {
        "volumes": ordered,
        "alternates": alternates,
        "total_packs": len(pack_labels),
        "total_bytes": sum(pack_sizes.values()),
    }


def _seed_metadata(mount: Path, cache: Path, repo_id: str) -> None:
    src = mount / "metadata" / repo_id
    if not src.is_dir():
        # The mount path may itself be the metadata root (when bootstrap
        # is sourced from the meta disc, which carries metadata/<repo_id>
        # at the root next to catalog.db).
        alt = mount / repo_id
        if alt.is_dir():
            src = alt
    if not src.is_dir():
        raise SystemExit(
            f"ERROR: metadata for repo {repo_id} not found on this disc "
            f"(looked for {src}). Every LCSAS data disc carries a metadata "
            f"copy — either this is not an LCSAS disc, or this repository "
            f"has no data on it yet."
        )
    (cache / "data").mkdir(parents=True, exist_ok=True)
    for subdir in METADATA_SUBDIRS:
        s = src / subdir
        d = cache / subdir
        if s.is_dir() and not d.exists():
            shutil.copytree(s, d)
    if (src / "config").is_file() and not (cache / "config").exists():
        shutil.copy2(src / "config", cache / "config")


def _catalog_freshness(conn: sqlite3.Connection) -> str:
    """Return MAX(created_at) from volumes as an ISO 8601 timestamp.

    This is monotonically increasing — a catalog from a later burn
    session always has a later max created_at. Used to detect when
    a data disc carries a fresher catalog than the one we bootstrapped
    from.
    """
    row = conn.execute("SELECT MAX(created_at) FROM volumes").fetchone()
    return (row[0] or "") if row else ""


def phase_bootstrap(args: argparse.Namespace) -> int:
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    reseed = getattr(args, "reseed", False)
    conn = sqlite3.connect(
        f"file:{args.catalog}?mode=ro&immutable=1", uri=True
    )
    try:
        if not args.repo:
            repos = _list_repos(conn)
            if not repos:
                print("ERROR: no repositories in catalog", file=sys.stderr)
                return 1
            print("Repositories on this archive:", file=sys.stderr)
            for _rid, name in repos:
                print(f"  {name}", file=sys.stderr)
            print("Re-run with --repo NAME", file=sys.stderr)
            return 2

        if reseed:
            # Validate the new catalog before destroying anything.
            try:
                _resolve_repo(conn, args.repo)
            except SystemExit:
                print(
                    "ERROR: repo not found in new catalog — keeping "
                    "existing metadata",
                    file=sys.stderr,
                )
                return 1
            # Clear stale metadata so _seed_metadata copies fresh versions.
            for sub in (*METADATA_SUBDIRS, "config"):
                p = cache / sub
                if p.exists():
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()

        repo_id = _resolve_repo(conn, args.repo)
        pick_list = _build_pick_list(conn, repo_id)
        pick_list["repo"] = args.repo
        pick_list["repo_id"] = repo_id
        pick_list["catalog_freshness"] = _catalog_freshness(conn)

        _seed_metadata(Path(args.mount), cache, repo_id)

        (cache / "pick-list.json").write_text(json.dumps(pick_list, indent=2))
        json.dump(pick_list, sys.stdout, indent=2)
        sys.stdout.write("\n")
    finally:
        conn.close()
    return 0


def _load_pick_list(cache: Path) -> dict:
    path = cache / "pick-list.json"
    if not path.is_file():
        print(
            "ERROR: pick-list.json not found in cache — run bootstrap first",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return json.loads(path.read_text())


def verify_disc(mount: Path, expected_label: str) -> bool:
    """Quick pre-check: parse volume_info.json and verify the label matches.

    Returns True if verification passes or no volume_info.json exists.
    Prints a warning and returns False on mismatch.
    """
    info_path = mount / "volume_info.json"
    if not info_path.is_file():
        return True
    try:
        info = json.loads(info_path.read_text())
        actual = info.get("label", "")
        if actual and actual != expected_label:
            print(
                f"  WARNING: disc label mismatch — volume_info.json says "
                f"'{actual}', expected '{expected_label}'",
                file=sys.stderr,
            )
            return False
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"  WARNING: could not read volume_info.json: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def phase_ingest(args: argparse.Namespace) -> int:
    cache = Path(args.cache)
    mount = Path(args.mount)
    disc_label = args.disc_label

    if getattr(args, "verify_disc", False) and not verify_disc(mount, disc_label):
        print(
            f"  {disc_label}: disc verification failed — skipping ingest",
            file=sys.stderr,
        )
        return 2

    pick_list = _load_pick_list(cache)

    wanted_primary: list[str] = []
    for entry in pick_list["volumes"]:
        if entry["label"] == disc_label:
            wanted_primary = entry["packs"]
            break

    alternates: dict = pick_list.get("alternates", {})
    wanted_alt: list[str] = [
        sha for sha, alt_labels in alternates.items() if disc_label in alt_labels
    ]

    wanted = sorted(set(wanted_primary) | set(wanted_alt))
    if not wanted:
        print(
            f"  {disc_label}: not in pick list — nothing to ingest",
            file=sys.stderr,
        )
        return 0

    data_src = mount / "data"
    data_dst = cache / "data"
    data_dst.mkdir(parents=True, exist_ok=True)

    ingested = 0
    corrupt: list[str] = []
    for sha256 in wanted:
        dst = pack_dest_path(data_dst, sha256)
        if dst.is_file():
            continue
        src = find_pack_file(data_src, sha256)
        if src is None:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        actual = sha256_file(dst)
        if actual != sha256:
            dst.unlink()
            corrupt.append(sha256)
            continue
        ingested += 1

    print(f"  {disc_label}: ingested {ingested} packs", file=sys.stderr)

    # Update persistent state so interrupted restores have context.
    state = _read_state(cache)
    completed = list(state.get("volumes_completed", []))
    if disc_label not in completed:
        completed.append(disc_label)
    corrupt_map: dict[str, str] = dict(state.get("corrupt_packs", {}))
    for sha in corrupt:
        corrupt_map[sha] = disc_label

    # Count total packs in cache.
    data_dst = cache / "data"
    cached_count = sum(1 for d in data_dst.iterdir() if d.is_dir()
                       for _ in d.iterdir()) if data_dst.is_dir() else 0

    _write_state(cache, {
        "phase": "ingest",
        "repo": pick_list.get("repo", ""),
        "volumes_completed": completed,
        "packs_ingested": cached_count,
        "packs_total": pick_list.get("total_packs", 0),
        "corrupt_packs": corrupt_map,
    })

    if corrupt:
        print(
            f"  WARNING: {len(corrupt)} pack(s) failed SHA-256 verification "
            f"on {disc_label}. The finalize phase will identify which alternate "
            f"discs hold them.",
            file=sys.stderr,
        )
        return 2
    return 0


def phase_finalize(args: argparse.Namespace) -> int:
    cache = Path(args.cache)
    pick_list = _load_pick_list(cache)
    data_dir = cache / "data"
    verify = getattr(args, "verify_integrity", True)

    missing: list[str] = []
    corrupted: list[str] = []
    checked = 0

    for entry in pick_list["volumes"]:
        for sha256 in entry["packs"]:
            path = pack_dest_path(data_dir, sha256)
            if not path.is_file():
                missing.append(sha256)
                continue
            if verify:
                checked += 1
                actual = sha256_file(path)
                if actual != sha256:
                    corrupted.append(sha256)

    if verify and checked > 0:
        print(
            f"  integrity: verified {checked} pack(s)",
            file=sys.stderr,
        )

    if corrupted:
        print(
            f"  CORRUPTED: {len(corrupted)} pack(s) in cache failed "
            f"SHA-256 verification — removing them.",
            file=sys.stderr,
        )
        for sha256 in corrupted:
            path = pack_dest_path(data_dir, sha256)
            path.unlink(missing_ok=True)
        # Corrupted packs are now missing; add to the missing list.
        missing.extend(corrupted)

    if not missing:
        _write_state(cache, {
            "phase": "complete",
            "packs_ingested": pick_list["total_packs"],
            "packs_total": pick_list["total_packs"],
        })
        print(
            f"  cache complete: {pick_list['total_packs']} packs, "
            f"{pick_list['total_bytes']} bytes",
            file=sys.stderr,
        )
        return 0

    # Classify missing packs as recoverable or unrecoverable.
    alternates: dict[str, list[str]] = pick_list.get("alternates", {})
    state = _read_state(cache)
    corrupt_map: dict[str, str] = dict(state.get("corrupt_packs", {}))

    recoverable: dict[str, int] = {}  # label → count
    unrecoverable: list[str] = []

    for sha in missing:
        # Find all possible sources for this pack.
        sources: list[str] = []
        for entry in pick_list["volumes"]:
            if sha in entry["packs"]:
                sources.append(entry["label"])
                break
        sources.extend(alternates.get(sha, []))
        # Filter out sources we already know are corrupt for this pack.
        viable = [s for s in sources
                  if corrupt_map.get(sha) != s]
        if viable:
            recoverable[viable[0]] = recoverable.get(viable[0], 0) + 1
        else:
            unrecoverable.append(sha)

    print(
        f"ERROR: {len(missing)} pack(s) still missing from cache.",
        file=sys.stderr,
    )

    if recoverable:
        print("", file=sys.stderr)
        print(
            f"  RECOVERABLE: {sum(recoverable.values())} pack(s) available "
            f"on alternate discs:",
            file=sys.stderr,
        )
        for label, count in sorted(recoverable.items()):
            print(f"    need {count} pack(s) from {label}", file=sys.stderr)

    if unrecoverable:
        print("", file=sys.stderr)
        print(
            f"  UNRECOVERABLE: {len(unrecoverable)} pack(s) have NO remaining "
            f"alternates.",
            file=sys.stderr,
        )
        print(
            "  The restore cannot complete with the available media.",
            file=sys.stderr,
        )
        print(
            "  Contact your backup administrator.",
            file=sys.stderr,
        )

    _write_state(cache, {"phase": "finalize_incomplete"})

    # Exit code 3 = unrecoverable; 1 = recoverable (retry).
    return 3 if unrecoverable else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="restore_single_drive.py",
        description="LCSAS single-drive disc-swap restore helper",
    )
    sub = p.add_subparsers(dest="phase", required=True)

    bp = sub.add_parser(
        "bootstrap",
        help="Read catalog, emit pick list, seed cache metadata",
    )
    bp.add_argument("--catalog", required=True, help="Path to catalog.db on mounted disc")
    bp.add_argument("--mount", required=True, help="Mount point of currently-loaded disc")
    bp.add_argument("--cache", required=True, help="Restore cache directory to populate")
    bp.add_argument("--repo", default=None, help="Repository name to restore")
    bp.add_argument(
        "--reseed", action="store_true", default=False,
        help="Re-bootstrap: validate new catalog, clear stale metadata, re-seed",
    )

    ip = sub.add_parser(
        "ingest",
        help="Copy needed packs from currently-mounted disc into cache",
    )
    ip.add_argument("--mount", required=True, help="Mount point of currently-loaded disc")
    ip.add_argument("--cache", required=True, help="Restore cache directory")
    ip.add_argument("--disc-label", required=True, help="Label of the disc now in the drive")

    ip.add_argument(
        "--verify-disc", action="store_true", default=False,
        help="Verify volume_info.json on disc before ingesting",
    )

    fp = sub.add_parser(
        "finalize",
        help="Verify cache completeness against pick list",
    )
    fp.add_argument("--cache", required=True, help="Restore cache directory")
    fp.add_argument(
        "--verify-integrity", action="store_true", default=True,
        dest="verify_integrity",
        help="Re-verify SHA-256 of cached packs (default: on)",
    )
    fp.add_argument(
        "--no-verify-integrity", action="store_false",
        dest="verify_integrity",
        help="Skip SHA-256 re-verification (faster)",
    )

    args = p.parse_args(argv)
    if args.phase == "bootstrap":
        return phase_bootstrap(args)
    if args.phase == "ingest":
        return phase_ingest(args)
    if args.phase == "finalize":
        return phase_finalize(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
