"""#701: collections, minimum fill and ``status --json``.

A collection (``[collections.<name>]`` in lcsas.toml) names a location, a
media type, a minimum fill and the repositories whose discs are kept
there.  ``lcsas stage --collection <name>`` applies all four, so no
wrapper script has to pass them or keep a side file in step with the
config.  ``lcsas status --json`` gives scripts the waiting volumes per
location without parsing the text table.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.cli.main import main
from lcsas.config.media import MediaType
from lcsas.config.settings import default_config, load_config
from lcsas.db.connection import get_connection, get_memory_connection
from lcsas.db.locations import create_location
from lcsas.db.models import Pack
from lcsas.db.packs import list_packs, register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_packs import get_pack_ids_for_volume
from lcsas.db.volumes import list_volumes

# TEST_TINY: 2 MiB, no ECC share; with metadata_reserve_mb = 0.5 a volume's
# pack space is 2,097,152 - 524,288 = 1,572,864 bytes.
BIG = 700_000  # two fit on a volume (89.0%), the third goes on the next


def _pack(data_dir: Path, i: int, size: int) -> str:
    content = (f"{i:08x}".encode() * (size // 8 + 1))[:size]
    sha = hashlib.sha256(content).hexdigest()
    (data_dir / sha[:2]).mkdir(parents=True, exist_ok=True)
    (data_dir / sha[:2] / sha).write_bytes(content)
    return sha


def _mirror(root: Path, name: str) -> Path:
    mirror = root / "mirror" / name
    for sub in ("index", "snapshots", "keys"):
        (mirror / sub).mkdir(parents=True)
        (mirror / sub / "dummy").write_text("{}")
    (mirror / "config").write_text("{}")
    (mirror / "data").mkdir()
    return mirror


def _setup(
    tmp_path: Path,
    collections: str = "",
    *,
    sizes: dict[str, list[int]] | None = None,
    locations: tuple[str, ...] = ("Bank_Box",),
    register: tuple[str, ...] = ("fam", "work"),
    extra: str = "",
) -> tuple[Path, Path]:
    """Config + catalog: repositories fam and work, their packs scanned."""
    sizes = sizes if sizes is not None else {"fam": [64, 64], "work": [64]}
    staging = tmp_path / "staging"
    staging.mkdir()
    db = tmp_path / "archive.db"
    mirrors = {name: _mirror(tmp_path, name) for name in ("fam", "work")}
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(textwrap.dedent(f"""\
        [paths]
        mirror_base = "{tmp_path / 'mirror'}"
        staging = "{staging}"
        database = "{db}"

        [defaults]
        media_type = "CD700"
        metadata_reserve_mb = 0.5
        label_prefix = "T"
        allow_unverified_repo_format = true

        [repos.fam]
        mirror_path = "{mirrors['fam']}"
        sources = ["/srv/photos", "/srv/letters"]

        [repos.work]
        mirror_path = "{mirrors['work']}"
        """) + textwrap.dedent(collections))
    if extra:
        cfg.write_text(cfg.read_text().replace(
            "[defaults]\n", "[defaults]\n" + extra, 1))
    conn = get_connection(db)
    create_all(conn)
    for name in register:
        register_repo(conn, name, name, str(mirrors[name]))
    for loc in locations:
        create_location(conn, loc)
    n = 0
    for name, pack_sizes in sizes.items():
        for size in pack_sizes:
            n += 1
            sha = _pack(mirrors[name] / "data", n, size)
            if name in register:
                register_pack(conn, sha256=sha, size_bytes=size, repo_id=name)
    conn.commit()
    conn.close()
    return cfg, db


_BANK = """
    [collections.bank]
    location = "Bank_Box"
    media_type = "TEST_TINY"
    repos = ["fam"]
    """


def _fake_create_iso(self, source_dir, output_iso, volume_label, **_kw):
    Path(output_iso).write_bytes(b"\x00" * 2048)
    return Path(output_iso)


def _stage(cfg: Path, *argv: str) -> int:
    with patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.create_iso",
               _fake_create_iso):
        return main(["--config", str(cfg), "stage", *argv])


def _volumes(db: Path) -> list[tuple[str, str, str, set[str]]]:
    """(label, media_type, location, repo_ids of its packs) per volume."""
    conn = get_connection(db)
    try:
        repo_of = {p.pack_id: p.repo_id for p in list_packs(conn)}
        return [
            (v.label, v.media_type, v.location,
             {repo_of[pid] for pid in get_pack_ids_for_volume(conn, v.volume_id)})
            for v in list_volumes(conn)
        ]
    finally:
        conn.close()


def _unstaged(db: Path) -> int:
    conn = get_connection(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM packs WHERE pack_id NOT IN "
            "(SELECT pack_id FROM volume_packs)").fetchone()[0]
    finally:
        conn.close()


# --- config ---------------------------------------------------------------


class TestConfig:
    def test_collection_and_sources_load(self, tmp_path):
        cfg, _db = _setup(tmp_path, _BANK + 'min_fill_pct = 90\n')
        config = load_config(cfg)
        bank = config.collections["bank"]
        assert bank.location == "Bank_Box"
        assert bank.repos == ("fam",)
        assert bank.media_type is MediaType.TEST_TINY
        assert bank.min_fill_pct == 90.0
        assert config.repositories["fam"].sources == (
            "/srv/photos", "/srv/letters")
        assert config.repositories["work"].sources == ()

    def test_media_type_defaults_to_none(self, tmp_path):
        cfg, _db = _setup(tmp_path, """
            [collections.home]
            location = "Bank_Box"
            repos = ["fam", "work"]
            """)
        home = load_config(cfg).collections["home"]
        assert home.media_type is None
        assert home.min_fill_pct == 0.0

    @pytest.mark.parametrize(("body", "named"), [
        pytest.param('location = "X"\nrepos = "fam"\n', "repos must be a list",
                     id="repos-string"),
        pytest.param('location = "X"\nrepos = ["fam", 3]\n',
                     "repos must be a list", id="repos-number-item"),
        pytest.param('location = "X"\nrepos = ["fam"]\nmin_fill_pct = "90"\n',
                     "min_fill_pct must be a number", id="fill-quoted"),
        pytest.param('location = 7\nrepos = ["fam"]\n',
                     "location must be a quoted string", id="location-number"),
        pytest.param('repos = ["fam"]\n', "has no location", id="no-location"),
        pytest.param('location = "X"\nrepos = []\n', "has no repos",
                     id="empty-repos"),
        pytest.param('location = "X"\n', "has no repos", id="no-repos"),
        pytest.param('location = "X"\nrepos = ["fam", "famliy"]\n',
                     "'famliy'", id="unknown-repo"),
        pytest.param('location = "X"\nrepos = ["fam"]\nmin_fill_pct = 150\n',
                     "between 0 and 100", id="fill-over-100"),
        pytest.param('location = "X"\nrepos = ["fam"]\nmedia_type = "BD26"\n',
                     "media_type 'BD26' is unknown", id="bad-media"),
    ])
    def test_bad_collection_fails_config_check_naming_it(
        self, tmp_path, capsys, body, named,
    ):
        cfg, _db = _setup(tmp_path, "[collections.bank]\n" + body)
        rc = main(["--config", str(cfg), "config", "check"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "[collections.bank]" in out
        assert named in out
        assert "Unexpected error" not in out

    def test_sources_must_be_a_list_of_strings(self, tmp_path, capsys):
        cfg, _db = _setup(tmp_path)
        cfg.write_text(cfg.read_text().replace(
            'sources = ["/srv/photos", "/srv/letters"]', 'sources = "/srv"'))
        rc = main(["--config", str(cfg), "config", "check"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "[repos.fam] sources must be a list of quoted strings" in out

    def test_collection_key_typo_fails_the_check(self, tmp_path, capsys):
        cfg, _db = _setup(tmp_path, _BANK + "min_fill = 90\n")
        rc = main(["--config", str(cfg), "config", "check"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "Unknown [collections.bank] keys: ['min_fill']" in out

    def test_collections_not_a_table(self, tmp_path, capsys):
        cfg, _db = _setup(tmp_path)
        cfg.write_text('collections = "bank"\n' + cfg.read_text())
        rc = main(["--config", str(cfg), "config", "check"])
        assert rc == 1
        assert "[collections] must be a table" in capsys.readouterr().out


class TestConfigCheckCatalog:
    def test_valid_collection_passes(self, tmp_path, capsys):
        cfg, _db = _setup(tmp_path, _BANK)
        rc = main(["--config", str(cfg), "config", "check"])
        assert rc == 0, capsys.readouterr().out

    def test_unknown_location_fails(self, tmp_path, capsys):
        cfg, _db = _setup(tmp_path, _BANK.replace("Bank_Box", "Bank_Bx"))
        rc = main(["--config", str(cfg), "config", "check"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "location 'Bank_Bx' is not registered" in out
        assert "did you mean: Bank_Box" in out

    def test_unregistered_repo_fails(self, tmp_path, capsys):
        cfg, _db = _setup(tmp_path, _BANK.replace('["fam"]', '["fam", "work"]'),
                          register=("fam",))
        rc = main(["--config", str(cfg), "config", "check"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "repo 'work' is not registered in the catalog" in out

    def test_retired_repo_fails(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, _BANK)
        conn = get_connection(db)
        conn.execute("UPDATE repositories SET status = 'retired' "
                     "WHERE name = 'fam'")
        conn.commit()
        conn.close()
        rc = main(["--config", str(cfg), "config", "check"])
        assert rc == 1
        assert "repo 'fam' is retired" in capsys.readouterr().out

    def test_missing_password_file_setting_fails(self, tmp_path, capsys):
        cfg, _db = _setup(tmp_path, _BANK)
        cfg.write_text(cfg.read_text().replace(
            "allow_unverified_repo_format = true", ""))
        rc = main(["--config", str(cfg), "config", "check"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "collection 'bank': repo 'fam' has no password_file" in out

    def test_no_catalog_is_a_note_not_an_error(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, _BANK.replace("Bank_Box", "Nowhere"))
        db.unlink()
        rc = main(["--config", str(cfg), "config", "check"])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "not checked" in out
        assert not db.exists()


# --- stage --collection ------------------------------------------------------


class TestStageCollection:
    def test_stages_only_its_repos_for_its_location_on_its_media(self, tmp_path):
        cfg, db = _setup(tmp_path, _BANK)
        assert _stage(cfg, "--collection", "bank") == 0
        [(label, media, location, repos)] = _volumes(db)
        assert media == "TEST_TINY"  # not the [defaults] CD700
        assert location == "Bank_Box"
        assert repos == {"fam"}
        assert "TEST_TINY" in label
        assert _unstaged(db) == 1  # work's pack

    def test_second_run_does_not_restage_for_the_location(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, _BANK)
        assert _stage(cfg, "--collection", "bank") == 0
        assert _stage(cfg, "--collection", "bank") == 1
        assert len(_volumes(db)) == 1
        out = capsys.readouterr().out
        assert "No packs need staging" in out
        assert "Unexpected error" not in out

    @pytest.mark.parametrize("flag", [
        ["--repo", "work"], ["--for-location", "Bank_Box"], ["--media", "BD25"],
    ])
    def test_flags_the_collection_sets_are_refused(self, tmp_path, capsys, flag):
        cfg, db = _setup(tmp_path, _BANK)
        assert _stage(cfg, "--collection", "bank", *flag) == 1
        assert f"do not also pass {flag[0]}" in capsys.readouterr().out
        assert _volumes(db) == []

    def test_unknown_collection(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, _BANK)
        assert _stage(cfg, "--collection", "bnak") == 1
        out = capsys.readouterr().out
        assert "Unknown collection 'bnak'" in out
        assert "its collections: bank" in out

    def test_unregistered_location_stages_nothing(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, _BANK, locations=())
        assert _stage(cfg, "--collection", "bank") == 1
        assert "location 'Bank_Box' is not registered" in capsys.readouterr().out
        conn = get_connection(db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM burn_sessions").fetchone()[0] == 0
        finally:
            conn.close()

    def test_unregistered_repo_is_an_error_not_a_skip(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, _BANK.replace('["fam"]', '["fam", "work"]'),
                         register=("fam",))
        assert _stage(cfg, "--collection", "bank") == 1
        assert "repo 'work' is not registered" in capsys.readouterr().out
        assert _volumes(db) == []


# --- minimum fill ----------------------------------------------------------


class TestMinFill:
    SIZES = {"fam": [BIG, BIG, BIG], "work": []}

    def test_last_volume_below_min_fill_is_held_back(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, sizes=self.SIZES)
        assert _stage(cfg, "--media", "TEST_TINY", "--min-fill", "80") == 0
        out = capsys.readouterr().out
        [(_label, _m, _loc, _r)] = _volumes(db)
        assert _unstaged(db) == 1
        assert "Not staging the last volume" in out
        assert "1 pack(s), 700,000 bytes" in out
        assert "44.5% of a TEST_TINY volume's pack space" in out
        assert "below the minimum fill of 80%" in out
        assert "--partial" in out
        assert "Staged 1 volume(s)" in out

    def test_partial_stages_it(self, tmp_path):
        cfg, db = _setup(tmp_path, sizes=self.SIZES)
        assert _stage(cfg, "--media", "TEST_TINY", "--min-fill", "80",
                      "--partial") == 0
        assert len(_volumes(db)) == 2
        assert _unstaged(db) == 0

    def test_only_volume_below_min_fill_stages_nothing(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, sizes={"fam": [BIG], "work": []})
        assert _stage(cfg, "--media", "TEST_TINY", "--min-fill", "90") == 0
        out = capsys.readouterr().out
        assert "Nothing staged" in out
        assert "Session:" not in out
        assert _volumes(db) == []
        conn = get_connection(db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM burn_sessions").fetchone()[0] == 0
        finally:
            conn.close()

    def test_dry_run_reports_the_hold_back(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, sizes=self.SIZES)
        assert _stage(cfg, "--media", "TEST_TINY", "--min-fill", "80",
                      "--dry-run") == 0
        out = capsys.readouterr().out
        assert "[DRY RUN] Not staging the last volume" in out
        assert "[DRY RUN] 1 volume(s) planned" in out
        assert "89.0% of its pack space" in out
        assert _volumes(db) == []

    def test_full_volume_meets_min_fill(self, tmp_path):
        cfg, db = _setup(tmp_path, sizes={"fam": [750_000, 750_000],
                                          "work": []})
        assert _stage(cfg, "--media", "TEST_TINY", "--min-fill", "90") == 0
        assert len(_volumes(db)) == 1  # 95.4% of its pack space

    def test_collection_min_fill_applies(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, _BANK + "min_fill_pct = 90\n",
                         sizes=self.SIZES)
        assert _stage(cfg, "--collection", "bank") == 0
        # Only the last volume is checked: 89.0% is staged, 44.5% waits.
        assert len(_volumes(db)) == 1
        assert _unstaged(db) == 1
        assert "below the minimum fill of 90%" in capsys.readouterr().out

    def test_min_fill_flag_overrides_the_collection(self, tmp_path):
        cfg, db = _setup(tmp_path, _BANK + "min_fill_pct = 90\n",
                         sizes=self.SIZES)
        assert _stage(cfg, "--collection", "bank", "--min-fill", "40") == 0
        assert len(_volumes(db)) == 2
        assert _unstaged(db) == 0

    @pytest.mark.parametrize("value", ["-1", "101"])
    def test_min_fill_range(self, tmp_path, capsys, value):
        cfg, _db = _setup(tmp_path)
        assert _stage(cfg, "--min-fill", value) == 1
        assert "--min-fill must be between 0 and 100" in capsys.readouterr().out

    def test_fill_is_measured_against_pack_space_not_capacity(self, tmp_path):
        """A BD25 volume packed full is 100%, though it is ~85% of the disc."""
        config = default_config(tmp_path, tmp_path, tmp_path / "a.db")
        conn = get_memory_connection()
        orch = BurnOrchestrator(config, conn, MagicMock(), MagicMock())
        full = MediaType.BD25.usable_bytes - config.metadata_reserve_bytes
        pack = Pack(pack_id=1, sha256="a" * 64, size_bytes=full,
                    repo_id="r", is_pruned=False, created_at="")
        plans = [([pack], full)]
        kept, short = orch._trim_underfilled(plans, MediaType.BD25, 90,
                                             partial=False)
        assert kept == plans and short is None
        kept, short = orch._trim_underfilled([([pack], full // 2)],
                                             MediaType.BD25, 90, partial=False)
        assert kept == [] and short is not None
        assert round(short.fill_pct) == 50


# --- status --json ---------------------------------------------------------


class TestStatusJson:
    def test_waiting_volumes_per_location(self, tmp_path, capsys):
        cfg, db = _setup(tmp_path, _BANK, locations=("Bank_Box", "Home_Shelf"))
        assert _stage(cfg, "--collection", "bank") == 0
        [(label, _m, _l, _r)] = _volumes(db)
        capsys.readouterr()

        rc = main(["--config", str(cfg), "status", "--json"])
        captured = capsys.readouterr()
        assert rc == 0
        doc = json.loads(captured.out)
        assert doc["format"] == 1
        assert doc["waiting"] == [label]
        [waiting] = doc["locations"]["Bank_Box"]["waiting"]
        assert waiting["label"] == label
        assert waiting["media_type"] == "TEST_TINY"
        assert waiting["session_id"]
        assert doc["locations"]["Home_Shelf"] == {
            "discs": 0, "waiting": [], "needs_reburn": []}
        assert doc["packs"]["total"] == 3
        [vol] = doc["volumes"]
        assert vol["label"] == label and vol["staged_for"] == "Bank_Box"

    def test_stdout_is_only_json_even_with_warnings(self, tmp_path, capsys):
        cfg, _db = _setup(tmp_path, extra="tpyo = 1\n")
        rc = main(["--config", str(cfg), "status", "--json"])
        captured = capsys.readouterr()
        assert rc == 0
        json.loads(captured.out)
        assert "Unknown [defaults] keys" in captured.err

    def test_text_output_is_unchanged(self, tmp_path, capsys):
        cfg, _db = _setup(tmp_path)
        assert main(["--config", str(cfg), "status"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("Packs: 3 total")
        assert "{" not in out

    def test_json_with_report_flags_is_refused(self, tmp_path, capsys):
        cfg, _db = _setup(tmp_path)
        assert main(["--config", str(cfg), "status", "--json",
                     "--redundancy"]) == 1


# --- repo list ---------------------------------------------------------------


def test_repo_list_shows_sources(tmp_path, capsys):
    cfg, _db = _setup(tmp_path)
    assert main(["--config", str(cfg), "repo", "list"]) == 0
    out = capsys.readouterr().out
    assert "sources: /srv/photos, /srv/letters" in out
    assert out.count("sources:") == 1
