"""Hardening: a quiet spell can't switch off the scheduled canaries (#462).

GitHub disables a workflow file that has a ``schedule:`` trigger after 60
days without repository activity (public repos).  Master was quiet
2026-08-05 -> 10-15 and seven canaries went dark without a sound.  The
#463 split stopped that from taking PR checks with it, and test.yml's
workflow-state check makes the first push afterwards fail loudly, but the
canaries were still off for the whole spell.

keepalive.yml closes that gap: weekly, it pushes one empty commit to master
once master has gone quiet for a while.  These tests pin the parts that
make it work:

1. It exists and is scheduled.
2. Its threshold leaves at least two weekly retries before day 60.
3. It pushes with the KEEPALIVE_TOKEN secret, never GITHUB_TOKEN.  Master's
   required checks block a GITHUB_TOKEN push, and a GITHUB_TOKEN push
   starts no workflows, so test.yml's state check would not run on it.
4. It proves the token can push on every run, not only when a commit is
   due, so an expired token goes red weeks before the cutoff.

Text-level, YAML-free parsing on purpose (pattern of
test_schedule_only_workflows.py): the suite is stdlib-only.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
KEEPALIVE = REPO_ROOT / ".github" / "workflows" / "keepalive.yml"

# GitHub's inactivity limit, and the weekly schedule's spacing.
_INACTIVITY_DAYS = 60
_SCHEDULE_SPACING_DAYS = 7


def _code() -> str:
    """keepalive.yml with comment lines removed, so a comment that names a
    pattern can't satisfy a check that the code itself fails."""
    assert KEEPALIVE.is_file(), (
        "keepalive.yml is gone.  Without it nothing restarts GitHub's "
        "60-day inactivity clock, and a quiet spell switches off every "
        "scheduled canary with nothing red anywhere (#462)."
    )
    text = KEEPALIVE.read_text(encoding="utf-8")
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )


def test_keepalive_is_scheduled_weekly() -> None:
    code = _code()
    crons = re.findall(r"""^\s+- cron:\s*["']([^"']+)["']""", code, re.M)
    assert len(crons) == 1, f"expected one cron in keepalive.yml, found {crons}"
    fields = crons[0].split()
    assert len(fields) == 5, f"keepalive cron {crons[0]!r} is not 5 fields"
    _minute, _hour, dom, month, dow = fields
    assert dom == "*" and month == "*" and re.fullmatch(r"[0-6]", dow), (
        f"keepalive cron {crons[0]!r} is not one fixed weekday.  The "
        "threshold test below assumes a run every "
        f"{_SCHEDULE_SPACING_DAYS} days."
    )


def test_threshold_leaves_retries_before_the_cutoff() -> None:
    m = re.search(r'^\s+KEEPALIVE_AFTER_DAYS:\s*"(\d+)"\s*$', _code(), re.M)
    assert m, "keepalive.yml has no KEEPALIVE_AFTER_DAYS -- parser broken?"
    days = int(m.group(1))
    assert days > 0
    # A weekly run can fire up to a week after the threshold passes; one
    # more failed or dropped run must still leave a try before day 60.
    assert days + 2 * _SCHEDULE_SPACING_DAYS < _INACTIVITY_DAYS, (
        f"KEEPALIVE_AFTER_DAYS={days} leaves no room for a failed or "
        f"dropped weekly run before GitHub's {_INACTIVITY_DAYS}-day cutoff."
    )


def test_pushes_with_the_token_secret() -> None:
    code = _code()
    assert re.search(
        r"^\s+token:\s*\$\{\{\s*secrets\.KEEPALIVE_TOKEN\s*\}\}\s*$", code, re.M
    ), (
        "keepalive.yml's checkout no longer uses secrets.KEEPALIVE_TOKEN, so "
        "its push runs as GITHUB_TOKEN.  Master's required status checks "
        "reject that push, and a GITHUB_TOKEN push starts no workflows."
    )
    assert "github.token" not in code.split("canary-watchdog:")[0], (
        "the keepalive job references github.token; its push must use "
        "KEEPALIVE_TOKEN (only canary-watchdog files issues with "
        "github.token)."
    )
    assert re.search(r'^\s+git push origin "HEAD:refs/heads/\$BRANCH"', code, re.M), (
        "keepalive.yml no longer pushes HEAD to the branch -- parser broken, "
        "or the push was removed?"
    )


def test_checks_the_token_on_every_run() -> None:
    code = _code()
    dry = code.find("git push --dry-run")
    gate = code.find('KEEPALIVE_AFTER_DAYS" ]')
    assert dry != -1, (
        "keepalive.yml no longer dry-run pushes on every run.  An expired "
        "token would then go unnoticed until master had been quiet for "
        "KEEPALIVE_AFTER_DAYS, leaving little time before the cutoff."
    )
    assert gate != -1, "age check not found -- parser broken?"
    assert dry < gate, (
        "the token check must run before the age check exits early, or it "
        "only runs when a commit is due."
    )
