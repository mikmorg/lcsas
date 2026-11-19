"""``lcsas burn`` refuses a volume no new disc of which is burned (#905).

A CONSOLIDATING, DEPRECATED (not lost) or DESTROYED volume used to be
refused only by the status graph inside the burn, after the disc prompt,
and ``lcsas burn`` printed that bare ``ValueError`` as "Unexpected
error".  The burn now asks ``VolumeLifecycle.may_burn`` before the first
disc prompt and refuses with a one-line reason, exit 1.
"""

from __future__ import annotations

import pytest

from lcsas.cli.main import main
from lcsas.db.lifecycle import VolumeLifecycle
from lcsas.exceptions import BurnError
from tests.unit.test_burn_keeps_status import _burn, _status, one  # noqa: F401
from tests.unit.volume_status import force_status


@pytest.mark.parametrize("status", ["CONSOLIDATING", "DEPRECATED",
                                    "DESTROYED"])
def test_burn_refuses_before_the_disc_prompt(one, status):  # noqa: F811
    force_status(one["conn"], one["volume_id"], status)
    one["conn"].commit()

    with pytest.raises(BurnError, match=(
            f"^Cannot burn volume {one['label']}: it is {status}; .* "
            f"Nothing was burned.$")):
        _burn(one)

    assert one["prompt"].messages == []
    one["xorriso"].burn_iso.assert_not_called()
    assert _status(one) == status


@pytest.mark.parametrize("status", ["STAGING", "BURNED", "VERIFIED"])
def test_may_burn_allows_a_burnable_volume(one, status):  # noqa: F811
    force_status(one["conn"], one["volume_id"], status)
    assert VolumeLifecycle(one["conn"]).may_burn(one["volume_id"]) is None


def test_cli_prints_one_line_and_exits_1(one, caplog):  # noqa: F811
    force_status(one["conn"], one["volume_id"], "CONSOLIDATING")
    one["conn"].commit()
    caplog.clear()

    # Any existing path passes the device check.
    rc = main(["--config", one["cfg"], "burn", "--session",
               one["session_id"], "--location", "Home",
               "--device", one["db"]])

    assert rc == 1
    assert "Unexpected error" not in caplog.text
    errors = [r.getMessage() for r in caplog.records
              if r.levelname == "ERROR"]
    assert errors == [
        f"Cannot burn volume {one['label']}: it is CONSOLIDATING; its packs "
        f"are being consolidated onto a new volume; burn that session and "
        f"run 'lcsas consolidate --deprecate', or 'lcsas session abort' it "
        f"to make this volume VERIFIED again. Nothing was burned."]
