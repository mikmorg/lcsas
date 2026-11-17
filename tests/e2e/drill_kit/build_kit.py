#!/usr/bin/env python3
"""Build the v1.0 release-drill archive (#476, #474; owner decisions 2026-11-02).

One test archive with the default label prefix and two repositories:

- ``photos``    — its password is SPLIT 2-of-3 into share cards, set in its
                  own ``[repos.photos]`` table (#587);
- ``documents`` — an ordinary password, with a password card.

The corpus (corpus.py) is fixed and non-personal: generated from ``--seed``,
so every build has the same files.  It holds the names, links, modes,
folders and file sizes that restores get wrong (#834); ``--profile windows``
(the default) keeps to names NTFS can store, ``--profile posix`` adds the
rest.  Each set is backed up twice, and each backup goes onto discs in a
burn session of its own, so a restore must take "latest" from the newest
disc.  The meta disc is built from the drill config and catalog (owner
decision Q19) and labelled ``LCSAS_META`` (#588).

It drives the real CLI (``python3 -m lcsas`` from ``--project-root``) the
way an owner would: rustic init/backup, ``init``, ``repo add``, ``key
split``, ``key card``, ``scan``, ``stage --media CD700``; then a second
rustic backup, ``scan`` and ``stage``; ``estate card``, ``meta build`` +
``meta verify --strict``, and xorriso for the meta ISO.  manifest.json is
written from the corpus tree as it was backed up the second time.

Everything lands in ``--workdir``, which must be OUTSIDE this checkout: it
holds passwords and share cards, and the repo is public (HANDOFF rule 1).
Build from a clean worktree of the rc being drilled and run it under the
lab's build lock; see README.md.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

import corpus  # noqa: E402  (tests/e2e/drill_kit/corpus.py)

SPLIT_K, SPLIT_N = 2, 3


def log(msg: str) -> None:
    print(f"[drill-kit] {msg}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join("<script>" if "\n" in a else a for a in cmd))
    return subprocess.run(cmd, check=True, **kw)


# `lcsas burn` without a disc writer: burn_session(skip_burn=True) records
# each volume of the session VERIFIED with a disc at the default location,
# as the test fixtures do (tests/e2e/fixture_lib.py burn_to_isos).  A
# snapshot goes on a disc only when its packs are on burned discs or on that
# session's own discs, so the first session must be "burned" before the
# second is staged, or the second session's discs would carry no snapshot.
_MARK_BURNED = """
import argparse, sys
from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.cli.common import _load_config_opt, _open_catalog
from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
from lcsas.iso.xorriso import SubprocessXorrisoRunner
args = argparse.Namespace(config=sys.argv[1], db=None)
config = _load_config_opt(args)
with _open_catalog(args, config) as conn:
    orch = BurnOrchestrator(
        config, conn,
        SubprocessXorrisoRunner(tmpdir=config.staging_path),
        SubprocessDVDisasterRunner(tmpdir=config.staging_path),
    )
    receipts = orch.burn_session(
        "latest", location=config.default_location, skip_burn=True,
        interactive=False,
    )
print("recorded as burned (no disc written):",
      ", ".join(r.volume_label for r in receipts))
"""


def use_small_corpus() -> None:
    """--small: a few small bulk files instead of ~670 MiB of them, for a
    quick local check of the kit.  The big file (over 100 MB) stays."""
    corpus.CORPUS = tuple((repo, n_text, 2, corpus.MiB)
                          for repo, n_text, _n, _size in corpus.CORPUS)
    corpus.ADDED = tuple((repo, 1, corpus.MiB) for repo, _n, _s in corpus.ADDED)


def write_config(work: Path) -> Path:
    cfg = work / "drill.toml"
    repos = []
    for repo, *_ in corpus.CORPUS:
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
    ap.add_argument("--profile", choices=corpus.PROFILES, default="windows",
                    help="windows (default): names NTFS can store, for the "
                         "Windows drills; posix: also the names, links and "
                         "long path only Linux/macOS can hold.")
    ap.add_argument("--small", action="store_true",
                    help="Few small bulk files (the big file stays): a quick "
                         "local check of the kit, not a drill kit.")
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

    def rustic_env(repo: str) -> dict[str, str]:
        return {**os.environ, "RUSTIC_REPOSITORY": str(work / "mirror" / repo),
                "RUSTIC_PASSWORD_FILE": str(work / "secrets" / f"{repo}.pw")}

    def backup(src: Path) -> None:
        for repo, *_ in corpus.CORPUS:
            # --as-path/--host: the snapshot says /photos on host "drill",
            # not the lab's path and hostname.
            run(["rustic", "backup", str(src / repo),
                 "--as-path", f"/{repo}", "--host", "drill"],
                env=rustic_env(repo))

    def stage() -> list[Path]:
        """One burn session: `lcsas stage`, then record its volumes as
        burned (no disc is written).  Returns its ISOs."""
        before = set((work / "staging").rglob("*.iso"))
        lcsas("stage", "--media", args.media)
        new = sorted(set((work / "staging").rglob("*.iso")) - before,
                     key=lambda p: p.name)
        if not new:
            sys.exit("stage produced no ISO")
        run([sys.executable, "-c", _MARK_BURNED, str(cfg)], env=env, cwd=work)
        return new

    log(f"workdir {work}; code from {root}")
    for sub in ("corpus", "mirror", "staging", "secrets", "cards", "iso"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(work / "secrets", 0o700)
    os.chmod(work / "cards", 0o700)

    log(f"1. corpus (profile {args.profile})")
    if args.small:
        use_small_corpus()
    src = work / "corpus"
    corpus.build_corpus(src, args.seed, args.profile)
    first = corpus.scan_sets(src)
    log(f"   {sum(len(v) for v in first.values())} items")

    log("2. passwords, rustic repositories, first backup")
    for repo, *_ in corpus.CORPUS:
        pw = work / "secrets" / f"{repo}.pw"
        pw.write_text(secrets.token_urlsafe(32) + "\n")
        os.chmod(pw, 0o600)
        run(["rustic", "init"], env=rustic_env(repo))
    backup(src)

    log("3. catalog, repositories, split, cards")
    cfg = write_config(work)
    lcsas("init")
    for repo, *_ in corpus.CORPUS:
        lcsas("repo", "add", repo, str(work / "mirror" / repo))
    lcsas("key", "split", "--repo", "photos",
          "--out", str(work / "cards" / "photos-shares"))
    lcsas("key", "card", "--repo", "documents",
          "--out", str(work / "cards" / "documents-password-card.txt"))
    lcsas("scan")

    log(f"4. first burn session: stage the data discs ({args.media})")
    first_isos = stage()

    log("5. change the corpus, second backup, second burn session")
    corpus.change_corpus(src, args.seed)
    backup(src)
    lcsas("scan")
    second_isos = stage()
    if any(i.name in {f.name for f in first_isos} for i in second_isos):
        sys.exit("the second session reused a disc label of the first")
    isos = first_isos + second_isos
    for iso in isos:
        shutil.copy2(iso, work / "iso" / iso.name)
    log(f"   {len(first_isos)} + {len(second_isos)} data disc(s): "
        f"{', '.join(i.name for i in isos)}")

    log("6. manifest (from the corpus as the second backup saw it)")
    manifest = corpus.make_manifest(src, first, profile=args.profile,
                                    seed=args.seed)
    corpus.write_manifest(work / "manifest.json", manifest)

    log("7. estate card")
    lcsas("estate", "card", "--output", str(work / "cards" / "estate-card.txt"))

    log("8. meta disc (from the drill config and catalog)")
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
    print(f"    (it counts the {len(isos)} data discs; the meta disc comes with them)")
    for card in shares[:SPLIT_K]:
        print(f"  {card}")
    print(f"  {work / 'cards' / 'documents-password-card.txt'}")
    print(f"    (write the password from {work / 'secrets' / 'documents.pw'} "
          "into its box)")
    print("Check a restore with:")
    print(f"  python3 {HERE / 'verify_restore.py'} {work / 'manifest.json'} "
          "<restored dir>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
