"""Tests for the ``lcsas meta`` command (``lcsas.cli.commands.meta``)."""

from __future__ import annotations

import json
from pathlib import Path

from lcsas.cli.main import build_parser, main
from lcsas.db.connection import get_connection
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volumes import create_volume


class TestCmdMetaBuild:
    def test_meta_build_dispatches(self, capsys):
        """Meta build command parses correctly."""
        parser = build_parser()
        args = parser.parse_args(["meta", "build", "--output", "/tmp/meta"])
        assert args.command == "meta"
        assert args.meta_command == "build"
        assert args.output == Path("/tmp/meta")


class TestCmdMetaVerify:
    """Phase 21.8 — `lcsas meta verify <dir>` audits a built meta disc
    against its recovery/MANIFEST.sha256."""

    def _populate_required_contents(self, out):
        """Lay down every artifact the RST-05 required-contents contract
        demands, so the manifest-hash assertions below can be exercised
        on an otherwise-complete tree (the contract check is orthogonal to
        the hash check and would otherwise flag ABSENT)."""
        from lcsas.meta.required_contents import required_meta_paths

        for rel in required_meta_paths():
            path = out / rel
            if rel == "tools":
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_bytes(b"contract-stub\n")

    def _make_meta(self, tmp_path, files: dict[str, bytes], *, complete=True):
        """Build a synthetic meta-volume tree with a matching MANIFEST.

        ``files`` maps relative paths under ``recovery/`` (e.g.
        ``"scripts/restore.sh"``) to their byte content.  The manifest
        is written with one line per file plus a header comment so
        we exercise the comment-skip path too.

        ``complete`` (default True) also lays down the RST-05
        required-contents artifacts so the manifest-hash tests aren't
        derailed by the orthogonal contract check.
        """
        import hashlib

        out = tmp_path / "meta"
        out.mkdir()
        recovery = out / "recovery"
        recovery.mkdir()
        if complete:
            self._populate_required_contents(out)

        lines = ["# Phase 21.4 merged manifest (test fixture)"]
        for rel, content in sorted(files.items()):
            path = recovery / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            sha = hashlib.sha256(content).hexdigest()
            lines.append(f"{sha}  ./{rel}")
        (recovery / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")
        return out

    def test_dispatcher_parses(self):
        """`meta verify <dir>` parses correctly and exposes the output
        path + the strict flag."""
        parser = build_parser()
        args = parser.parse_args(["meta", "verify", "/tmp/meta", "--strict"])
        assert args.command == "meta"
        assert args.meta_command == "verify"
        assert args.output == Path("/tmp/meta")
        assert args.strict is True

    def test_passes_on_clean_meta_volume(self, tmp_path, capsys, caplog):
        """All listed files present + correct SHA → exit 0 with PASSED."""
        out = self._make_meta(tmp_path, {
            "VERSION": b"21.8\n",
            "scripts/restore.sh": b"#!/bin/sh\n",
            "bin/x86_64-unknown-linux-musl/rustic-static": b"fake binary",
        })
        caplog.clear()

        result = main(["meta", "verify", str(out)])
        assert result == 0
        assert "PASSED" in caplog.text
        # Exactly 3 files were checked.
        assert "3 files" in caplog.text

    def test_missing_manifest_returns_error(self, tmp_path, caplog):
        """Pointing at something that isn't a meta-volume produces a
        clear error, not an internal stack trace."""
        not_a_meta = tmp_path / "not-a-meta"
        not_a_meta.mkdir()
        caplog.clear()

        result = main(["meta", "verify", str(not_a_meta)])
        assert result == 1
        assert "No recovery/MANIFEST.sha256" in caplog.text

    def test_detects_missing_file(self, tmp_path, caplog):
        """A file listed in the manifest but absent from disk is
        reported and the exit code is 1."""
        out = self._make_meta(tmp_path, {
            "VERSION": b"21.8\n",
            "scripts/restore.sh": b"#!/bin/sh\n",
        })
        # Delete one file after manifest is built.
        (out / "recovery" / "scripts" / "restore.sh").unlink()
        caplog.clear()

        result = main(["meta", "verify", str(out)])
        assert result == 1
        assert "MISSING" in caplog.text
        assert "scripts/restore.sh" in caplog.text
        assert "1 missing" in caplog.text

    def test_detects_corrupted_file(self, tmp_path, caplog):
        """A file whose SHA changed (bit-rot, accidental edit) is
        reported with both expected and observed hashes."""
        out = self._make_meta(tmp_path, {
            "VERSION": b"original\n",
        })
        # Corrupt the file after MANIFEST is written.
        (out / "recovery" / "VERSION").write_bytes(b"corrupted\n")
        caplog.clear()

        result = main(["meta", "verify", str(out)])
        assert result == 1
        assert "MISMATCH" in caplog.text
        assert "VERSION" in caplog.text
        assert "1 mismatched" in caplog.text

    def test_strict_flags_extras(self, tmp_path, caplog):
        """`--strict` mode catches files present under recovery/ but
        absent from the manifest — e.g. files that snuck in after
        manifest generation, indicating the manifest is stale."""
        out = self._make_meta(tmp_path, {
            "VERSION": b"21.8\n",
        })
        # Drop a stowaway file.
        (out / "recovery" / "stowaway.txt").write_bytes(b"surprise!\n")
        caplog.clear()

        result = main(["meta", "verify", str(out), "--strict"])
        assert result == 1
        assert "EXTRA" in caplog.text
        assert "stowaway.txt" in caplog.text

    def test_strict_skips_the_docs_manifest(self, tmp_path, caplog):
        """`--strict` must not count recovery/docs/MANIFEST.sha256 as an
        extra.  `make -C recovery manifest` excludes both manifests from
        MANIFEST.sha256 by rule (a manifest cannot list itself), so every
        real build ships docs/MANIFEST.sha256 unlisted; counting it made
        `make meta-gate` fail on every build since #432."""
        from lcsas.meta.required_contents import required_meta_paths

        # List the contract stubs under recovery/ so the only unlisted
        # file is the docs manifest.
        listed = {
            rel[len("recovery/"):]: b"contract-stub\n"
            for rel in required_meta_paths()
            if rel.startswith("recovery/")
        }
        out = self._make_meta(tmp_path, {"VERSION": b"21.8\n", **listed})
        (out / "recovery" / "docs").mkdir(exist_ok=True)
        (out / "recovery" / "docs" / "MANIFEST.sha256").write_bytes(
            b"0" * 64 + b"  ./START_HERE.txt\n"
        )
        caplog.clear()

        result = main(["meta", "verify", str(out), "--strict"])
        assert result == 0, caplog.text
        assert "EXTRA" not in caplog.text

    def test_strict_still_flags_other_docs_files(self, tmp_path, caplog):
        """Only the docs manifest itself is exempt: any other unlisted
        file under recovery/docs/ is still an extra."""
        out = self._make_meta(tmp_path, {
            "VERSION": b"21.8\n",
        })
        (out / "recovery" / "docs").mkdir(exist_ok=True)
        (out / "recovery" / "docs" / "MANIFEST.sha256").write_bytes(b"")
        (out / "recovery" / "docs" / "stowaway.txt").write_bytes(b"x\n")
        caplog.clear()

        result = main(["meta", "verify", str(out), "--strict"])
        assert result == 1
        assert "docs/stowaway.txt" in caplog.text
        assert "docs/MANIFEST.sha256" not in caplog.text

    def test_extras_ignored_without_strict(self, tmp_path, caplog):
        """Without --strict, an extra file under recovery/ is allowed.

        This is the common case: the meta-volume may legitimately
        carry per-host scratch files that aren't part of the
        integrity contract.
        """
        out = self._make_meta(tmp_path, {
            "VERSION": b"21.8\n",
        })
        (out / "recovery" / "stowaway.txt").write_bytes(b"ok\n")
        caplog.clear()

        result = main(["meta", "verify", str(out)])
        assert result == 0
        assert "PASSED" in caplog.text
        assert "EXTRA" not in caplog.text

    def test_reports_absent_required_artifacts(self, tmp_path, caplog):
        """RST-05: a built meta-volume whose manifest hashes all pass but
        which is *missing a required per-target artifact* must FAIL verify
        with ABSENT, naming the gap.  Before the gate this returned 0
        because the manifest only pins what was bundled."""
        out = self._make_meta(tmp_path, {"VERSION": b"21.8\n"})
        # Remove one required target entirely (simulates a disc that
        # never shipped the Windows binaries).
        import shutil
        shutil.rmtree(out / "recovery" / "bin" / "x86_64-pc-windows-gnu")
        caplog.clear()

        result = main(["meta", "verify", str(out)])
        assert result == 1
        assert "ABSENT" in caplog.text
        assert "x86_64-pc-windows-gnu" in caplog.text
        assert "lcsas-restore.exe" in caplog.text
        # The vintage note is surfaced so an operator reads an ABSENT
        # verdict as the honest signal, not a false alarm.
        assert "required-contents contract" in caplog.text

    def test_reports_absent_root_artifact(self, tmp_path, caplog):
        """A missing root-level restore artifact (outside recovery/) is
        also caught — the contract check is rooted at the output dir."""
        out = self._make_meta(tmp_path, {"VERSION": b"21.8\n"})
        (out / "restore.bat").unlink()
        caplog.clear()

        result = main(["meta", "verify", str(out)])
        assert result == 1
        assert "ABSENT" in caplog.text
        assert "restore.bat" in caplog.text


class TestCmdMetaBuildGate:
    """RST-05: `lcsas meta build` fails by default on an incomplete
    rescue disc; `--allow-incomplete` restores single-arch dev behavior."""

    def test_require_complete_fails_listing_missing_targets(
        self, tmp_path, monkeypatch, caplog
    ):
        """With an empty upstream cache (no rustic/python trees) the
        default build fails and enumerates every approved triple plus
        the missing root artifacts."""
        from lcsas.meta.required_contents import APPROVED_TARGETS

        # Point the upstream cache at an empty dir so no rustic/python
        # trees get bundled; tier-1 source bins may exist but rustic +
        # python are guaranteed absent → INCOMPLETE.
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "empty-cache"))
        out = tmp_path / "meta"
        caplog.clear()

        # --allow-no-dvdisaster-source so the empty cache trips the RST-05
        # completeness gate under test rather than the FMT-02 source gate.
        result = main([
            "meta", "build", "--output", str(out),
            "--allow-no-dvdisaster-source",
        ])
        assert result == 1
        assert "INCOMPLETE" in caplog.text
        # Every approved target is named in the missing report.
        for target in APPROVED_TARGETS:
            assert target in caplog.text
        # The remediation hint points at the build targets.
        assert "--allow-incomplete" in caplog.text

    def test_allow_incomplete_builds(self, tmp_path, monkeypatch, caplog):
        """--allow-incomplete downgrades the gate to a warning and the
        build returns 0 (the historical single-arch dev path)."""
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "empty-cache"))
        out = tmp_path / "meta"
        caplog.clear()

        result = main([
            "meta", "build", "--output", str(out), "--allow-incomplete",
            "--allow-no-dvdisaster-source",
        ])
        assert result == 0
        assert "INCOMPLETE" in caplog.text
        assert "built successfully" in caplog.text

    def test_reports_which_repos_got_their_keys(
        self, tmp_path, monkeypatch, caplog
    ):
        """Issue #435: a repo whose mirror is unreachable is skipped — but
        the operator must be able to tell from the command's output that
        this disc carries no keys for it."""
        import sqlite3

        mirror = tmp_path / "mirror" / "family"
        (mirror / "keys").mkdir(parents=True)
        (mirror / "keys" / "k0").write_text("key-material\n")

        db = tmp_path / "catalog.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE repositories (repo_id TEXT, mirror_path TEXT)"
        )
        conn.executemany(
            "INSERT INTO repositories VALUES (?, ?)",
            [("family", str(mirror)), ("gone", str(tmp_path / "unmounted"))],
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "empty-cache"))
        out = tmp_path / "meta"
        caplog.clear()

        result = main([
            "--db", str(db),
            "meta", "build", "--output", str(out), "--allow-incomplete",
            "--allow-no-dvdisaster-source",
        ])
        # Still exit 0: a stale catalog row must not block a rescue disc.
        assert result == 0
        assert "bundled for 1 of 2 catalogued repo(s): family" in caplog.text
        assert "NO keys on this meta disc: gone" in caplog.text
        assert (out / "metadata" / "family" / "keys" / "k0").is_file()


class TestCmdMetaBuildKeyGate:
    """#437: `lcsas meta build` FAILS when a live repo whose packs are on
    discs got no Rustic keys bundled.

    Reporting alone (#436) still lets a build script that only checks the
    exit code master and burn a disc that cannot decrypt a repository. But a
    blanket hard-fail is undecidable — LCSAS cannot tell "mirror
    unmounted right now" from "mirror gone for good" — so the gate is
    narrowed to a gap that is genuinely a survivability defect:
    packs-on-discs AND not-retired AND no-keys.
    """

    @staticmethod
    def _catalog(
        tmp_path, *, mirror_exists: bool, packs_on_discs: bool = True,
        retired: bool = False, volume_status: str = "BURNED",
    ):
        """A real (schema-current) catalog with one repo named 'family'."""
        from lcsas.db.packs import register_pack
        from lcsas.db.repos import register_repo as _register
        from lcsas.db.volume_packs import bulk_link_packs

        mirror = tmp_path / "mirror" / "family"
        if mirror_exists:
            (mirror / "keys").mkdir(parents=True)
            (mirror / "keys" / "k0").write_text("key-material\n")

        db = tmp_path / "catalog.db"
        conn = get_connection(db)
        create_all(conn)
        _register(conn, "family", "Family", str(mirror))
        if packs_on_discs:
            pack = register_pack(
                conn, sha256="f" * 64, size_bytes=1024, repo_id="family"
            )
            vol = create_volume(
                conn, label="V001", uuid="u-001",
                media_type="BD25", capacity_bytes=25_000_000_000,
                status=volume_status,
            )
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
        if retired:
            from lcsas.db.repos import set_repo_status
            set_repo_status(conn, "family", "retired")
        conn.commit()
        conn.close()
        return db

    @staticmethod
    def _build(db, tmp_path, monkeypatch, *, extra=()):
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "empty-cache"))
        out = tmp_path / "meta"
        argv = [
            "--db", str(db),
            "meta", "build", "--output", str(out), "--allow-incomplete",
            "--allow-no-dvdisaster-source", *extra,
        ]
        return main(argv), out

    # ── the gate fires ────────────────────────────────────────────

    def test_fails_for_active_repo_with_packs_and_no_keys(
        self, tmp_path, monkeypatch, caplog
    ):
        db = self._catalog(tmp_path, mirror_exists=False)
        caplog.clear()
        rc, _out = self._build(db, tmp_path, monkeypatch)
        assert rc == 1
        assert "could not decrypt" in caplog.text
        assert "family" in caplog.text

    def test_failure_names_all_three_ways_out(
        self, tmp_path, monkeypatch, caplog
    ):
        """An error that states the problem without stating the remedies
        just moves the dead end. All three must be offered."""
        db = self._catalog(tmp_path, mirror_exists=False)
        caplog.clear()
        rc, _out = self._build(db, tmp_path, monkeypatch)
        assert rc == 1
        assert "Mount the mirror" in caplog.text
        assert "lcsas repo retire" in caplog.text
        assert "--allow-missing-metadata" in caplog.text
        # `repo retire` must be offered WITH the warning that the
        # destructive neighbour deletes history — an operator who reaches
        # for `repo remove --force` to silence this gate destroys the pack
        # and snapshot record of data still sitting on their discs.
        assert "repo remove --force" in caplog.text
        assert "deletes it" in caplog.text

    def test_gate_ignores_is_pruned(self, tmp_path, monkeypatch, caplog):
        """packs.is_pruned is NOT in the tier-1 frozen surface, so tier-1
        will happily restore a pruned pack that is still on a disc. A repo
        whose on-disc packs are all pruned therefore still needs its keys.
        """
        db = self._catalog(tmp_path, mirror_exists=False)
        conn = get_connection(db)
        conn.execute("UPDATE packs SET is_pruned = 1")
        conn.commit()
        conn.close()
        caplog.clear()
        rc, _out = self._build(db, tmp_path, monkeypatch)
        assert rc == 1

    # ── the gate stays silent ─────────────────────────────────────

    def test_silent_for_retired_repo(self, tmp_path, monkeypatch, caplog):
        """`lcsas repo retire` is the operator saying the mirror is gone
        for good — that is a documented state, not a defect."""
        db = self._catalog(tmp_path, mirror_exists=False, retired=True)
        caplog.clear()
        rc, _out = self._build(db, tmp_path, monkeypatch)
        assert rc == 0
        assert "could not decrypt" not in caplog.text
        # ...but it is still REPORTED, per #436.
        assert "NO keys on this meta disc: family" in caplog.text

    def test_silent_for_repo_with_no_packs(
        self, tmp_path, monkeypatch, caplog
    ):
        """A freshly-registered or empty repo has nothing on any disc that
        needs its keys."""
        db = self._catalog(
            tmp_path, mirror_exists=False, packs_on_discs=False
        )
        caplog.clear()
        rc, _out = self._build(db, tmp_path, monkeypatch)
        assert rc == 0
        assert "could not decrypt" not in caplog.text

    def test_silent_when_packs_only_on_destroyed_volumes(
        self, tmp_path, monkeypatch, caplog
    ):
        """`volumes.status != 'DESTROYED'` is the same filter the tier-1 C
        reader uses, so gate and reader agree on what data still exists."""
        db = self._catalog(
            tmp_path, mirror_exists=False, volume_status="DESTROYED"
        )
        caplog.clear()
        rc, _out = self._build(db, tmp_path, monkeypatch)
        assert rc == 0
        assert "could not decrypt" not in caplog.text

    def test_silent_when_keys_were_bundled(
        self, tmp_path, monkeypatch, caplog
    ):
        db = self._catalog(tmp_path, mirror_exists=True)
        caplog.clear()
        rc, out = self._build(db, tmp_path, monkeypatch)
        assert rc == 0
        assert (out / "metadata" / "family" / "keys" / "k0").is_file()

    def test_silent_for_no_catalog_build(self, tmp_path, monkeypatch, caplog):
        """LOAD-BEARING. `_bundle_metadata` returns early with no catalog,
        so a no-catalog disc bundles zero repos — a supported, tested
        configuration (the blind-restore fixture builds one). A rule that
        failed 2-of-3 repos while passing 0-of-3 is the incoherence #437
        exists to avoid.
        """
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "empty-cache"))
        out = tmp_path / "meta"
        caplog.clear()
        rc = main([
            "--db", str(tmp_path / "absent.db"),
            "meta", "build", "--output", str(out), "--allow-incomplete",
            "--allow-no-dvdisaster-source",
        ])
        assert rc == 0
        assert "could not decrypt" not in caplog.text

    # ── the valve ─────────────────────────────────────────────────

    def test_allow_missing_metadata_downgrades_to_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        db = self._catalog(tmp_path, mirror_exists=False)
        caplog.clear()
        rc, out = self._build(
            db, tmp_path, monkeypatch, extra=["--allow-missing-metadata"]
        )
        assert rc == 0
        assert "ACKNOWLEDGED" in caplog.text
        assert "family" in caplog.text
        assert "built successfully" in caplog.text

    def test_acknowledgement_is_recorded_on_the_disc(
        self, tmp_path, monkeypatch
    ):
        """The disc must carry the fact that a gap was accepted — years
        later an heir has to tell an accepted gap from a defect, and the
        build log is long gone."""
        db = self._catalog(tmp_path, mirror_exists=False)
        _rc, out = self._build(
            db, tmp_path, monkeypatch, extra=["--allow-missing-metadata"]
        )
        info = json.loads((out / "volume_info.json").read_text())
        assert info["repo_metadata"]["acknowledged"] is True
        not_bundled = info["repo_metadata"]["not_bundled"]
        assert [e["repo_id"] for e in not_bundled] == ["family"]
        assert not_bundled[0]["packs_on_discs"] == 1
        assert not_bundled[0]["retired"] is False

    def test_acknowledged_is_false_on_an_ordinary_build(
        self, tmp_path, monkeypatch
    ):
        db = self._catalog(tmp_path, mirror_exists=True)
        _rc, out = self._build(db, tmp_path, monkeypatch)
        info = json.loads((out / "volume_info.json").read_text())
        assert info["repo_metadata"]["acknowledged"] is False

    def test_allow_incomplete_does_not_silence_the_key_gate(
        self, tmp_path, monkeypatch, caplog
    ):
        """--allow-incomplete means 'dev build lacking per-target
        binaries'. It is habitual, lives in build scripts, and must NOT
        double as an override for a survivability gap — that is exactly
        why --allow-missing-metadata is a separate flag."""
        db = self._catalog(tmp_path, mirror_exists=False)
        caplog.clear()
        rc, _out = self._build(db, tmp_path, monkeypatch)  # already passes --allow-incomplete
        assert rc == 1


class TestCmdMetaBuildPerRepoKeysNotAllowIncomplete:
    """#443: `missing_required_contents()`'s per-repo ``metadata/<repo>/
    keys`` entries (#367 — a mirror WAS reachable and bundled, but had no
    ``keys/``) are the SAME failure as #437's key gate ("this meta disc
    cannot decrypt repository X"), just reached a different way (mirror
    unreachable vs. mirror reachable-but-keyless). ``--allow-incomplete``
    means "dev build lacking per-target binaries" and must not double as
    an override for either shape of that gap — only
    ``--allow-missing-metadata`` may.

    Distinct from ``TestCmdMetaBuildKeyGate`` above: that class covers an
    UNREACHABLE mirror (the repo never gets bundled at all, so it shows
    up in ``metadata_results`` as skipped). This class covers a
    REACHABLE, bundled mirror whose ``keys/`` subtree is simply absent —
    ``missing_required_contents()``'s RST-05 path, not the #437
    ``metadata_results`` path.
    """

    @staticmethod
    def _catalog_with_keyless_mirror(tmp_path):
        """A reachable mirror for repo 'family' with a config file but
        deliberately NO keys/ subtree, so `_bundle_metadata` bundles it
        (bundled=True) while `missing_required_contents()` still flags
        `metadata/family/keys` as missing."""
        mirror = tmp_path / "mirror" / "family"
        mirror.mkdir(parents=True)
        (mirror / "config").write_text("repo-config\n")

        db = tmp_path / "catalog.db"
        conn = get_connection(db)
        create_all(conn)
        register_repo(conn, "family", "Family", str(mirror))
        conn.commit()
        conn.close()
        return db

    @staticmethod
    def _build(db, tmp_path, monkeypatch, *, extra=()):
        monkeypatch.setenv("LCSAS_RECOVERY_CACHE", str(tmp_path / "empty-cache"))
        out = tmp_path / "meta"
        argv = [
            "--db", str(db),
            "meta", "build", "--output", str(out),
            "--allow-no-dvdisaster-source", *extra,
        ]
        return main(argv), out

    def test_default_build_fails_naming_the_repo(
        self, tmp_path, monkeypatch, caplog
    ):
        db = self._catalog_with_keyless_mirror(tmp_path)
        caplog.clear()
        rc, out = self._build(db, tmp_path, monkeypatch)
        assert rc == 1
        assert "metadata/<repo>/keys" in caplog.text
        assert "family" in caplog.text
        assert "could not decrypt them" in caplog.text
        # bundled=True: the repo IS on the disc, just without keys/.
        assert (out / "metadata" / "family" / "config").is_file()
        assert not (out / "metadata" / "family" / "keys").exists()

    def test_allow_incomplete_alone_still_fails(
        self, tmp_path, monkeypatch, caplog
    ):
        """This is the whole point of #443: --allow-incomplete downgrades
        the ordinary per-target-binary gaps to a warning, but must NOT
        also wave through the missing repo key."""
        db = self._catalog_with_keyless_mirror(tmp_path)
        caplog.clear()
        rc, _out = self._build(
            db, tmp_path, monkeypatch, extra=("--allow-incomplete",)
        )
        assert rc == 1
        assert "family" in caplog.text
        assert "does NOT cover this" in caplog.text
        assert "--allow-missing-metadata" in caplog.text

    def test_allow_missing_metadata_downgrades_with_acknowledged_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        db = self._catalog_with_keyless_mirror(tmp_path)
        caplog.clear()
        rc, _out = self._build(
            db, tmp_path, monkeypatch,
            extra=("--allow-incomplete", "--allow-missing-metadata"),
        )
        assert rc == 0
        assert "ACKNOWLEDGED" in caplog.text
        assert "family" in caplog.text
        assert "built successfully" in caplog.text
