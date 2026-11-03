#!/usr/bin/env python3
"""Build the v1.0 release-drill archive (#476, #474; owner decisions 2026-11-02).

One test archive with the default label prefix and two repositories:

- ``photos``    — its password is SPLIT 2-of-3 into share cards, set in its
                  own ``[repos.photos]`` table (#587);
- ``documents`` — an ordinary password, with a password card.

The corpus is fixed and non-personal: generated from ``--seed``, so every
build has the same files and the same sha256 manifest.  It is sized for two
CD700 data discs.  The meta disc is built from the drill config and catalog
(owner decision Q19) and labelled ``LCSAS_META`` (#588).

It drives the real CLI (``python3 -m lcsas`` from ``--project-root``) the
way an owner would: rustic init/backup, ``init``, ``repo add``, ``key
split``, ``key card``, ``scan``, ``stage --media CD700``, ``estate card``,
``meta build`` + ``meta verify --strict``, and xorriso for the meta ISO.

Everything lands in ``--workdir``, which must be OUTSIDE this checkout: it
holds passwords and share cards, and the repo is public (HANDOFF rule 1).
Build from a clean worktree of the rc being drilled and run it under the
lab's build lock; see README.md.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]

SPLIT_K, SPLIT_N = 2, 3

# (repository, text files, bulk files, bulk file size).  ~630 MiB in all:
# CD700 holds ~522 MB of packs per disc after the default 100 MB metadata
# reserve, so this is two data discs.
CORPUS = (
    ("photos", 4, 64, 6 * 1024 * 1024),
    ("documents", 6, 40, 6 * 1024 * 1024),
)

_WORDS = (
    "archive", "disc", "drill", "restore", "sample", "record", "family",
    "garden", "harbour", "lantern", "meadow", "orchard", "pebble", "river",
    "season", "summer", "timber", "valley", "window", "winter", "letter",
    "notebook", "journal", "recipe", "ledger", "map", "album", "page",
    "chapter", "note",
)


def log(msg: str) -> None:
    print(f"[drill-kit] {msg}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def _text(rng: random.Random, title: str) -> bytes:
    lines = [
        title,
        "",
        "This file is TEST DATA for an LCSAS restore drill.",
        "It contains no personal information.",
        "",
    ]
    for _ in range(40):
        lines.append(" ".join(rng.choice(_WORDS) for _ in range(12)).capitalize() + ".")
    return ("\n".join(lines) + "\n").encode()


def build_corpus(root: Path, seed: str) -> dict[str, str]:
    """Write the fixed corpus under *root*; return {"<repo>/<file>": sha256}."""
    manifest: dict[str, str] = {}
    for repo, n_text, n_bulk, bulk_size in CORPUS:
        d = root / repo
        d.mkdir(parents=True)
        for i in range(1, n_text + 1):
            name = f"{'album-notes' if repo == 'photos' else 'letter'}-{i:02d}.txt"
            data = _text(random.Random(f"{seed}:{repo}:text:{i}"), f"{repo} {name}")
            (d / name).write_bytes(data)
            manifest[f"{repo}/{name}"] = hashlib.sha256(data).hexdigest()
        for i in range(1, n_bulk + 1):
            name = f"{'scan' if repo == 'photos' else 'document'}-{i:03d}.dat"
            # Incompressible, so the packs really fill two discs.
            data = random.Random(f"{seed}:{repo}:bulk:{i}").randbytes(bulk_size)
            (d / name).write_bytes(data)
            manifest[f"{repo}/{name}"] = hashlib.sha256(data).hexdigest()
    return manifest


def write_manifest(path: Path, manifest: dict[str, str]) -> None:
    """sha256sum -c format, paths relative to the restore target."""
    path.write_text(
        "".join(f"{h}  {p}\n" for p, h in sorted(manifest.items()))
    )


def write_config(work: Path) -> Path:
    cfg = work / "drill.toml"
    repos = []
    for repo, *_ in CORPUS:
        repos += [
            f"[repos.{repo}]",
            f'mirror_path = "{work / "mirror" / repo}"',
            f'password_file = "{work / "secrets" / f"{repo}.pw"}"',
        ]
        if repo == "photos":
            repos += [
                "key_split = true",
                f"key_threshold = {SPLIT_K}",
                f"key_shares = {SPLIT_N}",
            ]
        repos.append("")
    cfg.write_text("\n".join([
        "# LCSAS release-drill archive: TEST DATA ONLY (tests/e2e/drill_kit).",
        "[paths]",
        f'mirror_base = "{work / "mirror"}"',
        f'staging = "{work / "staging"}"',
        f'database = "{work / "catalog.db"}"',
        "",
        "[defaults]",
        'media_type = "CD700"',
        'label_prefix = "LCSAS"',
        'location = "Drill_Shelf"',
        "",
        "[survivability]",
        'archive_owner = "the drill organiser (this is a TEST archive)"',
        'archive_description = "Test files for an LCSAS restore drill: '
        'no personal data"',
        'key_storage_hints = "For this drill, the cards you need were handed '
        'to you with the discs."',
        'technical_contact = "The person running this drill"',
        "",
        *repos,
    ]))
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workdir", type=Path, required=True,
                    help="Empty directory OUTSIDE this checkout.")
    ap.add_argument("--project-root", type=Path, default=REPO_ROOT,
                    help="Checkout whose code builds the discs "
                         "(default: the one holding this script).")
    ap.add_argument("--seed", default="lcsas-drill-v1",
                    help="Corpus seed (default: %(default)s).")
    ap.add_argument("--media", default="CD700")
    args = ap.parse_args()

    work = args.workdir.resolve()
    root = args.project_root.resolve()
    if work == root or work.is_relative_to(root) or work.is_relative_to(REPO_ROOT):
        sys.exit("refusing: --workdir must be outside the checkout "
                 "(it will hold passwords and share cards; the repo is public)")
    if work.exists() and any(work.iterdir()):
        sys.exit(f"refusing: {work} is not empty")
    for tool in ("rustic", "xorriso", "dvdisaster"):
        if shutil.which(tool) is None:
            sys.exit(f"missing tool: {tool}")

    env = {**os.environ, "PYTHONPATH": str(root / "src")}

    def lcsas(*a: str) -> None:
        run([sys.executable, "-m", "lcsas", "--config", str(cfg), *a],
            env=env, cwd=work)

    log(f"workdir {work}; code from {root}")
    for sub in ("corpus", "mirror", "staging", "secrets", "cards", "iso"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(work / "secrets", 0o700)
    os.chmod(work / "cards", 0o700)

    log("1. corpus")
    manifest = build_corpus(work / "corpus", args.seed)
    write_manifest(work / "manifest.sha256", manifest)
    log(f"   {len(manifest)} files")

    log("2. passwords and rustic repositories")
    for repo, *_ in CORPUS:
        pw = work / "secrets" / f"{repo}.pw"
        pw.write_text(secrets.token_urlsafe(32) + "\n")
        os.chmod(pw, 0o600)
        renv = {**os.environ, "RUSTIC_REPOSITORY": str(work / "mirror" / repo),
                "RUSTIC_PASSWORD_FILE": str(pw)}
        run(["rustic", "init"], env=renv)
        # --as-path/--host: the snapshot says /photos on host "drill", not
        # the lab's path and hostname.
        run(["rustic", "backup", str(work / "corpus" / repo),
             "--as-path", f"/{repo}", "--host", "drill"], env=renv)

    log("3. catalog, repositories, split, cards")
    cfg = write_config(work)
    lcsas("init")
    for repo, *_ in CORPUS:
        lcsas("repo", "add", repo, str(work / "mirror" / repo))
    lcsas("key", "split", "--repo", "photos",
          "--out", str(work / "cards" / "photos-shares"))
    lcsas("key", "card", "--repo", "documents",
          "--out", str(work / "cards" / "documents-password-card.txt"))
    lcsas("scan")

    log(f"4. stage the data discs ({args.media})")
    lcsas("stage", "--media", args.media)
    isos = sorted((work / "staging").rglob("*.iso"))
    if not isos:
        sys.exit("stage produced no ISO")
    for iso in isos:
        shutil.copy2(iso, work / "iso" / iso.name)
    log(f"   {len(isos)} data disc(s): {', '.join(i.name for i in isos)}")

    log("5. estate card")
    lcsas("estate", "card", "--output", str(work / "cards" / "estate-card.txt"))

    log("6. meta disc (from the drill config and catalog)")
    meta = work / "meta-tree"
    lcsas("meta", "build", "--project-root", str(root), "--output", str(meta))
    lcsas("meta", "verify", "--strict", str(meta))
    run(["xorriso", "-as", "mkisofs", "-r", "-J", "-joliet-long",
         "-iso-level", "3", "-V", "LCSAS_META",
         "-o", str(work / "iso" / "LCSAS_META.iso"), str(meta)])

    shares = sorted((work / "cards" / "photos-shares").glob("*-card.txt"))
    log("done.")
    print(f"\nDiscs (insert as virtual CD media): {work / 'iso'}")
    for iso in sorted((work / "iso").glob("*.iso")):
        print(f"  {iso.name}")
    print("Hand the stand-in (#474) these printouts, and nothing else:")
    print(f"  {work / 'cards' / 'estate-card.txt'}")
    print(f"    (write the disc count by hand: {len(isos)} data discs + 1 meta disc)")
    for card in shares[:SPLIT_K]:
        print(f"  {card}")
    print(f"  {work / 'cards' / 'documents-password-card.txt'}")
    print(f"    (write the password from {work / 'secrets' / 'documents.pw'} "
          "into its box)")
    print("Check a restore with:")
    print(f"  python3 {HERE / 'verify_restore.py'} {work / 'manifest.sha256'} "
          "<restored dir>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
