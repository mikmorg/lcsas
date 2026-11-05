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
  "Backup set:" line, and Invoke-SetMenu reading restore.bat's set menu,
  with the split of a split set (#607), and typing the number of the set
  to restore;
* runs the helpers of the legs added for #604, #607 and #608: the C8
  swap and cache-copy counts, the check of the pack cache a stop leaves
  (each pack whole: its SHA-256 is its name), reading a printed share
  card and finding
  three of its words in a row in a folder, and finding the PowerShell
  lines of RECOVER_WINDOWS.txt "THE PASSWORD" (failing loudly when they
  are gone) and making them into a script that runs with no keyboard.
  That script is run here under pwsh 7; windows-e2e runs it under
  Windows PowerShell 5.1, which only Windows has.

The menu and banner text below are restore.bat's (``:menu``,
``:menu_line`` and the "Backup set:" echo).  Skipped where pwsh is absent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lcsas.cli.main import _share_card_text
from lcsas.keyshare import encode_master_secret, split_secret
from lcsas.staging.repo_marker import cmd_name

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "tests" / "e2e" / "windows_e2e_lib.ps1"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "windows-e2e.yml"
BAT = REPO_ROOT / "recovery" / "scripts" / "restore.bat"
DOC = REPO_ROOT / "recovery" / "docs" / "RECOVER_WINDOWS.txt"

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
# The other set is split 2-of-3, as in the fixture (#607).  An invented
# password; its cards are made fresh by each run of this module.
OTHER_PASSWORD = b"invented-work-pass-5813"
OTHER_SPLIT_ID = 4321


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
    # The workflow has thirteen today; an extractor that finds none would
    # prove nothing.
    assert len(steps) >= 13, [n for n, _ in steps]
    for leg in ("Invoke-SingleDriveLeg", "Invoke-StopAndResumeLeg",
                "Invoke-HiddenPasswordLeg", "Invoke-SplitCardsLeg"):
        assert any(leg in s for _, s in steps), leg
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
param([string]$Lib, [string]$Fixture, [string]$Work, [string]$DocFile)
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

function Invoke-Menu([string[]]$Labels, $Choose = $null) {
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
    try { Invoke-SetMenu -Proc $proc -OutFile $outFile -Sets $sets -Problems $p -Choose $Choose }
    catch { $err = "$_" }
    $proc.StandardInput.Close()
    $typed = $proc.StandardOutput.ReadToEnd()
    $proc.WaitForExit()
    return [ordered]@{ typed = $typed.Trim(); problems = [string[]]@($p); error = $err }
}
# restore.bat's menu line for a set: a split set's ends with its split.
function Label($s) {
    if ($s.split -and $s.split -ne 'none') {
        $k, $n = $s.split -split '-of-'
        return "$($s.name_cmd) (password split: any $k of $n share cards)"
    }
    return $s.name_cmd
}
$r.menu_second = Invoke-Menu @((Label $other), $sets.restore.name_cmd)
$r.menu_first = Invoke-Menu @($sets.restore.name_cmd, (Label $other))
$r.menu_ids = Invoke-Menu @($other.id, $sets.restore.id)
# #607: the split set chosen; its split missing or wrong on the menu.
$r.menu_choose_split = Invoke-Menu @($sets.restore.name_cmd, (Label $other)) -Choose $other
$r.menu_split_missing = Invoke-Menu @($sets.restore.name_cmd, $other.name_cmd)
$r.menu_split_wrong = Invoke-Menu @(
    $sets.restore.name_cmd,
    "$($other.name_cmd) (password split: any 3 of 5 share cards)")
$r.menu_split_on_unsplit = Invoke-Menu @(
    "$($sets.restore.name_cmd) (password split: any 2 of 3 share cards)",
    (Label $other))

$split = Get-SplitSet $sets
$r.split_set = $split.id
$p = New-Object 'System.Collections.Generic.List[string]'
Assert-SetNamed -Out "Backup set:    $($other.name_cmd)$crlf" -Sets $sets -Problems $p -Set $other
$r.named_other = [string[]]@($p)

# Share cards: the words of a printed card, and three in a row found.
$r.card1 = Get-CardWords -Path (Join-Path $Fixture @($split.cards)[0])
$r.card3 = Get-CardWords -Path (Join-Path $Fixture @($split.cards)[2])
$r.not_a_card = ''
try { Get-CardWords -Path (Join-Path $Fixture 'sets.json') | Out-Null }
catch { $r.not_a_card = "$_" }
$runs = Get-WordRuns -Cards @($r.card1, $r.card3)
$r.runs = @($runs).Count
$scan = Join-Path $Work 'scan'
New-Item -ItemType Directory -Force -Path (Join-Path $scan 'deep') | Out-Null
$w1 = @($r.card1 -split ' ')
[IO.File]::WriteAllText((Join-Path $scan 'deep/kept.txt'),
    "junk $($w1[5]) $($w1[6]) $($w1[7]) junk")
[IO.File]::WriteAllText((Join-Path $scan 'words.txt'),
    "$($w1[0]) and $($w1[1]) or $($w1[2])")
$r.found = @(Find-CardText -Dir $scan -Runs $runs |
             ForEach-Object { $_.Substring($scan.Length + 1) })

# C8: swaps <= discs, no disc in twice; each disc copied once.
$isos = @(1, 2, 3 | ForEach-Object { "A/LCSAS_TEST_TINY_2026_000$_.iso" })
function Get-Swaps([string[]]$Inserted) {
    $p = New-Object 'System.Collections.Generic.List[string]'
    Assert-SwapsWithinDiscs -Inserted $Inserted -DataIsos $isos -What 'here' -Problems $p
    return ,([string[]]@($p))
}
$r.swaps_ok = Get-Swaps @($isos[2], $isos[0], $isos[1])
$r.swaps_twice = Get-Swaps @($isos[2], $isos[0], $isos[2])
$r.swaps_more = Get-Swaps @($isos[2], $isos[0], $isos[1], $isos[0])
function Get-Copied([string]$Out) {
    $p = New-Object 'System.Collections.Generic.List[string]'
    Assert-CopiedOnce -Out $Out -DataIsos $isos -Problems $p
    return ,([string[]]@($p))
}
$line = '[lcsas-restore] copying disc {0} to the cache: 2 MB.  From a disc ' +
        'this can take several minutes.' + $crlf
$once = (($isos | ForEach-Object { $line -f [IO.Path]::GetFileNameWithoutExtension($_) }) -join '')
$r.copied_once = Get-Copied $once
$r.copied_twice = Get-Copied ($once + ($line -f 'LCSAS_TEST_TINY_2026_0002'))
$r.copied_missing = Get-Copied ($line -f 'LCSAS_TEST_TINY_2026_0001')
# A long drain's progress lines are not a second copy (tier 1's
# drain_tick); a copy counted over two runs says so.
$progress = '[lcsas-restore] copying disc LCSAS_TEST_TINY_2026_0002 to the cache: ' +
            '16 of 24 MB' + $crlf
$r.copied_progress = Get-Copied ($once + $progress + $progress)
$p = New-Object 'System.Collections.Generic.List[string]'
$twice1 = $once + ($line -f 'LCSAS_TEST_TINY_2026_0001')
Assert-CopiedOnce -Out $twice1 -DataIsos @($isos[0], $isos[0]) -What 'over both runs' -Problems $p
$r.copied_what = [string[]]@($p)

# D item 6 (C8): the pack cache after a stop.  A pack is named by the
# SHA-256 of its bytes.
function New-Pack([string]$Cache, [string]$Text, [int]$Cut = 0) {
    $bytes = [Text.Encoding]::ASCII.GetBytes($Text)
    $hash = [Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    $sha = [BitConverter]::ToString($hash).Replace('-', '').ToLowerInvariant()
    $dir = Join-Path (Join-Path $Cache 'data') $sha.Substring(0, 2)
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    if ($Cut -gt 0) { $bytes = [byte[]]$bytes[0..($Cut - 1)] }
    [IO.File]::WriteAllBytes((Join-Path $dir $sha), $bytes)
    return $sha
}
function New-Cache([string]$Name) {
    $c = Join-Path $Work $Name
    New-Item -ItemType Directory -Force -Path $c | Out-Null
    # tier 1's catalog copy sits at the top of the cache: not a pack.
    [IO.File]::WriteAllText((Join-Path $c '.locator-catalog.db'), 'not a pack')
    return $c
}
function Get-Cache([string]$Cache, [hashtable]$Map) {
    $p = New-Object 'System.Collections.Generic.List[string]'
    $n = Assert-PackCacheWhole -Cache $Cache -Isos @($isos[0]) -PackMap $Map -Problems $p
    return [ordered]@{ n = $n; problems = [string[]]@($p) }
}
$c = New-Cache 'cache-good'
$one = New-Pack $c ('invented pack one ' * 40)
$two = New-Pack $c ('invented pack two ' * 40)
$r.cache_good = Get-Cache $c @{ $isos[0] = @($one, $two) }
$r.cache_missing = Get-Cache $c @{ $isos[0] = @($one, $two, ('f' * 64)) }
$c = New-Cache 'cache-short'
$one = New-Pack $c ('invented pack one ' * 40)
$cut = New-Pack $c ('invented pack two ' * 40) -Cut 100
$r.cache_short_name = $cut
$r.cache_short = Get-Cache $c @{ $isos[0] = @($one, $cut) }
$c = New-Cache 'cache-part'
$one = New-Pack $c ('invented pack one ' * 40)
$partDir = Join-Path (Join-Path $c 'data') $one.Substring(0, 2)
[IO.File]::WriteAllText((Join-Path $partDir "$('e' * 64).part"), 'half')
$r.cache_part = Get-Cache $c @{ $isos[0] = @($one) }
$r.cache_empty = Get-Cache (New-Cache 'cache-empty') @{ $isos[0] = @($one) }

# #608: the PowerShell lines of RECOVER_WINDOWS.txt, and the script made
# of them; a doc without them fails loudly.
$lines = Get-DocPasswordLines -DocPath $DocFile
$r.doc_lines = @($lines.Lines)
$r.doc_at = @($lines.ReadHost, $lines.PwFile, $lines.Restore)
New-DocPasswordScript -Doc $lines -Path (Join-Path $Work 'doc-password.ps1')
$text = [IO.File]::ReadAllText($DocFile)
$r.doc_errors = [ordered]@{}
$broken = [ordered]@{
    no_heading = $text.Replace("`nTHE PASSWORD`n", "`nTHE PASSWORDS`n")
    no_start = $text.Replace('$s = Read-Host -AsSecureString', '$s = Read-Host')
    no_end = $text.Replace("    Remove-Item `$f`n", "")
    no_bstr = $text.Replace('PtrToStringBSTR', 'PtrToStringAuto')
    no_utf8 = $text.Replace('Text.UTF8Encoding($false)', 'Text.UTF8Encoding($true)')
}
foreach ($k in $broken.Keys) {
    $f = Join-Path $Work "doc-$k.txt"
    [IO.File]::WriteAllText($f, $broken[$k], [Text.UTF8Encoding]::new($false))
    try { Get-DocPasswordLines -DocPath $f | Out-Null; $r.doc_errors[$k] = '' }
    catch { $r.doc_errors[$k] = "$_" }
}
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
    # The other set split 2-of-3, with its printed cards as `lcsas key
    # split` writes them (#607).
    other_name = fixture_lib.WINDOWS_E2E_OTHER_SET_NAMES[0]
    shares = split_secret(encode_master_secret(OTHER_PASSWORD), 2, 3)
    cards = []
    for i, share in enumerate(shares, 1):
        card = fixture / "cards" / f"{other_name}-share-{i}-card.txt"
        card.parent.mkdir(exist_ok=True)
        card.write_text(_share_card_text(
            other_name, i, 2, 3, share, "2026-11-04", OTHER_SPLIT_ID,
        ), encoding="utf-8")
        cards.append(card.relative_to(fixture).as_posix())
    other = fixture_lib.FixtureSet(
        OTHER_ID, other_name, cmd_name(other_name, OTHER_ID),
        manifest="manifest-other1.json", split=(2, 3),
        split_id=OTHER_SPLIT_ID, cards=tuple(cards),
    )
    (fixture / "sets.json").write_text(json.dumps({
        "restore": restore.as_json(),
        "others": [other.as_json()],
        "restore_discs": {},
    }, indent=2) + "\n", encoding="ascii")
    driver = work / "helpers.ps1"
    driver.write_text(_HELPERS, encoding="utf-8")
    res = _run_pwsh(driver, "-Lib", str(LIB), "-Fixture", str(fixture),
                    "-Work", str(work), "-DocFile", str(DOC))
    assert res.returncode == 0, res.stdout + res.stderr
    got = json.loads((work / "result.json").read_text(encoding="utf-8"))
    got["shares"] = shares
    got["work"] = str(work)
    return got


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


# ---------------------------------------------------------------------------
# #607: the split set in the menu and the share cards
# ---------------------------------------------------------------------------

def test_restore_bat_still_prints_the_card_texts_the_split_leg_waits_for() -> None:
    bat = BAT.read_text(encoding="utf-8", errors="replace")
    assert 'set "_LB=!_LB! (password split: any !MK_K! of !MK_N! share cards)"' in bat
    assert "echo The password for !SET_NAME! is split into !SPLIT_N! share cards; you" in bat
    assert "echo need any !SPLIT_K! of them!SID_TEXT!." in bat
    assert 'set /p "SHARE=Words of card %SI%: "' in bat
    assert "echo Share card %SI% was not typed right: the line above says where." in bat
    assert "echo [lcsas-restore] the share cards rebuilt the password." in bat
    assert ('set /p "ANOTHER_ANS=Restore another backup set from these discs? '
            '[y/N]: "') in bat


def test_invoke_set_menu_reads_the_split_and_chooses_the_split_set(helpers: dict) -> None:
    got = helpers["menu_choose_split"]
    assert (got["error"], got["problems"], got["typed"]) == ("", [], "2")
    assert helpers["split_set"] == OTHER_ID
    assert helpers["named_other"] == []


@pytest.mark.parametrize(("case", "says"), [
    ("menu_split_missing", "shows 'Work documents' as split none; its marker says 2-of-3"),
    ("menu_split_wrong", "shows 'Work documents' as split 3-of-5; its marker says 2-of-3"),
    ("menu_split_on_unsplit", "shows 'Fotos da fam_lia' as split 2-of-3; its marker says none"),
])
def test_invoke_set_menu_flags_a_split_that_is_not_the_sets(
    helpers: dict, case: str, says: str,
) -> None:
    got = helpers[case]
    assert any(says in p for p in got["problems"]), got


def test_get_card_words_reads_a_printed_card(helpers: dict) -> None:
    assert helpers["card1"] == helpers["shares"][0]
    assert helpers["card3"] == helpers["shares"][2]
    assert "not a share card" in helpers["not_a_card"]


def test_find_card_text_finds_three_words_in_a_row_only(helpers: dict) -> None:
    runs = {
        " ".join(card.split()[i:i + 3])
        for card in (helpers["shares"][0], helpers["shares"][2])
        for i in range(len(card.split()) - 2)
    }
    assert helpers["runs"] == len(runs)
    assert helpers["found"] == ["deep/kept.txt"]


# ---------------------------------------------------------------------------
# C8: swaps <= discs, each disc copied into the cache once
# ---------------------------------------------------------------------------

def test_swaps_within_discs(helpers: dict) -> None:
    assert helpers["swaps_ok"] == []
    assert any(
        "LCSAS_TEST_TINY_2026_0003.iso went into the drive 2 times" in p
        for p in helpers["swaps_twice"]
    ), helpers["swaps_twice"]
    assert any(
        "4 swaps here for 3 data discs" in p for p in helpers["swaps_more"]
    ), helpers["swaps_more"]


def test_copied_once(helpers: dict) -> None:
    assert helpers["copied_once"] == []
    assert helpers["copied_twice"] == [
        "'copying disc LCSAS_TEST_TINY_2026_0002 to the cache' appears 2 times "
        "(want exactly once, #612)"
    ]
    assert [p.split("'")[1] for p in helpers["copied_missing"]] == [
        "copying disc LCSAS_TEST_TINY_2026_0002 to the cache",
        "copying disc LCSAS_TEST_TINY_2026_0003 to the cache",
    ]


def test_copied_once_counts_only_the_drains_launch_line(helpers: dict) -> None:
    """A drain of 16 MiB or more also prints '... to the cache: n of N MB'
    progress lines; they are the same copy, not a second one."""
    assert helpers["copied_progress"] == []
    # Counted over two runs, and a disc listed twice is checked once.
    assert helpers["copied_what"] == [
        "'copying disc LCSAS_TEST_TINY_2026_0001 to the cache' appears 2 times "
        "over both runs (want exactly once, #612)"
    ]


def test_pack_cache_whole(helpers: dict) -> None:
    """D item 6 (C8): every pack of the disc read before the stop is in
    the cache, each whole (its SHA-256 is its name), and no .part."""
    assert helpers["cache_good"] == {"n": 2, "problems": []}
    missing = helpers["cache_missing"]["problems"]
    assert len(missing) == 1 and missing[0].startswith(
        "1 pack(s) of LCSAS_TEST_TINY_2026_0001.iso, read before the stop, "
        "are not in the pack cache"), missing
    assert missing[0].endswith("f" * 64), missing
    short = helpers["cache_short"]["problems"]
    assert len(short) == 1 and short[0].startswith(
        f"pack {helpers['cache_short_name']} in the pack cache is not whole: "
        "its SHA-256 is "), short
    assert short[0].endswith("(100 bytes)"), short
    part = helpers["cache_part"]["problems"]
    assert len(part) == 1 and part[0].startswith(
        "partial pack file(s) left in the pack cache after the stop: "), part
    assert part[0].endswith("e" * 64 + ".part"), part
    empty = helpers["cache_empty"]
    assert empty["n"] == 0 and len(empty["problems"]) == 1, empty
    assert empty["problems"][0].startswith("no pack in the pack cache "), empty


# ---------------------------------------------------------------------------
# #608: RECOVER_WINDOWS.txt's PowerShell lines
# ---------------------------------------------------------------------------

def test_get_doc_password_lines_finds_the_docs_lines(helpers: dict) -> None:
    """The lines are the doc's own, from "$s = Read-Host -AsSecureString"
    to "Remove-Item $f"."""
    lines = helpers["doc_lines"]
    assert lines[0].startswith("$s = Read-Host -AsSecureString")
    assert lines[-1] == "Remove-Item $f"
    doc = DOC.read_text(encoding="utf-8")
    assert "\n".join("    " + ln for ln in lines) in doc
    read_host, pwfile, restore = helpers["doc_at"]
    assert lines[read_host].startswith("$s = Read-Host")
    assert lines[pwfile] == "$env:LCSAS_PWFILE = $f"
    assert re.fullmatch(r"[A-Z]:\\restore\.bat", lines[restore])


@pytest.mark.parametrize(("case", "says"), [
    ("no_heading", "lines 'THE PASSWORD' (want 1)"),
    ("no_start", "the snippet's start marker"),
    ("no_end", "Remove-Item $f (the end marker) (0 lines)"),
    ("no_bstr", "PtrToStringBSTR (0 lines)"),
    ("no_utf8", "UTF8Encoding($false) (0 lines)"),
])
def test_get_doc_password_lines_fails_loudly_when_the_lines_change(
    helpers: dict, case: str, says: str,
) -> None:
    got = helpers["doc_errors"][case]
    assert says in got, got


def test_the_doc_password_script_writes_the_password_exactly(
    helpers: dict, tmp_path: Path,
) -> None:
    """The script made of the doc's lines, run with no keyboard: only the
    Read-Host source differs from the doc, and it writes the password
    with no BOM and no line break and sets LCSAS_PWFILE to the file.
    Here under pwsh 7; windows-e2e runs it under Windows PowerShell 5.1."""
    script = Path(helpers["work"]) / "doc-password.ps1"
    text = script.read_text(encoding="ascii")
    body = text.splitlines()[1:-2]  # the doc's lines, between the harness's
    doc_lines = helpers["doc_lines"][:helpers["doc_at"][1] + 1]
    assert body[1:] == doc_lines[1:]
    assert body[0] == (
        "$s = ConvertTo-SecureString -String $env:LCSAS_E2E_DOC_PW "
        "-AsPlainText -Force"
    )
    temp = tmp_path / "temp"
    temp.mkdir()
    result = tmp_path / "result.json"
    password = "invented pass: \u00e9t\u00e9 & 100% <ok>"
    env = {**os.environ, "TEMP": str(temp), "LCSAS_E2E_DOC_PW": password,
           "LCSAS_E2E_DOC_RESULT": str(result)}
    assert PWSH is not None
    res = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    made = json.loads(result.read_text(encoding="utf-8"))
    assert made["pwfile"] == made["f"]
    # "$env:TEMP\lcsas-pw.txt": off Windows the "\" is part of the name.
    assert made["f"] == f"{temp}\\lcsas-pw.txt"
    assert Path(made["f"]).read_bytes() == password.encode("utf-8")
