"""A scratch LCSAS install for owner-side integration tests (#945).

Real rustic repositories, real xorriso mastering TEST_TINY ISOs, and every
owner command through ``lcsas.cli.main.main``: the seam the owner uses.
Only the drive is left out: :meth:`ScratchInstall.mark_burned` records a
session as burned the way ``tests/e2e/drill_kit/build_kit.py`` does
(``burn_session(skip_burn=True)``).

:meth:`ScratchInstall.heir_restore` then runs the real ``restore.sh`` of a
META disc built from this checkout against the staged disc trees, the
heir's path from the estate card.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import shutil
import sqlite3
import subprocess
from pathlib import Path

from lcsas.cli.main import main

HINT = "Sealed envelope held by the test custodian"


class ScratchInstall:
    """An owner's install under *root*: config, catalog, mirrors, staging."""

    def __init__(
        self,
        root: Path,
        repos: tuple[str, ...] = ("alpha", "beta"),
        *,
        survivability: str | None = None,
        extra_config: str = "",
        locations: tuple[str, ...] = ("Home_Shelf",),
    ) -> None:
        self.root = root
        self.repos = repos
        for sub in ("keys", "repos", "data", "db", "mb", "st", "tmp"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        self.config = root / "lcsas.toml"
        self.db = root / "db" / "catalog.db"
        self.staging = root / "st"
        if survivability is None:
            survivability = (
                "[survivability]\n"
                'archive_owner = "Test Owner"\n'
                'technical_contact = "Test Contact"\n'
                f'password_hints = "{HINT}"\n'
            )
        text = (
            "[paths]\n"
            f'mirror_base = "{root / "mb"}"\n'
            f'staging = "{root / "st"}"\n'
            f'catalog = "{self.db}"\n'
            "[defaults]\n"
            'media_type = "TEST_TINY"\n'
            f'location = "{locations[0]}"\n'
            'label_prefix = "LCSAS"\n'
            "metadata_reserve_mb = 0\n"
            + survivability + extra_config
        )
        for name in repos:
            text += (
                f"[repos.{name}]\n"
                f'mirror_path = "{self.mirror(name)}"\n'
                f'password_file = "{self.key(name)}"\n'
            )
        self.config.write_text(text)
        assert self.lcsas("init") == 0
        for loc in locations[1:]:
            assert self.lcsas("location", "add", loc) == 0
        for i, name in enumerate(repos):
            self.key(name).write_text(f"scratch-password-{name}-{i}\n")
            (self.data(name)).mkdir(parents=True, exist_ok=True)
            self.rustic(name, "init")
        self._rng = random.Random(945)

    # -- paths ---------------------------------------------------------
    def mirror(self, name: str) -> Path:
        return self.root / "repos" / name

    def key(self, name: str) -> Path:
        return self.root / "keys" / f"{name}.key"

    def data(self, name: str) -> Path:
        return self.root / "data" / name

    # -- tools ---------------------------------------------------------
    def rustic(self, name: str, *args: str) -> str:
        out = subprocess.run(
            ["rustic", "-r", str(self.mirror(name)), "--password-file",
             str(self.key(name)), *args],
            capture_output=True, text=True, check=True,
            env={**os.environ, "TMPDIR": str(self.root / "tmp"),
                 "HOME": str(self.root / "tmp")},
        )
        return out.stdout

    def lcsas(self, *argv: str) -> int:
        return main(["--config", str(self.config), *argv])

    def backup(self, name: str, filename: str, size: int = 6000) -> None:
        """Write *filename* into the set's folder and back the folder up."""
        (self.data(name) / filename).write_bytes(self._rng.randbytes(size))
        self.rustic(name, "backup", str(self.data(name)), "--as-path",
                    f"/{name}")

    def add_repos(self) -> None:
        for name in self.repos:
            assert self.lcsas("repo", "add", name, str(self.mirror(name))) == 0

    # -- catalog -------------------------------------------------------
    def query(self, sql: str, *params: object) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def repo_id(self, name: str) -> str:
        (row,) = self.query("SELECT repo_id FROM repos WHERE name = ?", name)
        return str(row[0])

    def sessions(self) -> list[str]:
        return [str(r[0]) for r in self.query(
            "SELECT session_id FROM burn_sessions ORDER BY created_at, rowid")]

    def labels(self) -> list[str]:
        return [str(r[0]) for r in self.query(
            "SELECT label FROM volumes ORDER BY label")]

    def mark_burned(self, session_id: str, location: str | None = None) -> None:
        """Record *session_id*'s volumes as burned, no disc written."""
        from lcsas.burn.orchestrator import BurnOrchestrator
        from lcsas.cli.common import _load_config_opt, _open_catalog
        from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
        from lcsas.iso.xorriso import SubprocessXorrisoRunner

        args = argparse.Namespace(config=str(self.config), db=None)
        config = _load_config_opt(args)
        assert config is not None
        with _open_catalog(args, config) as conn:
            orch = BurnOrchestrator(
                config, conn,
                SubprocessXorrisoRunner(tmpdir=config.staging_path),
                SubprocessDVDisasterRunner(tmpdir=config.staging_path),
            )
            orch.burn_session(
                session_id, location=location or config.default_location,
                skip_burn=True, interactive=False,
            )

    # -- staged discs --------------------------------------------------
    def disc_trees(self) -> dict[str, Path]:
        """Every staged disc tree by label (``st/<session>/<LABEL>/``)."""
        trees: dict[str, Path] = {}
        for session in self.staging.iterdir():
            if not session.is_dir():
                continue
            for tree in session.iterdir():
                if tree.is_dir() and tree.name.startswith("LCSAS_"):
                    trees[tree.name] = tree
        return trees

    def disc_snapshots(self, label: str, name: str) -> set[str]:
        """The snapshot files of set *name* on disc *label*."""
        snaps = (self.disc_trees()[label] / "metadata" / self.repo_id(name)
                 / "snapshots")
        return {p.name for p in snaps.iterdir()} if snaps.is_dir() else set()

    def mirror_snapshots(self, name: str) -> set[str]:
        return {p.name for p in (self.mirror(name) / "snapshots").iterdir()}

    def disc_user_version(self, label: str) -> int:
        db = self.disc_trees()[label] / "catalog.db"
        if not db.is_file():
            (db,) = list(self.disc_trees()[label].rglob("*.db"))
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()

    # -- the heir ------------------------------------------------------
    def heir_restore(
        self, name: str, out: Path, labels: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run the real ``restore.sh`` of a META disc built from this
        checkout against copies of the staged disc trees (*labels*, all
        by default), with no questions."""
        from lcsas.meta.builder import MetaVolumeBuilder

        work = self.root / f"heir-{out.name}"
        mnt = work / "mnt"
        mnt.mkdir(parents=True)
        for label, tree in sorted(self.disc_trees().items()):
            if labels is None or label in labels:
                shutil.copytree(tree, mnt / label)
        MetaVolumeBuilder(
            mnt / "LCSAS_META", catalog_db_path=None,
            allow_no_dvdisaster_source=True,
        ).build()
        (work / "home").mkdir()
        (work / "tmp").mkdir()
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(work / "home"),
            "TMPDIR": str(work / "tmp"),
            "LCSAS_MOUNT_DIRS": str(mnt),
            "LCSAS_TOOLS_DIR": str(work / "tools"),
            "LCSAS_NEWEST_DISC_PROMPT": "0",
            "LCSAS_ANOTHER_SET": "0",
            "LCSAS_INTERACTIVE": "off",
            "LCSAS_BACKUP_SET": name,
        }
        return subprocess.run(
            ["sh", str(mnt / "LCSAS_META" / "restore.sh"),
             "--password-file", str(self.key(name)),
             "--restore-folder", str(out)],
            capture_output=True, text=True, env=env,
            stdin=subprocess.DEVNULL, timeout=600,
        )


def tree_digest(folder: Path) -> dict[str, str]:
    """Relative path -> sha256 of every file under *folder*."""
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*")) if p.is_file()
    }
