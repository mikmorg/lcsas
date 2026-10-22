"""Tests for Rustic JSON output parsers."""

from __future__ import annotations

import json

import pytest

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
        import pytest
        with pytest.raises(ValueError, match="Could not extract snapshot_id"):
            parse_backup_output("not json at all")

    def test_parse_empty(self):
        import pytest
        with pytest.raises(ValueError, match="Could not extract snapshot_id"):
            parse_backup_output("")


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

    # rustic 0.11.2 `snapshots --json`, trimmed: grouped by host/label/paths.
    _RUSTIC_GROUPED = json.dumps([
        {
            "group_key": {"hostname": "vm-desk1", "label": "", "paths": ["/data"]},
            "snapshots": [
                {"time": "2026-10-21T01:58:53.8+00:00", "tree": "t1",
                 "paths": ["/data"], "hostname": "vm-desk1", "tags": [],
                 "id": "a" * 64},
                {"time": "2026-10-21T02:10:00.0+00:00", "tree": "t2",
                 "paths": ["/data"], "hostname": "vm-desk1", "tags": ["x"],
                 "id": "b" * 64},
            ],
        },
        {
            "group_key": {"hostname": "laptop", "label": "", "paths": ["/home"]},
            "snapshots": [{"time": "2026-10-20T00:00:00+00:00", "id": "c" * 64}],
        },
    ])

    def test_rustic_grouped_output_is_flattened(self):
        """#536: each group is not a snapshot; its members are."""
        result = parse_snapshots_output(self._RUSTIC_GROUPED)
        assert [s.snapshot_id for s in result] == ["a" * 64, "b" * 64, "c" * 64]
        assert result[1].tags == ["x"]
        assert result[0].timestamp == "2026-10-21T01:58:53.8+00:00"

    def test_group_key_fills_in_missing_hostname_and_paths(self):
        third = parse_snapshots_output(self._RUSTIC_GROUPED)[2]
        assert third.hostname == "laptop"
        assert third.paths == ["/home"]

    def test_older_rustic_pair_form_is_flattened(self):
        output = json.dumps([[{"hostname": "h", "paths": ["/p"]},
                              [{"id": "d" * 64, "time": "t"}]]])
        (snap,) = parse_snapshots_output(output)
        assert (snap.snapshot_id, snap.hostname, snap.paths) == ("d" * 64, "h", ["/p"])

    def test_entries_without_an_id_are_dropped_with_a_warning(self, caplog):
        output = json.dumps([{"time": "t"}, {"id": "e" * 64, "time": "t"}])
        with caplog.at_level("WARNING"):
            result = parse_snapshots_output(output)
        assert [s.snapshot_id for s in result] == ["e" * 64]
        assert "no snapshot id" in caplog.text

    def test_no_result_ever_has_an_empty_id(self):
        output = json.dumps([{"group_key": {}, "snapshots": []}, {}, 3, "x", None])
        assert parse_snapshots_output(output) == []


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
        with pytest.raises(ValueError, match="failed to parse JSON"):
            parse_restore_plan_output("snap_z", "nope")


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
