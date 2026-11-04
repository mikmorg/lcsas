"""windows-e2e's PowerShell, parsed and partly run off Windows (#606, #617).

The windows-e2e legs are PowerShell (tests/e2e/windows_e2e_lib.ps1 and the
``shell: pwsh`` steps of .github/workflows/windows-e2e.yml), and they run
only on a hand-dispatched Windows runner.  A syntax error in the lib, which
every leg dot-sources, fails every leg at once and is only seen there.
pwsh runs on Linux too (GitHub's ubuntu runners ship it), so this:

* parses the lib and every ``shell: pwsh`` step with PowerShell's own
  parser, and
* runs the helpers that are plain text work: Get-FixtureSets reading a
  sets.json as the fixture writes it, Assert-SetNamed on restore.bat's
  "Backup set:" line, and Invoke-SetMenu reading restore.bat's set menu and
  typing the number of the set to restore.

The menu and banner text below are restore.bat's (``:menu``,
``:menu_line`` and the "Backup set:" echo).  Skipped where pwsh is absent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lcsas.staging.repo_marker import cmd_name

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "tests" / "e2e" / "windows_e2e_lib.ps1"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "windows-e2e.yml"
BAT = REPO_ROOT / "recovery" / "scripts" / "restore.bat"

sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))
import fixture_lib  # noqa: E402

PWSH = shutil.which("pwsh")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(PWSH is None, reason="pwsh not installed"),
]

# Invented ids (the fixture's are fresh UUIDs every build).
RESTORE_ID = "4c8e2f61-0b7d-4a93-9e15-d26a8b3f70c4"
OTHER_ID = "9a05d3be-7c42-4e18-b6f9-13e0c7a52d8b"


def _pwsh_steps(text: str) -> list[tuple[str, str]]:
    """(step name, script) for every ``shell: pwsh`` step's ``run: |``
    block.  No YAML library: the project's tests use the standard
    library only, and the workflow's layout is regular."""
    lines = text.splitlines()
    steps: list[tuple[str, str]] = []
    for i, line in enumerate(lines):
        m = re.match(r"^( +)run: \|\s*$", line)
        if not m:
            continue
        ind = len(m.group(1))
        start = i
        while start > 0 and not lines[start].startswith(" " * (ind - 2) + "- "):
            start -= 1
        body: list[str] = []
        j = i + 1
        while j < len(lines) and (
            not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > ind
        ):
            body.append(lines[j])
            j += 1
        keys = [
            ln for ln in lines[start:j]
            if len(ln) - len(ln.lstrip()) == ind or ln.startswith(" " * (ind - 2) + "- ")
        ]
        if any(k.strip() == "shell: pwsh" for k in keys):
            name = re.sub(r"^\s*- name:\s*", "", lines[start]).strip()
            steps.append((name, textwrap.dedent("\n".join(body)).strip() + "\n"))
    return steps


def _run_pwsh(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert PWSH is not None
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script), *args],
        capture_output=True, text=True, timeout=120,
    )


_PARSE = r"""
$bad = @{}
foreach ($p in $args) {
    $tokens = $null; $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$tokens, [ref]$errors)
    if ($errors.Count -gt 0) {
        $bad[$p] = @($errors | ForEach-Object {
            "line $($_.Extent.StartLineNumber): $($_.Message)"
        })
    }
}
$bad | ConvertTo-Json -Depth 4 -Compress
"""


def test_the_lib_and_every_pwsh_step_parse(tmp_path: Path) -> None:
    steps = _pwsh_steps(WORKFLOW.read_text(encoding="utf-8"))
    # The workflow has eleven today; an extractor that finds none would
    # prove nothing.
    assert len(steps) >= 10, [n for n, _ in steps]
    assert any("Invoke-SingleDriveLeg" in s for _, s in steps)
    files = [str(LIB)]
    for n, (_name, script) in enumerate(steps):
        f = tmp_path / f"step{n:02d}.ps1"
        f.write_text(script, encoding="utf-8")
        files.append(str(f))
    driver = tmp_path / "parse.ps1"
    driver.write_text(_PARSE, encoding="utf-8")
    res = _run_pwsh(driver, *files)
    assert res.returncode == 0, res.stderr
    bad = json.loads(res.stdout.strip() or "{}")
    named = {
        (LIB.name if p == str(LIB) else steps[int(Path(p).stem[4:])][0]): errs
        for p, errs in bad.items()
    }
    assert named == {}


def test_the_parse_check_catches_a_syntax_error(tmp_path: Path) -> None:
    """The check above is not vacuous."""
    broken = tmp_path / "broken.ps1"
    broken.write_text("function Foo {\n  if ($x -eq 1 {\n}\n", encoding="utf-8")
    driver = tmp_path / "parse.ps1"
    driver.write_text(_PARSE, encoding="utf-8")
    res = _run_pwsh(driver, str(broken))
    assert res.returncode == 0, res.stderr
    assert str(broken) in json.loads(res.stdout)


# ---------------------------------------------------------------------------
# The helpers, run
# ---------------------------------------------------------------------------

_HELPERS = r"""
param([string]$Lib, [string]$Fixture, [string]$Work)
$ErrorActionPreference = 'Stop'
. $Lib
$r = [ordered]@{}
$sets = Get-FixtureSets -Fixture $Fixture
$r.name = $sets.restore.name
$r.name_cmd = $sets.restore.name_cmd
$r.all = @(Get-AllSets $sets | ForEach-Object { $_.id })
$other = @($sets.others)[0]
$crlf = "`r`n"

function Get-NamedProblems([string]$Out) {
    $p = New-Object 'System.Collections.Generic.List[string]'
    Assert-SetNamed -Out $Out -Sets $sets -Problems $p
    return ,([string[]]@($p))
}
$good = "Backup set:    $($sets.restore.name_cmd)$crlf" +
        "Repo:          X:\metadata\$($sets.restore.id)$crlf"
$r.named_good = Get-NamedProblems $good
$r.named_by_id = Get-NamedProblems ("Backup set:    $($sets.restore.id)$crlf")
$r.named_missing = Get-NamedProblems ("Restore complete.$crlf")
$r.menu_shows_id = Get-NamedProblems ($good + "   [2] $($other.id)$crlf")

function Invoke-Menu([string[]]$Labels) {
    $t = $crlf + 'This archive contains more than one backup set:' + $crlf
    $i = 0
    foreach ($l in $Labels) { $i++; $t += "   [$i] $l$crlf" }
    $t += $crlf + 'Type the number of the one to restore, or Q then ENTER to quit.' + $crlf
    $t += 'Which one do you want to restore? [1]: '
    $outFile = Join-Path $Work ("menu-" + [guid]::NewGuid() + ".txt")
    [IO.File]::WriteAllText($outFile, $t)
    # A stand-in for restore.bat: cat echoes what the leg types.
    $psi = [System.Diagnostics.ProcessStartInfo]::new('cat')
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.UseShellExecute = $false
    $proc = [System.Diagnostics.Process]::Start($psi)
    $p = New-Object 'System.Collections.Generic.List[string]'
    $err = ''
    try { Invoke-SetMenu -Proc $proc -OutFile $outFile -Sets $sets -Problems $p }
    catch { $err = "$_" }
    $proc.StandardInput.Close()
    $typed = $proc.StandardOutput.ReadToEnd()
    $proc.WaitForExit()
    return [ordered]@{ typed = $typed.Trim(); problems = [string[]]@($p); error = $err }
}
$r.menu_second = Invoke-Menu @($other.name_cmd, $sets.restore.name_cmd)
$r.menu_first = Invoke-Menu @($sets.restore.name_cmd, $other.name_cmd)
$r.menu_ids = Invoke-Menu @($other.id, $sets.restore.id)
$json = $r | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText((Join-Path $Work 'result.json'), $json, [Text.UTF8Encoding]::new($false))
"""


@pytest.fixture(scope="module")
def helpers(tmp_path_factory: pytest.TempPathFactory) -> dict:
    work = tmp_path_factory.mktemp("pwsh-helpers")
    fixture = work / "fixture"
    fixture.mkdir()
    # sets.json as build_windows_fixture writes it (ASCII JSON, \u escapes).
    restore = fixture_lib.FixtureSet(
        RESTORE_ID, fixture_lib.WINDOWS_E2E_SET_NAME,
        cmd_name(fixture_lib.WINDOWS_E2E_SET_NAME, RESTORE_ID),
    )
    other = fixture_lib.FixtureSet(
        OTHER_ID, fixture_lib.WINDOWS_E2E_OTHER_SET_NAMES[0],
        cmd_name(fixture_lib.WINDOWS_E2E_OTHER_SET_NAMES[0], OTHER_ID),
    )
    (fixture / "sets.json").write_text(json.dumps({
        "restore": restore.as_json(),
        "others": [other.as_json()],
        "restore_discs": {},
    }, indent=2) + "\n", encoding="ascii")
    driver = work / "helpers.ps1"
    driver.write_text(_HELPERS, encoding="utf-8")
    res = _run_pwsh(driver, "-Lib", str(LIB), "-Fixture", str(fixture), "-Work", str(work))
    assert res.returncode == 0, res.stdout + res.stderr
    return json.loads((work / "result.json").read_text(encoding="utf-8"))


def test_get_fixture_sets_reads_the_fixtures_sets_json(helpers: dict) -> None:
    assert helpers["name"] == fixture_lib.WINDOWS_E2E_SET_NAME
    assert helpers["name_cmd"] == "Fotos da fam_lia"
    assert helpers["all"] == [RESTORE_ID, OTHER_ID]


def test_restore_bat_still_prints_the_texts_the_legs_wait_for() -> None:
    """The texts the helpers below are fed are restore.bat's own."""
    bat = BAT.read_text(encoding="utf-8", errors="replace")
    assert "echo Backup set:    !SET_NAME!" in bat
    assert "echo This archive contains more than one backup set:" in bat
    assert 'set /p "REPO_PICK=Which one do you want to restore? [1]: "' in bat
    assert "echo    [%~1] !CAND_LABEL_%~1!" in bat


def test_assert_set_named_passes_the_name_and_flags_the_id(helpers: dict) -> None:
    assert helpers["named_good"] == []
    assert any("did not show the set's name" in p for p in helpers["named_missing"])
    assert any(
        f"showed the id {RESTORE_ID}" in p for p in helpers["named_by_id"]
    ), helpers["named_by_id"]
    assert any(
        f"showed the id {OTHER_ID}" in p for p in helpers["menu_shows_id"]
    ), helpers["menu_shows_id"]


@pytest.mark.parametrize(("case", "number"), [("menu_first", "1"), ("menu_second", "2")])
def test_invoke_set_menu_types_the_number_restore_bat_shows(
    helpers: dict, case: str, number: str,
) -> None:
    got = helpers[case]
    assert got["error"] == ""
    assert got["problems"] == []
    assert got["typed"] == number


def test_invoke_set_menu_fails_a_menu_of_ids(helpers: dict) -> None:
    got = helpers["menu_ids"]
    assert got["typed"] == ""
    assert "the set menu has no 'Fotos da fam_lia'" in got["error"]
    assert any(f"lists the id {RESTORE_ID}" in p for p in got["problems"]), got
    assert any("does not list 'Work documents'" in p for p in got["problems"]), got
