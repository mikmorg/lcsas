"""Tests for Rustic JSON output parsers."""

from __future__ import annotations

import json

from lcsas.rustic.parser import (
    parse_backup_output,
    parse_prune_output,
    parse_restore_plan_output,
    parse_snapshots_output,
)


class TestParseBackup:
    def test_parse_summary(self):
        output = json.dumps({
            "snapshot_id": "abc123def",
            "files_new": 10,
            "files_changed": 2,
            "files_unmodified": 100,
            "data_added": 5000,
            "total_duration": 1.5,
        })
        result = parse_backup_output(output)
        assert result.snapshot_id == "abc123def"
        assert result.files_new == 10
        assert result.data_added_bytes == 5000

    def test_parse_multiline(self):
        """Rustic may emit progress lines before the summary."""
        lines = [
            json.dumps({"message_type": "status", "percent_done": 0.5}),
            json.dumps({"snapshot_id": "final_snap", "files_new": 5}),
        ]
        output = "\n".join(lines)
        result = parse_backup_output(output)
        assert result.snapshot_id == "final_snap"

    def test_parse_unparseable(self):
        result = parse_backup_output("not json at all")
        assert result.snapshot_id == "unknown"

    def test_parse_empty(self):
        result = parse_backup_output("")
        assert result.snapshot_id == "unknown"


class TestParseSnapshots:
    def test_parse_list(self):
        output = json.dumps([
            {"id": "snap1", "time": "2026-01-01T00:00:00Z",
             "hostname": "server", "paths": ["/data"], "tags": ["daily"]},
            {"id": "snap2", "time": "2026-01-02T00:00:00Z",
             "hostname": "server", "paths": ["/data"]},
        ])
        result = parse_snapshots_output(output)
        assert len(result) == 2
        assert result[0].snapshot_id == "snap1"
        assert result[0].tags == ["daily"]
        assert result[1].tags == []

    def test_parse_empty(self):
        assert parse_snapshots_output("[]") == []

    def test_parse_invalid(self):
        assert parse_snapshots_output("broken") == []


class TestParseRestorePlan:
    def test_parse_with_packs(self):
        output = json.dumps({
            "packs": ["hash1", "hash2", "hash3"],
            "total_size": 1500,
            "file_count": 10,
        })
        result = parse_restore_plan_output("snap_x", output)
        assert result.snapshot_id == "snap_x"
        assert len(result.required_pack_hashes) == 3
        assert result.total_size_bytes == 1500

    def test_parse_empty(self):
        result = parse_restore_plan_output("snap_y", "{}")
        assert result.required_pack_hashes == []

    def test_parse_invalid(self):
        result = parse_restore_plan_output("snap_z", "nope")
        assert result.required_pack_hashes == []


class TestParsePrune:
    def test_parse_prune(self):
        output = json.dumps({
            "packs_to_delete": ["dead1", "dead2"],
            "packs_to_repack": ["repack1"],
            "space_freed": 50000,
        })
        result = parse_prune_output(output)
        assert len(result.packs_to_delete) == 2
        assert result.space_freed_bytes == 50000

    def test_parse_empty(self):
        result = parse_prune_output("{}")
        assert result.packs_to_delete == []
