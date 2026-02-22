"""Tests for remaining coverage gaps in parser, staging, and labels."""

from __future__ import annotations

from lcsas.db.models import Pack
from lcsas.rustic.parser import (
    parse_backup_output,
    parse_prune_output,
    parse_restore_plan_output,
    parse_snapshots_output,
)
from lcsas.staging.builder import StagingBuilder
from lcsas.utils.labels import generate_volume_label, next_seq_num

# =========================================================================
# rustic/parser.py — remaining branches
# =========================================================================


class TestParserEdgeCases:
    def test_backup_no_snapshot_id_field(self):
        """Dict without any snapshot ID field returns 'unknown'."""
        result = parse_backup_output('{"status": "ok"}')
        assert result.snapshot_id == "unknown"

    def test_backup_empty_output(self):
        result = parse_backup_output("")
        assert result.snapshot_id == "unknown"

    def test_backup_multiline_with_summary_last(self):
        """Parser searches lines in reverse to find the summary."""
        output = '{"status": "scanning"}\n{"snapshot_id": "abc123", "files_new": 10}'
        result = parse_backup_output(output)
        assert result.snapshot_id == "abc123"

    def test_snapshots_single_object_not_array(self):
        """Parser wraps single dict in a list."""
        output = '{"id": "snap1", "time": "2026-01-01", "hostname": "box", "paths": [], "tags": []}'
        result = parse_snapshots_output(output)
        assert len(result) == 1
        assert result[0].snapshot_id == "snap1"

    def test_snapshots_non_dict_items_skipped(self):
        """Non-dict items in the list are skipped."""
        output = (
            '[{"id": "s1", "time": "", "hostname": "",'
            ' "paths": [], "tags": []}, null, "string"]'
        )
        result = parse_snapshots_output(output)
        assert len(result) == 1

    def test_snapshots_invalid_json(self):
        result = parse_snapshots_output("not json")
        assert result == []

    def test_restore_plan_pack_ids_key(self):
        """Alternative 'pack_ids' key for pack hashes."""
        output = '{"pack_ids": ["p1", "p2"], "total_size": 1024, "file_count": 5}'
        result = parse_restore_plan_output("snap1", output)
        assert result.required_pack_hashes == ["p1", "p2"]

    def test_restore_plan_non_dict_json(self):
        """Non-dict JSON returns default RestorePlan."""
        result = parse_restore_plan_output("snap1", "[1, 2, 3]")
        assert result.required_pack_hashes == []

    def test_restore_plan_invalid_json(self):
        result = parse_restore_plan_output("snap1", "bad json")
        assert result.snapshot_id == "snap1"
        assert result.required_pack_hashes == []

    def test_restore_plan_packs_not_list(self):
        """When packs value is not a list, falls back to empty list."""
        output = '{"packs": "not_a_list"}'
        result = parse_restore_plan_output("snap1", output)
        assert result.required_pack_hashes == []

    def test_prune_non_dict_json(self):
        """Non-dict JSON returns default PruneResult."""
        result = parse_prune_output("[1, 2, 3]")
        assert result.packs_to_delete == []

    def test_prune_invalid_json(self):
        result = parse_prune_output("bad")
        assert result.packs_to_delete == []


# =========================================================================
# staging/builder.py — edge cases
# =========================================================================


class TestStagingEdgeCases:
    def _make_pack(self, sha: str, size: int = 100) -> Pack:
        return Pack(
            pack_id=1, sha256=sha, size_bytes=size,
            repo_id="test", is_pruned=False, created_at="2026-01-01",
        )

    def test_stage_pack_already_staged(self, tmp_path):
        """Pack already in staging dir is not re-hardlinked but still counted."""
        builder = StagingBuilder(tmp_path / "staging")
        builder.initialize()

        sha = "a" * 64
        mirror_data = tmp_path / "mirror_data"
        mirror_data.mkdir()
        (mirror_data / sha).write_bytes(b"pack_data")

        # Pre-place the pack in staging
        (builder.data_dir / sha).write_bytes(b"already_staged")

        count = builder.stage_packs([self._make_pack(sha)], mirror_data)
        assert count == 1  # still counted
        # Original content preserved (not overwritten)
        assert (builder.data_dir / sha).read_bytes() == b"already_staged"

    def test_stage_without_initialize(self, tmp_path):
        """stage_packs creates data dir via ensure_dir even without initialize()."""
        builder = StagingBuilder(tmp_path / "staging2")
        # Skip initialize() deliberately

        sha = "b" * 64
        mirror_data = tmp_path / "mirror_data"
        mirror_data.mkdir()
        (mirror_data / sha).write_bytes(b"data")

        count = builder.stage_packs([self._make_pack(sha)], mirror_data)
        assert count == 1
        # Two-level layout: data/<prefix>/<hash>
        assert (builder.data_dir / sha[:2] / sha).exists()

    def test_cleanup(self, tmp_path):
        """Cleanup removes entire staging tree."""
        builder = StagingBuilder(tmp_path / "staging")
        builder.initialize()
        (builder.data_dir / "test.txt").write_bytes(b"x")
        builder.cleanup()
        assert not builder.root.exists()


# =========================================================================
# utils/labels.py — edge cases
# =========================================================================


class TestLabelsEdgeCases:
    def test_next_seq_num_malformed_labels(self):
        """Labels with non-integer suffix are skipped."""
        labels = ["LCSAS_BD_2026_abc", "LCSAS_BD_2026_002"]
        assert next_seq_num(labels, "LCSAS") == 3

    def test_next_seq_num_too_few_parts(self):
        """Labels with fewer than 4 parts are skipped."""
        labels = ["SHORT_LABEL", "AB"]
        assert next_seq_num(labels, "LCSAS") == 1

    def test_next_seq_num_wrong_prefix(self):
        """Labels with different prefix are ignored."""
        labels = ["OTHER_BD_2026_005"]
        assert next_seq_num(labels, "LCSAS") == 1

    def test_next_seq_num_empty_list(self):
        assert next_seq_num([], "LCSAS") == 1

    def test_next_seq_num_mixed(self):
        labels = [
            "LCSAS_BD_2026_003",
            "LCSAS_BD_2026_001",
            "WRONG_BD_2026_999",
            "LCSAS_MD_2026_bad",
        ]
        assert next_seq_num(labels, "LCSAS") == 4

    def test_generate_volume_label_large_seq(self):
        """Sequence > 999 produces wider number (no crash)."""
        label = generate_volume_label("LCSAS", "BD", 1001)
        assert "1001" in label

    def test_generate_volume_label_media_shortening(self):
        """MDISC and BDXL get shortened."""
        label = generate_volume_label("LCSAS", "MDISC100", 1)
        assert "MD100" in label
        label2 = generate_volume_label("LCSAS", "BDXL100", 1)
        assert "BX100" in label2
