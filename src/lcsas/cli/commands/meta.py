"""``lcsas meta``: build, verify."""

from __future__ import annotations

import argparse
from pathlib import Path

from lcsas.cli.common import _load_config_opt, _resolve_db_path
from lcsas.log import get_logger

logger = get_logger()


def cmd_meta_build(args: argparse.Namespace) -> int:
    """Build a self-contained meta disc with all the recovery tools."""
    from lcsas.meta.builder import MetaBuildError, MetaVolumeBuilder

    # Load config for survivability fields (START_HERE.txt, PASSWORD_INFO.txt).
    # A broken config downgrades those fields; it must not block the build,
    # so this is the one caller that swallows the error.
    config = None
    try:
        config = _load_config_opt(args)
    except Exception as e:
        logger.warning(f"Could not load config for START_HERE.txt: {e}")

    output = args.output.resolve()
    db_path: Path | None = None
    try:
        db_path = _resolve_db_path(args, config)
        if not db_path.is_file():
            db_path = None
    except Exception:
        db_path = None

    builder = MetaVolumeBuilder(
        output_dir=output,
        project_root=args.project_root,
        config=config,
        catalog_db_path=db_path,
        allow_no_zstd=getattr(args, "allow_no_zstd", False),
        allow_no_dvdisaster_source=getattr(
            args, "allow_no_dvdisaster_source", False
        ),
        allow_missing_metadata=getattr(
            args, "allow_missing_metadata", False
        ),
    )

    logger.info(f"Building meta disc in {output} ...")
    try:
        builder.build()
    except FileNotFoundError as e:
        logger.error(f"{e}")
        logger.error("Ensure rustic, xorriso, and python3 are installed.")
        return 1
    except MetaBuildError as e:
        logger.error(f"{e}")
        return 1

    # Issue #562: name at build time the files that `meta verify --strict`
    # would report as EXTRA, so a stray file shows here instead of only
    # when (and if) the owner runs a strict verify.  A warning, not a
    # failure: the disc can still restore.
    unlisted = builder.unlisted_recovery_files()
    if unlisted:
        shown = ", ".join(unlisted[:10])
        if len(unlisted) > 10:
            shown += f", and {len(unlisted) - 10} more"
        logger.warning(
            "The meta disc's recovery/ holds %d file(s) that "
            "recovery/MANIFEST.sha256 does not list, so `lcsas meta verify "
            "--strict` will fail: %s. Build from a git checkout, which "
            "bundles only tracked files; if these files are tracked, "
            "regenerate the manifest with `make -C recovery manifest`.",
            len(unlisted), shown,
        )

    # RST-05: required-contents completeness gate (default ON).  A
    # meta disc missing per-target binaries or root restore artifacts
    # is incomplete; fail loud unless --allow-incomplete.
    #
    # Issue #443: the per-repo metadata/<repo>/keys entries (#367) are
    # split out of that gate. "--allow-incomplete" means "dev build
    # lacking per-target binaries" — habitual, and lives in build
    # scripts. A missing repo key is the SAME survivability gap #437
    # already gates behind --allow-missing-metadata (mirror unreachable
    # at build time); it must not be silenced by the dev-build flag just
    # because this time the mirror *was* reachable but had no keys/.
    # So: group 1 (the per-repo keys entries) answers only to
    # --allow-missing-metadata; group 2 (everything else) keeps today's
    # --allow-incomplete behaviour unchanged.
    missing = builder.missing_required_contents()
    if missing:
        from lcsas.meta.required_contents import is_per_repo_keys_gap
        keys_missing = [m for m in missing if is_per_repo_keys_gap(m)]
        other_missing = [m for m in missing if not is_per_repo_keys_gap(m)]
        gate_failed = False

        if other_missing:
            if getattr(args, "allow_incomplete", False):
                logger.warning(
                    "Meta disc is INCOMPLETE (%d required artifact(s) "
                    "missing) — building anyway because --allow-incomplete "
                    "was given. Missing: %s",
                    len(other_missing), ", ".join(other_missing),
                )
            else:
                from lcsas.meta.required_contents import APPROVED_TARGETS
                logger.error(
                    "Meta disc is INCOMPLETE — %d required artifact(s) "
                    "missing:", len(other_missing),
                )
                for target in APPROVED_TARGETS:
                    prefix = f"recovery/bin/{target}/"
                    target_missing = [
                        m[len(prefix):] if m.startswith(prefix) else m
                        for m in other_missing
                        if m.startswith(prefix)
                        or m == f"recovery/bin/{target}/python"
                    ]
                    if target_missing:
                        logger.error(
                            "  [%s] %s", target, ", ".join(target_missing)
                        )
                root_missing = [
                    m for m in other_missing
                    if not m.startswith("recovery/bin/")
                ]
                if root_missing:
                    logger.error("  [root] %s", ", ".join(root_missing))
                logger.error(
                    "Build the binaries (make build-recovery / "
                    "make keyshare-arches, sh recovery/scripts/fetch_upstream.sh) "
                    "or pass --allow-incomplete for a dev build."
                )
                gate_failed = True

        if keys_missing:
            repo_ids = sorted(
                m[len("metadata/"):-len("/keys")] for m in keys_missing
            )
            if getattr(args, "allow_missing_metadata", False):
                logger.warning(
                    "ACKNOWLEDGED: %d repo(s) bundled onto this meta disc "
                    "have NO keys/ in their metadata — building anyway "
                    "because --allow-missing-metadata was given. This disc "
                    "CANNOT decrypt: %s",
                    len(keys_missing), ", ".join(repo_ids),
                )
                logger.warning(
                    "Recorded on the disc as repo_metadata.acknowledged in "
                    "volume_info.json."
                )
            else:
                logger.error(
                    "Meta disc is missing metadata/<repo>/keys for %d "
                    "repo(s) — this meta disc could not decrypt them: %s",
                    len(keys_missing), ", ".join(repo_ids),
                )
                logger.error(
                    "--allow-incomplete does NOT cover this — a missing "
                    "repo key is a survivability gap, not a dev-build "
                    "hedge. Resolve this in one of two ways:"
                )
                logger.error(
                    "  1. Mount/repair the mirror so its keys/ subtree is "
                    "present, then rebuild — the fix that actually makes "
                    "the disc restorable."
                )
                logger.error(
                    "  2. `--allow-missing-metadata` to accept the gap for "
                    "this build; the acknowledgement is recorded on the "
                    "disc."
                )
                gate_failed = True

        if gate_failed:
            return 1

    # Issue #367: recommended-contents advisory (WARN only).  A meta disc
    # missing the tier-2b restic hedge or lcsas-keyshare can still
    # restore via tier 1 + tier 3, so warn rather than fail — and stock
    # restic comes from the upstream cache, so it is legitimately absent on
    # a cold-cache build.
    recommended_missing = builder.missing_recommended_contents()
    if recommended_missing:
        logger.warning(
            "Meta disc is missing %d RECOMMENDED artifact(s) (it can still "
            "restore without them — stock restic tier-2b hedge and/or "
            "lcsas-keyshare): %s",
            len(recommended_missing), ", ".join(recommended_missing),
        )

    # Issue #435: state which repos' Rustic keys this disc carries.  An
    # unreachable mirror is skipped by design (a stale catalog row must
    # not block a meta disc) — but shipping a meta disc that cannot
    # decrypt a repository has to be a decision the owner sees, not an
    # accident discovered during a restore.
    results = builder.metadata_results
    if results:
        bundled = [r for r in results if r.bundled]
        skipped = [r for r in results if not r.bundled]
        logger.info(
            "Repository metadata bundled for %d of %d catalogued repo(s): %s",
            len(bundled), len(results),
            ", ".join(r.repo_id for r in bundled) or "(none)",
        )
        if skipped:
            logger.warning(
                "%d repo(s) have NO keys on this meta disc: %s — their "
                "packs cannot be decrypted from this disc alone. Mount the "
                "mirror(s) and rebuild if that is not what you want.",
                len(skipped), ", ".join(r.repo_id for r in skipped),
            )

        # Issue #437: the key gate.  Reporting alone (#436) still lets a
        # build script that only checks the exit code master and burn a
        # disc that cannot decrypt a repository's packs.  Fail the build —
        # but ONLY for a gap that is genuinely a survivability defect:
        #
        #   * has packs on a volume that still exists  (data is out there
        #     right now that needs these keys), AND
        #   * is not retired  (the owner has not said the mirror is
        #     gone for good — `lcsas repo retire`), AND
        #   * got no keys bundled.
        #
        # Everything else keeps #436's behaviour exactly: report, exit 0.
        # That includes an empty repo, a freshly-registered repo, a
        # retired repo, and — load-bearing — a build with no catalog at
        # all, which bundles zero repos by design and is a supported,
        # tested configuration.  A rule that failed 2-of-3 repos while
        # passing 0-of-3 is the incoherence #437 exists to avoid.
        gaps = [
            r for r in skipped
            if not r.retired and r.packs_on_discs > 0
        ]
        if gaps:
            if getattr(args, "allow_missing_metadata", False):
                logger.warning(
                    "ACKNOWLEDGED: %d repo(s) with packs on discs have NO "
                    "keys on this meta disc — building anyway because "
                    "--allow-missing-metadata was given. This disc CANNOT "
                    "decrypt: %s",
                    len(gaps),
                    ", ".join(
                        f"{r.repo_id} ({r.packs_on_discs} pack(s) on discs)"
                        for r in gaps
                    ),
                )
                logger.warning(
                    "Recorded on the disc as repo_metadata.acknowledged in "
                    "volume_info.json."
                )
            else:
                logger.error(
                    "Meta disc carries NO Rustic keys for %d repo(s) whose "
                    "packs are on discs that still exist — this meta disc "
                    "could not decrypt them:", len(gaps),
                )
                for r in gaps:
                    logger.error(
                        "  %s — %d pack(s) on discs; mirror %s: %s",
                        r.repo_id, r.packs_on_discs,
                        r.mirror_path or "<unset>", r.detail,
                    )
                logger.error("Resolve this in one of three ways:")
                logger.error(
                    "  1. Mount the mirror(s) above and rebuild — the fix "
                    "that actually makes the disc restorable."
                )
                logger.error(
                    "  2. `lcsas repo retire <repo>` if a mirror is gone "
                    "for good. Reversible, and it keeps the repo's pack and "
                    "snapshot history (unlike `repo remove --force`, which "
                    "deletes it). Also drop its [repos.<name>] block from "
                    "your config — retirement is a catalog fact, and scan/"
                    "burn still fail on a config mirror_path that is gone."
                )
                logger.error(
                    "  3. `--allow-missing-metadata` to accept the gap for "
                    "this build; the acknowledgement is recorded on the disc."
                )
                return 1

    logger.info(f"Meta disc built successfully at {output}")
    logger.info("Contents:")
    logger.info("  tools/          Portable rustic, xorriso, python3 + libraries")
    logger.info("  lcsas/          LCSAS source code")
    logger.info("  restore.sh      Bootstrap restore script")
    logger.info("  README_RESTORE.md  Restore instructions")
    logger.info("  START_HERE.txt  Plain-language guide for heirs")
    return 0


def cmd_meta_verify(args: argparse.Namespace) -> int:
    """Audit a built meta disc against its recovery/MANIFEST.sha256.

    Phase 21.8.  Mirrors `make verify-recovery` (which audits the
    upstream-binary cache on the build host) but operates on the
    output of `lcsas meta build` — useful for catching bit-rot on
    a meta disc directory before it's mastered into an ISO, or
    for periodic disc-health checks on a mounted meta disc.
    """
    import hashlib

    out = args.output.resolve()
    manifest_path = out / "recovery" / "MANIFEST.sha256"
    if not manifest_path.is_file():
        logger.error(
            "No recovery/MANIFEST.sha256 under %s — is this really a meta disc?",
            out,
        )
        return 1

    recovery_root = manifest_path.parent
    expected: dict[str, str] = {}
    for line in manifest_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("  ", 1)
        if len(parts) != 2:
            logger.warning("Malformed manifest line: %r", line)
            continue
        sha, rel = parts
        # Manifest entries look like "./scripts/restore.sh"; normalize.
        rel = rel[2:] if rel.startswith("./") else rel
        expected[rel] = sha.lower()

    if not expected:
        logger.error("Manifest %s contained no entries.", manifest_path)
        return 1

    mismatched: list[tuple[str, str, str]] = []
    missing: list[str] = []
    checked = 0
    for rel, want_sha in expected.items():
        path = recovery_root / rel
        if not path.is_file():
            missing.append(rel)
            continue
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        got = h.hexdigest()
        if got != want_sha:
            mismatched.append((rel, want_sha, got))
        else:
            checked += 1

    extras: list[str] = []
    if args.strict:
        # The two manifests are never counted: the curation rule in
        # recovery/Makefile keeps both out of MANIFEST.sha256 (a manifest
        # cannot list itself).  `meta build` warns with the same check.
        from lcsas.meta.builder import files_outside_manifest
        extras = files_outside_manifest(recovery_root, expected.keys())

    # RST-05: required-contents check, independent of the manifest.  The
    # manifest is regenerated from whatever was bundled, so it cannot
    # catch a *never-bundled* target — this contract check can.  Rooted
    # at the meta disc output dir (not recovery/), so it also covers the
    # root-level restore artifacts that live outside recovery/.
    from lcsas.meta.required_contents import VINTAGE_NOTE, required_meta_paths
    absent: list[str] = []
    for rel in required_meta_paths():
        path = out / rel
        if rel == "tools":
            if not path.is_dir():
                absent.append(rel)
        elif not path.is_file():
            absent.append(rel)

    for rel in sorted(missing):
        logger.error("  MISSING  %s", rel)
    for rel, want, got in sorted(mismatched):
        logger.error(
            "  MISMATCH %s\n    expected %s\n    got      %s",
            rel, want, got,
        )
    for rel in sorted(extras):
        logger.error("  EXTRA    %s  (present on disk but absent from manifest)", rel)
    for rel in sorted(absent):
        logger.error("  ABSENT   %s  (required by the meta disc contract)", rel)

    total_issues = len(missing) + len(mismatched) + len(extras) + len(absent)
    if total_issues:
        logger.error(
            "Meta disc verify FAILED: %d issue(s) "
            "(%d missing, %d mismatched, %d extra, %d absent).",
            total_issues, len(missing), len(mismatched), len(extras), len(absent),
        )
        if absent:
            logger.error("Note: %s", VINTAGE_NOTE)
        return 1

    logger.info(
        "Meta disc verify PASSED: %d files match recovery/MANIFEST.sha256; "
        "all required-contents artifacts present.",
        checked,
    )
    return 0
