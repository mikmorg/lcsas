# Shared helpers for the windows-e2e multi-disc, RAM-relocation and
# meta-disc-eject legs (#517).
#
# Dot-source from a `shell: pwsh` step of the `restore` job:
#
#     . (Join-Path $env:GITHUB_WORKSPACE 'repo/tests/e2e/windows_e2e_lib.ps1')
#
# Everything here drives the REAL recovery/scripts/restore.bat on a REAL
# windows-latest runner, against the ISOs the `fixture` job built and the
# first restore step mounted.  Nothing here changes restore.bat's
# behaviour; the legs differ from the happy path only in that
# LCSAS_NO_RELOCATE is REMOVED from the environment, so the single-drive
# RAM-relocation block (restore.bat "Single-drive guard") really runs.
#
# Design notes
#   * restore.bat's stdout+stderr are redirected to a file INSIDE the
#     `cmd /c` string (a PowerShell-level redirect resets $LASTEXITCODE;
#     see the wrong-password step in windows-e2e.yml).
#   * Assertions are collected into a list and thrown together at the end,
#     after all diagnostics are printed, so one red run tells the whole
#     story instead of the first failure only.
#   * Since rc6 (#609) the relocated copy lives in the fixed tools folder,
#     %LOCALAPPDATA%\LCSAS-restore, which stays until a restore has
#     finished.  Every relocated leg starts by removing it (Clear-ToolsDir),
#     so no leg takes an earlier leg's copy by accident, and checks it is
#     gone after exit 0 and kept after exit 75 or 77.
#   * The fixture has two backup sets, registered as an owner does (`lcsas
#     repo add`), so their folders on the discs are named by UUID ids, not
#     by their names (#617).  sets.json says which is which.  The leg that
#     goes through restore.bat's set menu is the single-drive leg; every
#     other leg names the set in LCSAS_REPO (by its id, or by the name
#     restore.bat shows), and checks the "Backup set:" line shows that
#     name, never the id (#606).

# The fixture's backup sets (sets.json, from the fixture job): .restore is
# the one manifest.json describes, .others are there to fill the menu.
# Each has .id (the UUID folder name on the discs), .name, and .name_cmd,
# the name restore.bat shows (C1: letters outside A-Z/0-9/space/._+,- are
# "_").
function Get-FixtureSets {
    param([string]$Fixture)
    $p = Join-Path $Fixture 'sets.json'
    if (-not (Test-Path -LiteralPath $p)) { throw "no sets.json in $Fixture" }
    $sets = Get-Content -LiteralPath $p -Raw -Encoding utf8 | ConvertFrom-Json
    if (-not $sets.restore.id -or -not $sets.restore.name_cmd) { throw "sets.json names no set to restore" }
    if ($sets.restore.id -eq $sets.restore.name) { throw "sets.json: the set's id is its name; the fixture must use repo add (#617)" }
    return $sets
}

# Every set of the fixture, the restored one first.
function Get-AllSets {
    param($Sets)
    return @(@($Sets.restore) + @($Sets.others | Where-Object { $_ }))
}

# #607: the fixture's set whose password is split into share cards (its
# .split is "K-of-N", as its marker says; .cards are the printed cards,
# relative to the fixture folder; .manifest is its tree).
function Get-SplitSet {
    param($Sets)
    $split = @(Get-AllSets $Sets | Where-Object { $_.split -and $_.split -ne 'none' })
    if ($split.Count -ne 1) { throw "sets.json has $($split.Count) split sets (want 1)" }
    if (-not $split[0].cards -or -not $split[0].manifest -or -not $split[0].split_id) {
        throw "sets.json: the split set $($split[0].name_cmd) has no cards, manifest or split_id"
    }
    return $split[0]
}

# #606: restore.bat names the chosen set by its name_cmd on the
# "Backup set:" line, and no line shows a set's id where a name belongs.
# (The "Repo:" line is the set's folder, which IS named by its id.)
# -Set: the set the leg restored (default: the one manifest.json is).
function Assert-SetNamed {
    param(
        [string]$Out,
        $Sets,
        [System.Collections.Generic.List[string]]$Problems,
        $Set = $null
    )
    if (-not $Set) { $Set = $Sets.restore }
    $want = 'Backup set:    ' + $Set.name_cmd
    if (-not $Out.Contains($want)) {
        $Problems.Add("no '$want' line: restore.bat did not show the set's name (#606)")
    }
    foreach ($s in (Get-AllSets $Sets)) {
        $id = [regex]::Escape($s.id)
        if ($Out -match "Backup set:\s+$id" -or $Out -match "\[\d+\]\s+$id") {
            $Problems.Add("restore.bat showed the id $($s.id) where the name '$($s.name_cmd)' belongs (#606)")
        }
    }
}

function Find-MetaDrive {
    foreach ($v in (Get-Volume | Where-Object { $_.DriveLetter })) {
        $l = [string]$v.DriveLetter
        if (Test-Path -LiteralPath "${l}:\restore.bat") { return $l }
    }
    return $null
}

# Mounted optical volumes (the ISOs) that carry a data\ tree, with the
# number of pack files each holds.
function Get-DataDiscs {
    $found = @()
    foreach ($v in (Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveType -eq 'CD-ROM' })) {
        $l = [string]$v.DriveLetter
        if (Test-Path -LiteralPath "${l}:\data") {
            $names = @(Get-ChildItem -LiteralPath "${l}:\data" -Recurse -File -ErrorAction SilentlyContinue |
                       ForEach-Object { $_.Name })
            $found += [pscustomobject]@{
                Drive = "${l}:"; Label = $v.FileSystemLabel; Packs = $names.Count; Names = $names
            }
        }
    }
    return $found
}

# --repo is a metadata\<id> folder (keys/index/snapshots, no data\), so
# EVERY pack must come from a drive restore.bat hands to lcsas-restore.exe
# (it names them: "drives searched for discs:").  This counts every pack,
# whatever set it belongs to, so on its own it no longer proves the
# restored set came from more than one disc (the fixture has two sets,
# #617).  The fixture job proves that part on Linux: the restored set's
# own packs lie on two or more discs, none holding them all
# (fixture_lib.restore_set_span_problem, sets.json restore_discs).  With
# that and the byte-exact tree check, a restore consumed more than one
# disc.
function Assert-MultiDisc {
    param([int]$Minimum = 2)
    $discs = @(Get-DataDiscs | Where-Object { $_.Packs -gt 0 })
    $discs | Format-Table -Property Drive, Label, Packs -AutoSize | Out-String | Write-Host
    Write-Host "Mounted data discs holding packs: $($discs.Count)"
    if ($discs.Count -lt $Minimum) {
        throw "fixture spans only $($discs.Count) data disc(s); need >= $Minimum"
    }
    # No single disc may hold every pack, or one disc would suffice and the
    # sweep would prove nothing.
    $distinct = @($discs | ForEach-Object { $_.Names } | Sort-Object -Unique)
    Write-Host "Distinct pack files across all data discs: $($distinct.Count)"
    foreach ($d in $discs) {
        if ($d.Packs -ge $distinct.Count) {
            throw "$($d.Drive) alone holds all $($distinct.Count) packs; not a multi-disc fixture"
        }
    }
}

# Same byte-exact comparison as the "Verify restored tree matches manifest"
# step; appends a message to $Problems for every discrepancy.
# -ManifestName: another set's manifest (sets.json names it), for a leg
# that restores that set.
function Test-TreeAgainstManifest {
    param(
        [string]$Fixture,
        [string]$Target,
        [System.Collections.Generic.List[string]]$Problems,
        [string]$ManifestName = 'manifest.json'
    )
    $manifest = Get-Content (Join-Path $Fixture $ManifestName) -Raw | ConvertFrom-Json
    $anchor = Get-ChildItem -Path $Target -Recurse -Filter 'file_000.bin' -ErrorAction SilentlyContinue |
              Select-Object -First 1
    if (-not $anchor) {
        $Problems.Add("restored tree has no file_000.bin under $Target")
        return
    }
    $root = $anchor.Directory.FullName
    Write-Host "Restored tree root: $root"
    $bad = 0
    foreach ($p in $manifest.PSObject.Properties) {
        $f = Join-Path $root $p.Name
        if (-not (Test-Path -LiteralPath $f)) { Write-Host "MISSING: $($p.Name)"; $bad++; continue }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash.ToLower()
        if ($got -ne $p.Value) { Write-Host "HASH MISMATCH: $($p.Name)"; $bad++ }
    }
    $expected = @($manifest.PSObject.Properties.Name)
    Get-ChildItem -Path $root -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($root.Length).TrimStart('\').Replace('\', '/')
        if ($expected -notcontains $rel) { Write-Host "EXTRA: $rel"; $bad++ }
    }
    if ($bad -gt 0) { $Problems.Add("restored tree differs from manifest ($bad discrepancies)") }
    else { Write-Host "Restored tree is byte-identical to manifest ($($expected.Count) files)." }
}

# The tools folder restore.bat copies itself into from a disc (#609), and
# the launcher it puts on the Desktop while a restore can be carried on.
function Get-ToolsDir { return (Join-Path $env:LOCALAPPDATA 'LCSAS-restore') }
function Get-DesktopLauncher {
    return (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Resume LCSAS restore.bat')
}

# Describe a tools folder.
function Write-ToolsDirReport {
    param([string]$Dir)
    $files = @(Get-ChildItem -LiteralPath $Dir -Recurse -File -Force -ErrorAction SilentlyContinue)
    $bytes = [int64](($files | Measure-Object -Property Length -Sum).Sum)
    Write-Host ("Tools folder {0}: {1} files, {2:N1} MB" -f $Dir, $files.Count, ($bytes / 1MB))
    foreach ($d in (Get-ChildItem -LiteralPath $Dir -Recurse -Directory -Depth 2 -ErrorAction SilentlyContinue)) {
        $n = @(Get-ChildItem -LiteralPath $d.FullName -Recurse -File -Force -ErrorAction SilentlyContinue).Count
        Write-Host ("  {0}  ({1} files)" -f $d.FullName.Substring($Dir.Length), $n)
    }
}

# Start a leg with no tools folder and no Desktop launcher.
function Clear-ToolsDir {
    $t = Get-ToolsDir
    if (Test-Path -LiteralPath $t) { Remove-Item -Recurse -Force -LiteralPath $t }
    $l = Get-DesktopLauncher
    if (Test-Path -LiteralPath $l) { Remove-Item -Force -LiteralPath $l }
}

# #609: after exit 0 the tools folder and the Desktop launcher are gone
# (the copy leaves .done, and the relaunch line or the launcher deletes
# the folder).  Fail on either, and remove it so a later leg starts clean.
function Assert-ToolsDirGone {
    param([System.Collections.Generic.List[string]]$Problems)
    $t = Get-ToolsDir
    if (Test-Path -LiteralPath $t) {
        Write-Host "LEFT BEHIND by restore.bat after exit 0: $t"
        Write-ToolsDirReport -Dir $t
        $Problems.Add("restore.bat left its tools folder behind after a finished restore: $t")
    }
    $l = Get-DesktopLauncher
    if (Test-Path -LiteralPath $l) {
        $Problems.Add("restore.bat left the Desktop launcher behind after a finished restore: $l")
    }
    Clear-ToolsDir
}

# #609: after exit 75 (stopped at a disc box) or 77 (wrong password) the
# tools folder stays, with resume.cfg and the launcher (in the folder and
# on the Desktop), none of which may hold the password.
function Assert-ToolsDirKept {
    param(
        [System.Collections.Generic.List[string]]$Problems,
        [string]$Password
    )
    $t = Get-ToolsDir
    if (-not (Test-Path -LiteralPath $t)) {
        $Problems.Add("the tools folder $t is gone after a restore that did not finish")
        return
    }
    Write-ToolsDirReport -Dir $t
    if (Test-Path -LiteralPath (Join-Path $t '.done')) {
        $Problems.Add("the tools folder holds .done after a restore that did not finish")
    }
    $kept = @((Join-Path $t 'resume.cfg'), (Join-Path $t 'Resume LCSAS restore.bat'), (Get-DesktopLauncher))
    foreach ($f in $kept) {
        if (-not (Test-Path -LiteralPath $f)) { $Problems.Add("missing after the stop: $f"); continue }
        $text = [System.IO.File]::ReadAllText($f)
        if ($Password -and $text.Contains($Password)) { $Problems.Add("$f holds the password") }
        if ($f.EndsWith('resume.cfg')) { Write-Host "resume.cfg:"; Write-Host $text }
    }
}

# #612: the tools folder holds this computer's programs only, and the
# restore said what it copied.
function Assert-OnlyThisPlatform {
    param(
        [string]$Out,
        [System.Collections.Generic.List[string]]$Problems,
        [switch]$CheckFolder
    )
    $named = @([regex]::Matches($Out, 'recovery\\bin\\([^\\\r\n]+)\\') |
               ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
    if ($named.Count -eq 0) { $Problems.Add('the relocation named no program it copied (#612)') }
    foreach ($n in $named) {
        if ($n -ne 'x86_64-pc-windows-gnu') { $Problems.Add("the relocation copied recovery\bin\$n (#612)") }
    }
    if (-not $CheckFolder) { return }
    $bin = Join-Path (Get-ToolsDir) 'recovery\bin'
    $dirs = @(Get-ChildItem -LiteralPath $bin -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    if (($dirs -join ',') -ne 'x86_64-pc-windows-gnu') {
        $Problems.Add("tools folder recovery\bin holds: $($dirs -join ', ') (want x86_64-pc-windows-gnu only)")
    }
    $progs = @(Get-ChildItem -LiteralPath (Join-Path $bin 'x86_64-pc-windows-gnu') -File -ErrorAction SilentlyContinue |
               ForEach-Object { $_.Name } | Sort-Object)
    if (($progs -join ',') -ne 'lcsas-ecc.exe,lcsas-keyshare.exe,lcsas-restore.exe') {
        $Problems.Add("tools folder programs: $($progs -join ', ')")
    }
}

# #611: from a disc, restore.bat's write probe printed "Access is denied."
function Assert-NoAccessDenied {
    param(
        [string]$Out,
        [System.Collections.Generic.List[string]]$Problems
    )
    if ($Out -match 'Access is denied') { $Problems.Add("restore.bat printed 'Access is denied.' (#611)") }
}

function Read-SharedText {
    param([string]$Path)
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
        try { return ([System.IO.StreamReader]::new($fs)).ReadToEnd() }
        finally { $fs.Dispose() }
    } catch { return '' }
}

function Get-TempNames {
    param([string]$Filter)
    return @(Get-ChildItem -Path $env:TEMP -Filter $Filter -Force -ErrorAction SilentlyContinue |
             ForEach-Object { $_.Name })
}

# ---------------------------------------------------------------------
# Leg: restore.bat WITHOUT LCSAS_NO_RELOCATE, started with its cwd on the
# disc exactly as a double-click does.  Asserts that the relocation
# happened into the tools folder, copied this computer's programs only and
# said so (#612) without "Access is denied." (#611), that the tier-1
# binary ran from the copy with a local catalog copy, that the restored
# tree is byte-exact, and that the script removed the tools folder and its
# password file after the restore (#609).
#
# -SeedStale: first leave in the tools folder what an older LCSAS version
# would (a different restore.bat and tier-1 program, a pack cache and a
# stale .done).  restore.bat must replace it (its `fc /b` says "not the
# same") rather than run it; wine's fc cannot tell, so only this proves it.
# The folder holds .lcsas-tools, as every tools folder restore.bat has
# made since rc6 does: without it restore.bat refuses the folder.
# ---------------------------------------------------------------------
function Invoke-RelocationLeg {
    param(
        [string]$Fixture,
        [string]$BatRel,        # path of restore.bat relative to the meta-disc root
        [string]$TargetName,
        [switch]$SeedStale,
        # LCSAS_REPO names the set by its id, or by the name restore.bat
        # shows (name_cmd), which it must match too (#606).
        [ValidateSet('id', 'name_cmd')]
        [string]$PickBy = 'id'
    )
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false
    Remove-Item Env:LCSAS_NO_RELOCATE -ErrorAction SilentlyContinue
    Remove-Item Env:LCSAS_RELOCATED -ErrorAction SilentlyContinue
    $sets = Get-FixtureSets -Fixture $Fixture
    $env:LCSAS_REPO = $sets.restore.$PickBy
    Write-Host "LCSAS_REPO=$($env:LCSAS_REPO) (the set's $PickBy)"

    $meta = Find-MetaDrive
    if (-not $meta) { throw 'meta ISO not mounted (no drive holds restore.bat)' }
    $bat = "${meta}:\$BatRel"
    if (-not (Test-Path -LiteralPath $bat)) { throw "no $bat on the meta disc" }
    Assert-MultiDisc

    Clear-ToolsDir
    $tools = Get-ToolsDir
    if ($SeedStale) {
        $old = Join-Path $tools 'recovery\scripts'
        $oldBin = Join-Path $tools 'recovery\bin\x86_64-pc-windows-gnu'
        New-Item -ItemType Directory -Force -Path $old, $oldBin, (Join-Path $tools 'pack-cache') | Out-Null
        Set-Content -LiteralPath (Join-Path $old 'restore.bat') -Value '@echo off& echo STALE COPY RAN& exit /b 9' -Encoding ascii
        Set-Content -LiteralPath (Join-Path $oldBin 'lcsas-restore.exe') -Value 'not this version' -Encoding ascii
        Set-Content -LiteralPath (Join-Path $tools 'pack-cache\stale.pack') -Value 'stale' -Encoding ascii
        Set-Content -LiteralPath (Join-Path $tools '.done') -Value '' -Encoding ascii
        Set-Content -LiteralPath (Join-Path $tools '.lcsas-tools') -Value 'made by restore.bat' -Encoding ascii
        Write-Host "Seeded a stale tools folder at $tools"
    }

    $target = Join-Path $env:RUNNER_TEMP $TargetName
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $pw = (Get-Content (Join-Path $Fixture 'repo.pw') -Raw).Trim()
    $answers = Join-Path $env:RUNNER_TEMP "answers-$TargetName.txt"
    @($target, $pw, '', '', '', '', '', '') | Set-Content -Path $answers -Encoding ascii
    $outFile = Join-Path $env:RUNNER_TEMP "out-$TargetName.txt"

    $pwBefore  = Get-TempNames 'lcsas-pw-*'

    Write-Host "Running (no LCSAS_NO_RELOCATE, cwd ${meta}:\): $bat"
    Push-Location -LiteralPath "${meta}:\"
    try {
        cmd /c "`"$bat`" < `"$answers`" > `"$outFile`" 2>&1"
        $rc = $LASTEXITCODE
    } finally { Pop-Location }
    $out = Get-Content $outFile -Raw
    Write-Host '----- restore.bat output -----'
    Write-Host $out
    Write-Host '----- end output -----'
    Write-Host "restore.bat exit code: $rc"

    $problems = New-Object 'System.Collections.Generic.List[string]'

    # 1. relocation happened, into the tools folder, copying this
    #    computer's programs only (#612), quietly (#611).
    $m = [regex]::Match($out, 'copied recovery files to (.+)')
    if ($m.Success) {
        $copied = $m.Groups[1].Value.Trim()
        Write-Host "Relocation message seen; tools folder = $copied"
        if ($copied -ne $tools) { $problems.Add("copied to $copied, not the tools folder $tools") }
    } else {
        $problems.Add("no 'copied recovery files to' message: the relocation did not happen")
    }
    if ($out -match 'using the recovery files already copied') {
        $problems.Add('restore.bat ran a tools folder it should have replaced')
    }
    if ($out -match 'STALE COPY RAN') { $problems.Add('the stale restore.bat in the tools folder ran') }
    if ($out -notmatch 'PAUSES the restore') { $problems.Add('no QuickEdit note (#613)') }
    Assert-OnlyThisPlatform -Out $out -Problems $problems
    Assert-NoAccessDenied -Out $out -Problems $problems
    Assert-SetNamed -Out $out -Sets $sets -Problems $problems

    # 2. the tier-1 binary that ran came from the tools folder, so the
    #    programs were copied (the folder is gone by now to look in).
    $t1 = [regex]::Match($out, '\[tier 1\] running (.+)')
    if ($t1.Success) {
        $ran = $t1.Groups[1].Value.Trim()
        Write-Host "Tier-1 binary: $ran"
        if (-not $ran.StartsWith("$tools\", [System.StringComparison]::OrdinalIgnoreCase)) {
            $problems.Add("tier-1 binary did not run from the tools folder: $ran")
        }
    } else {
        $problems.Add("no '[tier 1] running' line: the tools folder had no lcsas-restore.exe (the programs were not copied)")
    }

    # 3. #610: tier 1 got a COPY of a disc's catalog.db, made in the
    #    tools folder, never the file on the disc (an open file holds the
    #    disc in its drive).  Every ISO is mounted here, so restore.bat
    #    finds a catalog.db at launch.
    $cat = [regex]::Match($out, 'using catalog ([A-Z]:\\catalog\.db) \(a copy in (.+)\)')
    if ($cat.Success) {
        Write-Host "Catalog: $($cat.Groups[1].Value), copy in $($cat.Groups[2].Value)"
        if ($cat.Groups[2].Value.Trim() -ne $tools) {
            $problems.Add("the catalog copy is not in the tools folder: $($cat.Groups[2].Value)")
        }
    } else {
        $problems.Add("no 'using catalog X:\catalog.db (a copy in ...)' line: tier 1 got no local copy of the catalog")
    }

    # 4. completion + exit status + byte-exact tree.
    if ($rc -ne 0) { $problems.Add("restore.bat exit code $rc (want 0)") }
    # Case-sensitive: tier 1's own "[lcsas-restore] restore complete" line
    # (also in $out via 2>&1) must not stand in for restore.bat's banner.
    if ($out -cnotmatch 'Restore complete') { $problems.Add("'Restore complete' banner missing") }
    Test-TreeAgainstManifest -Fixture $Fixture -Target $target -Problems $problems

    # 5. nothing left behind: the password file in %TEMP%, or the tools
    #    folder and its Desktop launcher (#609).
    $pwLeft = @(Get-TempNames 'lcsas-pw-*' | Where-Object { $pwBefore -notcontains $_ })
    if ($pwLeft.Count -gt 0) { $problems.Add("password file(s) left in %TEMP%: $($pwLeft -join ', ')") }
    Assert-ToolsDirGone -Problems $problems

    if ($problems.Count -gt 0) {
        throw ("relocation leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host 'Relocation leg passed: copied to the tools folder, tier 1 ran from it with a local catalog copy, tree byte-exact, folder removed.'
}

# ---------------------------------------------------------------------
# Leg (#545): the wrong-password case of the windows-e2e.yml step, but
# relocated.  The relaunch line now runs commands after the relocated copy
# returns, and any of them can replace its exit code, so a failure must
# still come back as 77 through the relocation.  Every other relocated
# leg expects 0, which a lost exit code also gives.  Since rc6 (#609) the
# tools folder STAYS after 77 (only a finished restore removes it), with
# resume.cfg and the launcher, and no password in either.
# ---------------------------------------------------------------------
function Invoke-RelocatedWrongPasswordLeg {
    param(
        [string]$Fixture,
        [string]$BatRel,
        [string]$TargetName
    )
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false
    Remove-Item Env:LCSAS_NO_RELOCATE -ErrorAction SilentlyContinue
    Remove-Item Env:LCSAS_RELOCATED -ErrorAction SilentlyContinue
    $sets = Get-FixtureSets -Fixture $Fixture
    $env:LCSAS_REPO = $sets.restore.id

    $meta = Find-MetaDrive
    if (-not $meta) { throw 'meta ISO not mounted (no drive holds restore.bat)' }
    $bat = "${meta}:\$BatRel"
    Clear-ToolsDir

    $target = Join-Path $env:RUNNER_TEMP $TargetName
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $wrong = 'definitely-the-wrong-password'
    $answers = Join-Path $env:RUNNER_TEMP "answers-$TargetName.txt"
    @($target, $wrong, '', '', '', '', '', '') |
        Set-Content -Path $answers -Encoding ascii
    $outFile = Join-Path $env:RUNNER_TEMP "out-$TargetName.txt"

    $pwBefore  = Get-TempNames 'lcsas-pw-*'

    Write-Host "Running relocated, wrong password (cwd ${meta}:\): $bat"
    Push-Location -LiteralPath "${meta}:\"
    try {
        cmd /c "`"$bat`" < `"$answers`" > `"$outFile`" 2>&1"
        $rc = $LASTEXITCODE
    } finally { Pop-Location }
    $out = Get-Content $outFile -Raw
    Write-Host '----- restore.bat output -----'
    Write-Host $out
    Write-Host '----- end output -----'
    Write-Host "relocated wrong-password exit code: $rc"

    $problems = New-Object 'System.Collections.Generic.List[string]'
    if ($out -notmatch 'copied recovery files to') {
        $problems.Add("no 'copied recovery files to' message: the relocation did not happen")
    }
    if ($rc -ne 77) { $problems.Add("exit code $rc through the relocation (want 77, as without it)") }
    if ($out -notmatch 'could not decrypt the repository') { $problems.Add('wrong-password banner missing') }
    if ($out -match 'trying tier 2') { $problems.Add('fell through to tier 2 on a wrong password') }
    $restored = @(Get-ChildItem -Path $target -Recurse -File -Force -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -ne '.lcsas-restore-marker' })
    if ($restored.Count -gt 0) {
        $problems.Add("left a partial tree behind: $($restored.FullName -join ', ')")
    }
    $pwLeft = @(Get-TempNames 'lcsas-pw-*' | Where-Object { $pwBefore -notcontains $_ })
    if ($pwLeft.Count -gt 0) { $problems.Add("password file(s) left in %TEMP%: $($pwLeft -join ', ')") }
    Assert-NoAccessDenied -Out $out -Problems $problems
    Assert-SetNamed -Out $out -Sets $sets -Problems $problems
    # #609: not finished, so the tools folder stays, with a way to carry on.
    Assert-ToolsDirKept -Problems $problems -Password $wrong
    if ($out -notmatch 'To try again, run ".+Resume LCSAS restore\.bat"') {
        $problems.Add('the wrong-password banner does not name the launcher')
    }
    Clear-ToolsDir

    if ($problems.Count -gt 0) {
        throw ("relocated wrong-password leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host 'Relocated wrong-password leg passed: exit 77 through the relocation, tools folder kept.'
}

# ---------------------------------------------------------------------
# Leg: relocate, then EJECT the meta disc (Dismount-DiskImage) before the
# restore proper starts, and require the restore to finish anyway.
#
# Determinism: restore.bat is started with a redirected stdin PIPE that we
# do not write to.  After the relocation banner it blocks at the "Restore
# to which folder?" prompt (set /p).  We wait for the relocated copy's
# banner, dismount the meta ISO, confirm the drive is gone, and only THEN
# feed the target + password.  So tier 1 runs entirely after the eject.
# ---------------------------------------------------------------------
function Invoke-EjectLeg {
    param(
        [string]$Fixture,
        [string]$BatRel,
        [string]$TargetName
    )
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false
    Remove-Item Env:LCSAS_NO_RELOCATE -ErrorAction SilentlyContinue
    Remove-Item Env:LCSAS_RELOCATED -ErrorAction SilentlyContinue

    $meta = Find-MetaDrive
    if (-not $meta) { throw 'meta ISO not mounted (no drive holds restore.bat)' }
    $bat = "${meta}:\$BatRel"
    $metaIso = (Get-ChildItem -Path $Fixture -Filter 'LCSAS_META.iso').FullName
    if (-not $metaIso) { throw 'LCSAS_META.iso not found in the fixture dir' }
    # The set is named up front: the banner this leg waits for comes after
    # the set menu, which would wait for an answer.
    $sets = Get-FixtureSets -Fixture $Fixture
    $env:LCSAS_REPO = $sets.restore.id

    $target = Join-Path $env:RUNNER_TEMP $TargetName
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $pw = (Get-Content (Join-Path $Fixture 'repo.pw') -Raw).Trim()
    $outFile = Join-Path $env:RUNNER_TEMP "out-$TargetName.txt"
    if (Test-Path -LiteralPath $outFile) { Remove-Item -Force -LiteralPath $outFile }
    Clear-ToolsDir

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'cmd.exe'
    $psi.Arguments = "/c `"`"$bat`" > `"$outFile`" 2>&1`""
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.StandardInputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = "${meta}:\"

    $problems = New-Object 'System.Collections.Generic.List[string]'
    $proc = $null
    $ejected = $false
    try {
        Write-Host "Starting (no LCSAS_NO_RELOCATE, stdin held open): $bat"
        $proc = [System.Diagnostics.Process]::Start($psi)

        # Wait for the relocated copy to be RUNNING (its banner), not merely
        # copied: the parent must already be inside `call`.
        $deadline = (Get-Date).AddSeconds(90)
        $text = ''
        while ((Get-Date) -lt $deadline) {
            $text = Read-SharedText $outFile
            if ($text -match 'copied recovery files to' -and $text -match 'Architecture:\s+\S+') { break }
            if ($proc.HasExited) { break }
            Start-Sleep -Milliseconds 200
        }
        if ($text -notmatch 'copied recovery files to') {
            throw "relocation message never appeared; output so far:`n$text"
        }
        if ($text -notmatch 'Architecture:\s+\S+') {
            throw "relocated copy never reached its banner; output so far:`n$text"
        }
        Write-Host 'Relocated copy is up and waiting for the target prompt; ejecting the meta disc now.'
        # #612: while the copy runs, its folder holds this computer's
        # programs only.
        Assert-OnlyThisPlatform -Out $text -Problems $problems -CheckFolder

        Dismount-DiskImage -ImagePath $metaIso | Out-Null
        for ($i = 0; $i -lt 60; $i++) {
            if (-not (Test-Path -LiteralPath "${meta}:\")) { $ejected = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ejected) { throw "${meta}: is still present after Dismount-DiskImage" }
        Write-Host "${meta}: is gone (meta disc ejected)."

        # Now let the restore proceed.
        try {
            foreach ($line in @($target, $pw, '', '', '', '', '', '')) { $proc.StandardInput.WriteLine($line) }
            $proc.StandardInput.Close()
        } catch {
            # The script may already have exited (e.g. it aborted); the
            # output and assertions below report why.
            Write-Host "stdin write failed: $($_.Exception.Message)"
        }

        if (-not $proc.WaitForExit(240000)) {
            $problems.Add('restore.bat did not finish within 240 s after the eject')
            & taskkill /PID $proc.Id /T /F | Out-Null
        }
    } finally {
        if ($proc -and -not $proc.HasExited) { & taskkill /PID $proc.Id /T /F | Out-Null }
    }
    $rc = if ($proc -and $proc.HasExited) { $proc.ExitCode } else { 'n/a' }
    $out = Read-SharedText $outFile
    Write-Host '----- restore.bat output -----'
    Write-Host $out
    Write-Host '----- end output -----'
    Write-Host "restore.bat exit code after the eject: $rc"

    if (-not $ejected) { $problems.Add('the meta disc was never ejected') }
    # Case-sensitive, as in Invoke-RelocationLeg: the banner, not tier 1's line.
    if ($out -cnotmatch 'Restore complete') { $problems.Add("'Restore complete' banner missing after the eject") }
    if ($out -notmatch '\[lcsas-restore\] restore complete') { $problems.Add('tier-1 restore did not report completion after the eject') }
    # #532: the outer instance used to read its `exit /b` from the ejected
    # disc after the relocated copy returned, printing this and exiting 1.
    if ($out -match 'The system cannot find the path specified') {
        $problems.Add('the outer cmd tried to read restore.bat from the ejected disc ("The system cannot find the path specified.")')
    }
    if ("$rc" -ne '0') { $problems.Add("exit code after a completed restore is $rc, not 0") }
    Test-TreeAgainstManifest -Fixture $Fixture -Target $target -Problems $problems
    Assert-NoAccessDenied -Out $out -Problems $problems
    Assert-SetNamed -Out $out -Sets $sets -Problems $problems
    Assert-ToolsDirGone -Problems $problems

    if ($problems.Count -gt 0) {
        throw ("eject leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host 'Eject leg passed: the relocated restore survived the meta-disc eject.'
}

# Wait until $Pattern (a case-sensitive regex) shows up in $Path, or
# $Proc exits, or $Seconds pass.  Returns the text read last; the caller
# tests it again.
function Wait-ForOutput {
    param(
        [string]$Path,
        [string]$Pattern,
        [System.Diagnostics.Process]$Proc,
        [int]$Seconds
    )
    $deadline = (Get-Date).AddSeconds($Seconds)
    $text = ''
    while ((Get-Date) -lt $deadline) {
        $text = Read-SharedText $Path
        if ($text -cmatch $Pattern) { break }
        if ($Proc.HasExited) { Start-Sleep -Milliseconds 500; $text = Read-SharedText $Path; break }
        Start-Sleep -Milliseconds 200
    }
    return $text
}

# Put $Iso into the drive letter $Letter: the disc in it comes out
# (Dismount-DiskImage, which removes the letter), then $Iso is mounted
# with no letter and `mountvol` gives it $Letter.  This is the one-drive
# reader's eject and insert, in the SAME letter.
function Switch-DiscInLetter {
    param(
        [string]$Letter,
        [string]$Current,
        [string]$Iso
    )
    Dismount-DiskImage -ImagePath $Current | Out-Null
    for ($i = 0; $i -lt 60; $i++) {
        if (-not (Test-Path -LiteralPath "${Letter}:\")) { break }
        Start-Sleep -Milliseconds 500
    }
    $img = Mount-DiskImage -ImagePath $Iso -NoDriveLetter -PassThru
    $vol = $null
    for ($i = 0; $i -lt 60; $i++) {
        $vol = Get-Volume -DiskImage $img -ErrorAction SilentlyContinue
        if ($vol -and $vol.Path) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not ($vol -and $vol.Path)) { throw "no volume for $Iso after Mount-DiskImage -NoDriveLetter" }
    Write-Host "mountvol ${Letter}: $($vol.Path)"
    & mountvol "${Letter}:" $vol.Path | Out-Host
    for ($i = 0; $i -lt 20; $i++) {
        if (Test-Path -LiteralPath "${Letter}:\data") { return }
        Start-Sleep -Milliseconds 500
    }
    # Second way to give a volume a letter, should mountvol not take.
    Write-Host "mountvol did not give ${Letter}: to $Iso; trying Win32_Volume"
    $wv = Get-CimInstance -ClassName Win32_Volume | Where-Object { $_.DeviceID -eq $vol.Path }
    if ($wv) { Set-CimInstance -InputObject $wv -Property @{ DriveLetter = "${Letter}:" } }
    for ($i = 0; $i -lt 40; $i++) {
        if (Test-Path -LiteralPath "${Letter}:\data") { return }
        Start-Sleep -Milliseconds 500
    }
    throw "$Iso did not appear at ${Letter}: after mountvol"
}

# Which data ISO holds which pack (each data ISO mounted once, its data\
# listed, then dismounted).
function Get-PackMap {
    param([string[]]$DataIsos)
    $packMap = @{}
    foreach ($iso in $DataIsos) {
        $img = Mount-DiskImage -ImagePath $iso -PassThru
        $l = [string](Get-Volume -DiskImage $img).DriveLetter
        $names = @(Get-ChildItem -LiteralPath "${l}:\data" -Recurse -File -ErrorAction SilentlyContinue |
                   ForEach-Object { $_.Name })
        Write-Host ("{0}: {1} packs" -f (Split-Path $iso -Leaf), $names.Count)
        $packMap[$iso] = $names
        Dismount-DiskImage -ImagePath $iso | Out-Null
    }
    return $packMap
}

# Start $Bat in cmd.exe with stdin held open and stdout+stderr in
# $OutFile; LCSAS_INTERACTIVE=on makes tier 1 ask at the disc box
# although its stdin is a pipe.
function Start-RestoreProcess {
    param(
        [string]$Bat,
        [string]$OutFile,
        [string]$WorkDir,
        [hashtable]$Env = @{}
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'cmd.exe'
    $psi.Arguments = "/c `"`"$Bat`" > `"$OutFile`" 2>&1`""
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.StandardInputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = $WorkDir
    $psi.EnvironmentVariables['LCSAS_INTERACTIVE'] = 'on'
    foreach ($k in $Env.Keys) { $psi.EnvironmentVariables[$k] = $Env[$k] }
    return [System.Diagnostics.Process]::Start($psi)
}

# The data ISO whose label is newest: the latest year, then the highest
# number (the last two "_" fields of LCSAS_<MEDIA>_<YYYY>_<SEQ>.iso).
function Get-NewestDataIso {
    param([string[]]$DataIsos)
    $best = $null
    $bestKey = $null
    foreach ($iso in $DataIsos) {
        $parts = [IO.Path]::GetFileNameWithoutExtension($iso).Split('_')
        $key = [int64]$parts[$parts.Count - 2] * 1000000 + [int64]$parts[$parts.Count - 1]
        if ($null -eq $bestKey -or $key -gt $bestKey) { $best = $iso; $bestKey = $key }
    }
    return $best
}

# #605 (C7): the newest-data-disc step.  Wait for its question, take the
# disc that is in $Letter out and put the newest data ISO in, in the SAME
# letter, press ENTER, and check that the script read the disc's full
# label from its VOLUME_INFO.txt.  Returns the ISO now in $Letter.
function Invoke-NewestDiscStep {
    param(
        [System.Diagnostics.Process]$Proc,
        [string]$OutFile,
        [string]$Letter,
        [string]$Current,
        [string[]]$DataIsos,
        [System.Collections.Generic.List[string]]$Problems
    )
    $text = Wait-ForOutput -Path $OutFile -Pattern 'Newest data disc: ' -Proc $Proc -Seconds 300
    if ($text -cnotmatch 'Newest data disc: ') { throw "no newest-data-disc question; output so far:`n$text" }
    if ($text -cnotmatch 'Take the META disc out and put in your NEWEST data disc') {
        $Problems.Add('the newest-data-disc step does not use the C7 wording')
    }
    $newest = Get-NewestDataIso -DataIsos $DataIsos
    $label = [IO.Path]::GetFileNameWithoutExtension($newest)
    Switch-DiscInLetter -Letter $Letter -Current $Current -Iso $newest
    Write-Host "newest-data-disc step: $label is in ${Letter}:; ENTER"
    $Proc.StandardInput.WriteLine('')
    $want = [regex]::Escape("newest data disc: $label in drive ${Letter}:")
    $text = Wait-ForOutput -Path $OutFile -Pattern $want -Proc $Proc -Seconds 120
    if ($text -cnotmatch $want) { $Problems.Add("the script did not read the label $label in ${Letter}:") }
    return $newest
}

# #606 (F4): restore.bat's set menu.  Wait for it, check that it lists
# every set by the name restore.bat shows (name_cmd) and none by its id,
# and choose a set by its number.  The order is restore.bat's (its
# folders are named by UUID ids), so the number is read off the screen,
# as a reader would.  #607 (C7): a set whose password is split is listed
# as "<name_cmd> (password split: any K of N share cards)"; the K and N
# shown must be the set's own (sets.json "split", as its marker says),
# and a set that is not split has no such words.  -Choose: the set to
# pick (default: the one manifest.json is).
function Invoke-SetMenu {
    param(
        [System.Diagnostics.Process]$Proc,
        [string]$OutFile,
        $Sets,
        [System.Collections.Generic.List[string]]$Problems,
        $Choose = $null
    )
    if (-not $Choose) { $Choose = $Sets.restore }
    $prompt = 'Which one do you want to restore\? \[1\]: '
    $text = Wait-ForOutput -Path $OutFile -Pattern $prompt -Proc $Proc -Seconds 120
    if ($text -cnotmatch $prompt) { throw "no set menu; output so far:`n$text" }
    $start = $text.LastIndexOf('This archive contains more than one backup set:')
    if ($start -lt 0) { throw "no set menu heading; output so far:`n$text" }
    $entries = @{}
    $shownSplit = @{}
    $line = '(?m)^[ \t]*\[(\d+)\] ([^\r\n]+?)(?: \(password split: any (\d+) of (\d+) share cards\))?[ \t]*\r?$'
    foreach ($m in [regex]::Matches($text.Substring($start), $line)) {
        $label = $m.Groups[2].Value.Trim()
        $entries[$label] = $m.Groups[1].Value
        $shownSplit[$label] = if ($m.Groups[3].Success) { "$($m.Groups[3].Value)-of-$($m.Groups[4].Value)" } else { 'none' }
    }
    foreach ($e in $entries.GetEnumerator()) {
        Write-Host "set menu: [$($e.Value)] $($e.Key) (split: $($shownSplit[$e.Key]))"
    }
    $all = Get-AllSets $Sets
    if ($entries.Count -ne $all.Count) {
        $Problems.Add("the set menu lists $($entries.Count) sets; the archive has $($all.Count)")
    }
    foreach ($s in $all) {
        if (-not $entries.ContainsKey($s.name_cmd)) {
            $Problems.Add("the set menu does not list '$($s.name_cmd)' (#606)")
        } elseif ($s.split -and $shownSplit[$s.name_cmd] -ne $s.split) {
            $Problems.Add("the set menu shows '$($s.name_cmd)' as split $($shownSplit[$s.name_cmd]); its marker says $($s.split) (#607)")
        }
        if ($entries.ContainsKey($s.id)) { $Problems.Add("the set menu lists the id $($s.id), not '$($s.name_cmd)' (#606)") }
    }
    $pick = $entries[$Choose.name_cmd]
    if (-not $pick) { throw "the set menu has no '$($Choose.name_cmd)'; output so far:`n$text" }
    Write-Host "set menu: choosing [$pick] $($Choose.name_cmd)"
    $Proc.StandardInput.WriteLine($pick)
}

# Answer tier 1's disc boxes until the restore ends.
#
# How a disc box is spotted: tier 1 prints the box on stderr, which the
# cmd /c string sends to $OutFile.  Its first line is
# "| Pack <16 hex>... is required for the next file." and it ends with
# the bare prompt "> ".  One more line holding 'is required for the next
# file', with the file ending in "> ", is a new box.  The 16-hex pack
# prefix is looked up in $PackMap; that data ISO goes into $Letter (if it
# is not in already) and ENTER is pressed.  At box $QuitAt, q instead.
# Returns @{ Boxes; Swaps; Current; Inserted }: Inserted lists the data
# ISOs put into the drive at the boxes, in order (one per swap).
function Invoke-DiscBoxes {
    param(
        [System.Diagnostics.Process]$Proc,
        [string]$OutFile,
        [string]$Letter,
        [string]$Current,
        [string[]]$DataIsos,
        [hashtable]$PackMap,
        [int]$QuitAt = 0,
        [System.Collections.Generic.List[string]]$Problems
    )
    $boxes = 0
    $swaps = 0
    $seen = 0
    $inserted = New-Object 'System.Collections.Generic.List[string]'
    $maxBoxes = $DataIsos.Count + 2
    $deadline = (Get-Date).AddSeconds(900)
    while ((Get-Date) -lt $deadline) {
        $text = Read-SharedText $OutFile
        if ($Proc.HasExited) { break }
        if ($text -cmatch 'Restore complete' -or $text -cmatch 'The restore stopped') { break }
        $n = [regex]::Matches($text, 'is required for the next file').Count
        if ($n -le $seen -or -not $text.EndsWith('> ')) {
            Start-Sleep -Milliseconds 300
            continue
        }
        $seen = $n
        $boxes++
        if ($QuitAt -gt 0 -and $boxes -eq $QuitAt) {
            Write-Host "box ${boxes}: typing q, to stop the restore here"
            $Proc.StandardInput.WriteLine('q')
            continue
        }
        if ($boxes -gt $maxBoxes) {
            $Problems.Add("more than $maxBoxes disc boxes for $($DataIsos.Count) data discs; typed q")
            $Proc.StandardInput.WriteLine('q')
            continue
        }
        $packs = [regex]::Matches($text, '\| Pack ([0-9a-f]{16})\.\.\.')
        if ($packs.Count -eq 0) {
            $Problems.Add("box ${boxes} names no pack; typed q")
            $Proc.StandardInput.WriteLine('q')
            continue
        }
        $prefix = $packs[$packs.Count - 1].Groups[1].Value
        $next = $null
        foreach ($iso in $DataIsos) {
            if (@($PackMap[$iso] | Where-Object { $_.StartsWith($prefix) }).Count -gt 0) { $next = $iso }
        }
        if (-not $next) {
            $Problems.Add("box ${boxes} asks for pack $prefix, which is on no data ISO; typed q")
            $Proc.StandardInput.WriteLine('q')
            continue
        }
        if ($next -ne $Current) {
            if ($Current) {
                Switch-DiscInLetter -Letter $Letter -Current $Current -Iso $next
            } else {
                Insert-DiscInLetter -Letter $Letter -Iso $next
            }
            $Current = $next
            $swaps++
            $inserted.Add($next)
        }
        $isoName = Split-Path $next -Leaf
        Write-Host "box ${boxes}: pack $prefix is on $isoName, now in ${Letter}: (swaps so far: $swaps); ENTER"
        $Proc.StandardInput.WriteLine('')
    }
    return @{ Boxes = $boxes; Swaps = $swaps; Current = $Current; Inserted = @($inserted) }
}

# Put $Iso into the EMPTY drive letter $Letter (as Switch-DiscInLetter,
# with no disc to take out first).
function Insert-DiscInLetter {
    param(
        [string]$Letter,
        [string]$Iso
    )
    $img = Mount-DiskImage -ImagePath $Iso -NoDriveLetter -PassThru
    $vol = $null
    for ($i = 0; $i -lt 60; $i++) {
        $vol = Get-Volume -DiskImage $img -ErrorAction SilentlyContinue
        if ($vol -and $vol.Path) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not ($vol -and $vol.Path)) { throw "no volume for $Iso after Mount-DiskImage -NoDriveLetter" }
    Write-Host "mountvol ${Letter}: $($vol.Path)"
    & mountvol "${Letter}:" $vol.Path | Out-Host
    for ($i = 0; $i -lt 40; $i++) {
        if (Test-Path -LiteralPath "${Letter}:\data") { return }
        Start-Sleep -Milliseconds 500
    }
    throw "$Iso did not appear at ${Letter}: after mountvol"
}

# Wait for the end of a restore.bat run, closing stdin (which ends its
# final `pause`).  Returns the exit code, or 'n/a'.
function Complete-RestoreProcess {
    param(
        [System.Diagnostics.Process]$Proc,
        [System.Collections.Generic.List[string]]$Problems
    )
    try { $Proc.StandardInput.Close() } catch { Write-Host "stdin close: $($_.Exception.Message)" }
    if (-not $Proc.WaitForExit(120000)) {
        $Problems.Add('restore.bat did not exit within 120 s after its last box')
        & taskkill /PID $Proc.Id /T /F | Out-Null
    }
    if ($Proc.HasExited) { return $Proc.ExitCode }
    return 'n/a'
}

# C8 (#604, #617): swaps <= discs.  $Inserted lists every data ISO put
# into the one drive, in order: the newest-data-disc step's, then the
# boxes'.  With the pack cache on, tier 1 copies a data disc whole the
# first time it sees it ("copying disc <LABEL> to the cache": drain on
# first sight, D item 8), so a disc never has to go in twice.  So the
# swaps are at most the data discs, and no ISO is in the list twice.
function Assert-SwapsWithinDiscs {
    param(
        [string[]]$Inserted,
        [string[]]$DataIsos,
        [string]$What,
        [System.Collections.Generic.List[string]]$Problems
    )
    $Inserted = @($Inserted | Where-Object { $_ })
    if ($Inserted.Count -gt $DataIsos.Count) {
        $Problems.Add("$($Inserted.Count) swaps $What for $($DataIsos.Count) data discs (want at most $($DataIsos.Count): each disc goes in once, C8)")
    }
    foreach ($g in @($Inserted | Group-Object)) {
        if ($g.Count -gt 1) {
            $Problems.Add("$(Split-Path $g.Name -Leaf) went into the drive $($g.Count) times $What (the pack cache keeps a disc once it is read, C8)")
        }
    }
}

# #612 (C8): each data disc is copied into the pack cache exactly once.
function Assert-CopiedOnce {
    param(
        [string]$Out,
        [string[]]$DataIsos,
        [System.Collections.Generic.List[string]]$Problems
    )
    foreach ($iso in $DataIsos) {
        $label = [IO.Path]::GetFileNameWithoutExtension($iso)
        $n = [regex]::Matches($Out, 'copying disc ' + [regex]::Escape($label) + ' to the cache').Count
        if ($n -ne 1) { $Problems.Add("'copying disc $label to the cache' appears $n times (want exactly once, #612)") }
    }
}

# ---------------------------------------------------------------------
# Leg (#604, #617): ONE optical drive.  Only the meta ISO is mounted.
# restore.bat relocates (no LCSAS_NO_RELOCATE), and every data disc then
# goes into the meta disc's own drive letter, one at a time, as for a
# reader with one drive.  Up to rc5 restore.bat never gave tier 1 that
# letter, so this restore could not finish.
#
# stdin: first the newest-data-disc step (#605): the newest data ISO goes
# into the meta disc's letter and ENTER (Invoke-NewestDiscStep).  Then
# the set menu (#606): this is the leg with no LCSAS_REPO, so the menu
# must list each set by its name and the set is chosen by its number
# (Invoke-SetMenu).  Then restore.bat's two `set /p` prompts (folder,
# password) get one line each, written only once the prompt is in
# $outFile; then one empty line (ENTER) per box, after the swap
# (Invoke-DiscBoxes).  The catalog comes from the newest data disc, so
# the boxes name the disc they need.  stdin is closed after the final
# banner, which answers "another set?" with no and ends the `pause`.
# ---------------------------------------------------------------------
function Invoke-SingleDriveLeg {
    param(
        [string]$Fixture,
        [string]$BatRel,
        [string]$TargetName
    )
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false
    Remove-Item Env:LCSAS_NO_RELOCATE -ErrorAction SilentlyContinue
    Remove-Item Env:LCSAS_RELOCATED -ErrorAction SilentlyContinue
    # The reader picks the set from the menu.
    Remove-Item Env:LCSAS_REPO -ErrorAction SilentlyContinue
    $sets = Get-FixtureSets -Fixture $Fixture
    if (@(Get-AllSets $sets).Count -lt 2) { throw 'the fixture has one backup set: restore.bat shows no menu' }

    $isos = @(Get-ChildItem -Path $Fixture -Filter '*.iso')
    $metaIso = ($isos | Where-Object { $_.Name -eq 'LCSAS_META.iso' }).FullName
    if (-not $metaIso) { throw 'LCSAS_META.iso not found in the fixture dir' }
    $dataIsos = @($isos | Where-Object { $_.Name -ne 'LCSAS_META.iso' } | ForEach-Object { $_.FullName })
    if ($dataIsos.Count -lt 2) { throw "need >= 2 data ISOs for a swap; the fixture has $($dataIsos.Count)" }

    # An empty machine: nothing mounted.
    foreach ($iso in $isos) {
        Dismount-DiskImage -ImagePath $iso.FullName -ErrorAction SilentlyContinue | Out-Null
    }
    $packMap = Get-PackMap -DataIsos $dataIsos
    Clear-ToolsDir

    $img = Mount-DiskImage -ImagePath $metaIso -PassThru
    $letter = [string](Get-Volume -DiskImage $img).DriveLetter
    if (-not $letter) { throw 'the meta ISO got no drive letter' }
    Write-Host "Only the meta ISO is mounted, at ${letter}:"
    $bat = "${letter}:\$BatRel"
    if (-not (Test-Path -LiteralPath $bat)) { throw "no $bat on the meta disc" }

    $target = Join-Path $env:RUNNER_TEMP $TargetName
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $pw = (Get-Content (Join-Path $Fixture 'repo.pw') -Raw).Trim()
    $outFile = Join-Path $env:RUNNER_TEMP "out-$TargetName.txt"
    if (Test-Path -LiteralPath $outFile) { Remove-Item -Force -LiteralPath $outFile }
    $pwBefore  = Get-TempNames 'lcsas-pw-*'

    $problems = New-Object 'System.Collections.Generic.List[string]'
    $proc = $null
    $result = @{ Boxes = 0; Swaps = 0; Current = $metaIso; Inserted = @() }
    $stepSwaps = 0
    $stepIso = $null
    $rc = 'n/a'
    try {
        Write-Host "Starting (no LCSAS_NO_RELOCATE, LCSAS_INTERACTIVE=on): $bat"
        $proc = Start-RestoreProcess -Bat $bat -OutFile $outFile -WorkDir "${letter}:\"

        $current = Invoke-NewestDiscStep -Proc $proc -OutFile $outFile -Letter $letter -Current $metaIso `
            -DataIsos $dataIsos -Problems $problems
        $result.Current = $current
        $stepSwaps = 1
        $stepIso = $current

        Invoke-SetMenu -Proc $proc -OutFile $outFile -Sets $sets -Problems $problems

        $text = Wait-ForOutput -Path $outFile -Pattern 'Restore to which folder\?' -Proc $proc -Seconds 300
        if ($text -cnotmatch 'Restore to which folder\?') { throw "no folder prompt; output so far:`n$text" }
        $proc.StandardInput.WriteLine($target)
        $text = Wait-ForOutput -Path $outFile -Pattern 'Password: ' -Proc $proc -Seconds 120
        if ($text -cnotmatch 'Password: ') { throw "no password prompt; output so far:`n$text" }
        $proc.StandardInput.WriteLine($pw)

        $result = Invoke-DiscBoxes -Proc $proc -OutFile $outFile -Letter $letter -Current $current `
            -DataIsos $dataIsos -PackMap $packMap -Problems $problems
        $rc = Complete-RestoreProcess -Proc $proc -Problems $problems
    } finally {
        if ($proc -and -not $proc.HasExited) { & taskkill /PID $proc.Id /T /F | Out-Null }
    }
    $boxes = $result.Boxes
    # The step's swap (the newest data disc in) counts too: C8 counts
    # every time a disc goes into the drive.
    $swaps = $result.Swaps + $stepSwaps
    $inserted = @(@($stepIso) + @($result.Inserted) | Where-Object { $_ })
    $out = Read-SharedText $outFile
    Write-Host '----- restore.bat output -----'
    Write-Host $out
    Write-Host '----- end output -----'
    Write-Host "restore.bat exit code: $rc; disc boxes: $boxes; swaps: $swaps; data discs: $($dataIsos.Count)"

    if ($out -notmatch 'copied recovery files to') {
        $problems.Add("no 'copied recovery files to' message: the relocation did not happen")
    }
    if ("$rc" -ne '0') { $problems.Add("restore.bat exit code $rc (want 0)") }
    if ($out -cnotmatch 'Restore complete') { $problems.Add("'Restore complete' banner missing") }
    if ($result.Swaps -lt 1) { $problems.Add('no disc was swapped at a box: the leg proved nothing') }
    # C8 (#604, #617): swaps <= discs, the step's swap INCLUDED (this
    # allowed discs + 1 before).  The step puts the newest data disc in,
    # and tier 1 copies it whole into the pack cache at launch; each box
    # after that asks for a disc not read yet, which is copied whole when
    # it goes in.  So every data disc goes in at most once: at most
    # discs - 1 swaps at the boxes, plus the step's.  (windows-e2e on
    # 130baf4 and f9e7a57: 4 data discs, 1 + 3 = 4 swaps, 3 boxes.)
    # Boxes are bounded by the discs too, not discs - 1: a box shown again
    # for the disc already in (a slow drive) costs no swap.
    if ($boxes -gt $dataIsos.Count) {
        $problems.Add("$boxes disc boxes for $($dataIsos.Count) data discs (want at most $($dataIsos.Count))")
    }
    if ($swaps -ne $inserted.Count) { $problems.Add("swap count $swaps, but $($inserted.Count) discs went in") }
    Assert-SwapsWithinDiscs -Inserted $inserted -DataIsos $dataIsos -What 'with the step' -Problems $problems
    # #612 (C8): with the newest data disc read first, every data disc is
    # copied into the cache once, the first time it is in the drive.
    Assert-CopiedOnce -Out $out -DataIsos $dataIsos -Problems $problems
    # #605: the catalog copy is the newest disc's, so the first box names
    # the disc it needs.
    if ($out -match 'no disc catalog yet') { $problems.Add('a box had no catalog although the newest data disc was read first') }
    if ($out -match '/Volumes') { $problems.Add('a disc box lists the POSIX folder /Volumes on Windows') }
    if ($out -match 'trying tier 2') { $problems.Add('fell through to tier 2') }
    # #611: the relocation's write probe printed "Access is denied." on
    # every start from a disc (F1).
    Assert-NoAccessDenied -Out $out -Problems $problems
    Assert-OnlyThisPlatform -Out $out -Problems $problems
    Assert-SetNamed -Out $out -Sets $sets -Problems $problems
    Test-TreeAgainstManifest -Fixture $Fixture -Target $target -Problems $problems

    $pwLeft = @(Get-TempNames 'lcsas-pw-*' | Where-Object { $pwBefore -notcontains $_ })
    if ($pwLeft.Count -gt 0) { $problems.Add("password file(s) left in %TEMP%: $($pwLeft -join ', ')") }
    Assert-ToolsDirGone -Problems $problems

    # Leave no image mounted for the steps after this one.
    foreach ($iso in $isos) {
        Dismount-DiskImage -ImagePath $iso.FullName -ErrorAction SilentlyContinue | Out-Null
    }

    if ($problems.Count -gt 0) {
        throw ("single-drive leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host "Single-drive leg passed: $swaps swap(s) in ${letter}:, $boxes box(es), tree byte-exact."
}

# ---------------------------------------------------------------------
# Leg (#609, #615, C5): stop, and carry on later without the META disc.
# ONE optical drive, as in the single-drive leg.
#
# 1. restore.bat from the META disc; at the first box the named data disc
#    goes into the drive, and at the SECOND box the reader types q.  Tier 1
#    exits 75 ("paused by the reader"), which restore.bat hands back.  The
#    tools folder stays, with resume.cfg (no password) and "Resume LCSAS
#    restore.bat" in it and on the Desktop, and the banner names it.
# 2. The META disc is no longer in the drive: its letter holds a data
#    disc.  The Desktop launcher carries on: it asks only the password
#    (the folder is in resume.cfg), the boxes are answered as before, and
#    the restore finishes.  Exit 0, tree byte-exact, the tools folder and
#    the Desktop launcher gone.
# ---------------------------------------------------------------------
function Invoke-StopAndResumeLeg {
    param(
        [string]$Fixture,
        [string]$BatRel,
        [string]$TargetName
    )
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false
    Remove-Item Env:LCSAS_NO_RELOCATE -ErrorAction SilentlyContinue
    Remove-Item Env:LCSAS_RELOCATED -ErrorAction SilentlyContinue

    $isos = @(Get-ChildItem -Path $Fixture -Filter '*.iso')
    $metaIso = ($isos | Where-Object { $_.Name -eq 'LCSAS_META.iso' }).FullName
    if (-not $metaIso) { throw 'LCSAS_META.iso not found in the fixture dir' }
    $dataIsos = @($isos | Where-Object { $_.Name -ne 'LCSAS_META.iso' } | ForEach-Object { $_.FullName })
    if ($dataIsos.Count -lt 2) { throw "need >= 2 data ISOs to stop between two; the fixture has $($dataIsos.Count)" }
    # The set is named by its id (LCSAS_REPO), so no menu; carrying on
    # takes the set from resume.cfg.
    Remove-Item Env:LCSAS_REPO -ErrorAction SilentlyContinue
    $sets = Get-FixtureSets -Fixture $Fixture

    foreach ($iso in $isos) {
        Dismount-DiskImage -ImagePath $iso.FullName -ErrorAction SilentlyContinue | Out-Null
    }
    $packMap = Get-PackMap -DataIsos $dataIsos
    Clear-ToolsDir
    $tools = Get-ToolsDir
    $launcher = Join-Path $tools 'Resume LCSAS restore.bat'

    $img = Mount-DiskImage -ImagePath $metaIso -PassThru
    $letter = [string](Get-Volume -DiskImage $img).DriveLetter
    if (-not $letter) { throw 'the meta ISO got no drive letter' }
    $bat = "${letter}:\$BatRel"
    if (-not (Test-Path -LiteralPath $bat)) { throw "no $bat on the meta disc" }

    $target = Join-Path $env:RUNNER_TEMP $TargetName
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $pw = (Get-Content (Join-Path $Fixture 'repo.pw') -Raw).Trim()
    $outStop = Join-Path $env:RUNNER_TEMP "out-$TargetName-stop.txt"
    $outResume = Join-Path $env:RUNNER_TEMP "out-$TargetName-resume.txt"
    foreach ($f in @($outStop, $outResume)) {
        if (Test-Path -LiteralPath $f) { Remove-Item -Force -LiteralPath $f }
    }
    $pwBefore  = Get-TempNames 'lcsas-pw-*'

    $problems = New-Object 'System.Collections.Generic.List[string]'
    $proc = $null
    $current = $metaIso
    $rcStop = 'n/a'
    $rcResume = 'n/a'
    $stop = @{ Boxes = 0; Swaps = 0; Current = $metaIso; Inserted = @() }
    $resume = @{ Boxes = 0; Swaps = 0; Current = $metaIso; Inserted = @() }
    try {
        # --- 1. run from the META disc, stop at the second box ---------
        # The newest-data-disc step is skipped here (C4): this leg counts
        # boxes from the META disc on, and Invoke-SingleDriveLeg runs it.
        Write-Host "Starting (one drive, q at the second box): $bat"
        $proc = Start-RestoreProcess -Bat $bat -OutFile $outStop -WorkDir "${letter}:\" `
            -Env @{ LCSAS_NEWEST_DISC_PROMPT = '0'; LCSAS_REPO = $sets.restore.id }
        $text = Wait-ForOutput -Path $outStop -Pattern 'Restore to which folder\?' -Proc $proc -Seconds 300
        if ($text -cnotmatch 'Restore to which folder\?') { throw "no folder prompt; output so far:`n$text" }
        $proc.StandardInput.WriteLine($target)
        $text = Wait-ForOutput -Path $outStop -Pattern 'Password: ' -Proc $proc -Seconds 120
        if ($text -cnotmatch 'Password: ') { throw "no password prompt; output so far:`n$text" }
        $proc.StandardInput.WriteLine($pw)
        $stop = Invoke-DiscBoxes -Proc $proc -OutFile $outStop -Letter $letter -Current $current `
            -DataIsos $dataIsos -PackMap $packMap -QuitAt 2 -Problems $problems
        $current = $stop.Current
        $rcStop = Complete-RestoreProcess -Proc $proc -Problems $problems
        $proc = $null
        $out = Read-SharedText $outStop
        Write-Host '----- restore.bat output (stop) -----'
        Write-Host $out
        Write-Host '----- end output -----'
        Write-Host "exit code after q: $rcStop; boxes: $($stop.Boxes); swaps: $($stop.Swaps)"

        if ("$rcStop" -ne '75') { $problems.Add("exit code after q at a box is $rcStop (want 75: paused by the reader)") }
        if ($out -notmatch 'The restore stopped before it finished \(code 75\)') { $problems.Add('no stop banner with code 75') }
        if ($out -notmatch [regex]::Escape("`"$launcher`"")) { $problems.Add("the stop banner does not name $launcher") }
        if ($out -match 'trying tier 2|\[tier 2\]|no working recovery method') { $problems.Add('fell through to tier 2 after q') }
        if ($current -eq $metaIso) { $problems.Add('no data disc went in before the stop') }
        Assert-NoAccessDenied -Out $out -Problems $problems
        Assert-SetNamed -Out $out -Sets $sets -Problems $problems
        Assert-OnlyThisPlatform -Out $out -Problems $problems -CheckFolder
        Assert-ToolsDirKept -Problems $problems -Password $pw
        # D item 6 (C8): tier 1 writes each pack into the cache as
        # <pack>.part and renames it only when it is whole, so a stop
        # leaves no partial pack that the run carrying on would trust.
        # Checked now, while the tools folder is kept: a finished restore
        # removes the cache.  The disc that went in before the stop was
        # copied into it, so an empty cache means the check proved nothing.
        $cache = Join-Path $tools 'pack-cache'
        $cached = @(Get-ChildItem -LiteralPath $cache -Recurse -File -Force -ErrorAction SilentlyContinue)
        Write-Host "pack cache after the stop: $($cached.Count) file(s) in $cache"
        if ($cached.Count -eq 0) {
            $problems.Add("the pack cache $cache is empty after the stop: the disc read before it was not copied in")
        }
        $parts = @($cached | Where-Object { $_.Name -like '*.part' })
        if ($parts.Count -gt 0) {
            $problems.Add("partial pack file(s) left in the pack cache after the stop: $($parts.FullName -join ', ')")
        }

        # --- 2. carry on with the Desktop launcher, no META disc --------
        $desk = Get-DesktopLauncher
        if (-not (Test-Path -LiteralPath $desk)) { throw "no launcher on the Desktop: $desk" }
        Write-Host "Carrying on with $desk; ${letter}: holds $(Split-Path $current -Leaf), not the META disc"
        $proc = Start-RestoreProcess -Bat $desk -OutFile $outResume -WorkDir $env:USERPROFILE
        $text = Wait-ForOutput -Path $outResume -Pattern 'Password: ' -Proc $proc -Seconds 120
        if ($text -cnotmatch 'Password: ') { throw "no password prompt on resume; output so far:`n$text" }
        $proc.StandardInput.WriteLine($pw)
        $resume = Invoke-DiscBoxes -Proc $proc -OutFile $outResume -Letter $letter -Current $current `
            -DataIsos $dataIsos -PackMap $packMap -Problems $problems
        $rcResume = Complete-RestoreProcess -Proc $proc -Problems $problems
    } finally {
        if ($proc -and -not $proc.HasExited) { & taskkill /PID $proc.Id /T /F | Out-Null }
    }
    $out = Read-SharedText $outResume
    Write-Host '----- launcher output (carry on) -----'
    Write-Host $out
    Write-Host '----- end output -----'
    Write-Host "exit code after carrying on: $rcResume; boxes: $($resume.Boxes); swaps: $($resume.Swaps)"

    if ("$rcResume" -ne '0') { $problems.Add("exit code after carrying on is $rcResume (want 0)") }
    if ($out -cnotmatch 'Restore complete') { $problems.Add("'Restore complete' banner missing after carrying on") }
    if ($out -match 'Restore to which folder\?') { $problems.Add('carrying on asked for the folder again') }
    if ($out -notmatch 'Carrying on the restore into:') { $problems.Add('carrying on did not name the folder') }
    if ($out -match 'copied recovery files to') { $problems.Add('carrying on copied the tools again') }
    if ($out -match 'trying tier 2') { $problems.Add('fell through to tier 2 after carrying on') }
    # resume.cfg holds the set's id; the banner still shows its name.
    Assert-SetNamed -Out $out -Sets $sets -Problems $problems
    # C8: swaps <= discs over both runs (this allowed discs + 1 before).
    # The stopped run's pack cache is kept for the launcher's run, so a
    # disc read before the stop never goes in again; the box where q was
    # typed swapped nothing.  (windows-e2e on 130baf4 and f9e7a57: 1 + 3 =
    # 4 swaps for 4 data discs.)
    $total = $stop.Swaps + $resume.Swaps
    Assert-SwapsWithinDiscs -Inserted (@($stop.Inserted) + @($resume.Inserted)) -DataIsos $dataIsos `
        -What 'over the stop and the carrying on' -Problems $problems
    Assert-NoAccessDenied -Out $out -Problems $problems
    Test-TreeAgainstManifest -Fixture $Fixture -Target $target -Problems $problems
    $pwLeft = @(Get-TempNames 'lcsas-pw-*' | Where-Object { $pwBefore -notcontains $_ })
    if ($pwLeft.Count -gt 0) { $problems.Add("password file(s) left in %TEMP%: $($pwLeft -join ', ')") }
    Assert-ToolsDirGone -Problems $problems

    foreach ($iso in $isos) {
        Dismount-DiskImage -ImagePath $iso.FullName -ErrorAction SilentlyContinue | Out-Null
    }
    if ($problems.Count -gt 0) {
        throw ("stop-and-resume leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host "Stop-and-resume leg passed: exit 75 kept the tools folder; the launcher finished the restore with no META disc ($total swaps)."
}

# Every ISO of the fixture mounted, each at a drive letter of its own (the
# layout of the first legs), whatever the leg before left.  Returns the
# META disc's letter.
function Mount-FixtureIsos {
    param([string]$Fixture)
    $isos = @(Get-ChildItem -Path $Fixture -Filter '*.iso')
    foreach ($iso in $isos) {
        Dismount-DiskImage -ImagePath $iso.FullName -ErrorAction SilentlyContinue | Out-Null
    }
    foreach ($iso in $isos) { Mount-DiskImage -ImagePath $iso.FullName | Out-Null }
    for ($i = 0; $i -lt 60; $i++) {
        $meta = Find-MetaDrive
        if ($meta -and @(Get-DataDiscs).Count -eq ($isos.Count - 1)) { return $meta }
        Start-Sleep -Milliseconds 500
    }
    throw "after mounting $($isos.Count) ISOs: META disc at '$(Find-MetaDrive)', $(@(Get-DataDiscs).Count) data discs"
}

# ---------------------------------------------------------------------
# G1 (#608): the documented way to type the password hidden.
#
# RECOVER_WINDOWS.txt, "THE PASSWORD", gives PowerShell lines that read
# the password with Read-Host -AsSecureString, turn it back into text
# (SecureStringToBSTR + PtrToStringBSTR, which Windows PowerShell 5.1
# has; ConvertFrom-SecureString -AsPlainText needs PowerShell 7), write
# it to $env:TEMP\lcsas-pw.txt with no BOM and no line break, set
# LCSAS_PWFILE to that file, run restore.bat and delete the file.
#
# The doc has no markers around the lines, and this PR changes no doc
# text, so Get-DocPasswordLines finds them by what they are: in the
# section headed "THE PASSWORD", the indented block that starts with the
# Read-Host -AsSecureString line and ends with "Remove-Item $f".  It
# throws, naming what it missed, when the heading, the block or any of
# its lines is not there: a reworded doc must be looked at again, never
# skipped.  The lines are read from the META disc, the copy a reader has.
# ---------------------------------------------------------------------
function Get-DocPasswordLines {
    param([string]$DocPath)
    if (-not (Test-Path -LiteralPath $DocPath)) { throw "no $DocPath" }
    $lines = @([IO.File]::ReadAllText($DocPath, [Text.Encoding]::UTF8) -split "`r?`n")
    $heads = @(for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -ceq 'THE PASSWORD') { $i } })
    if ($heads.Count -ne 1) { throw "$DocPath has $($heads.Count) lines 'THE PASSWORD' (want 1)" }
    # The section runs to the next rule line (box-drawing dashes, U+2500)
    # after the heading's own two.
    $end = $lines.Count
    for ($i = $heads[0] + 2; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\u2500{10,}\s*$') { $end = $i; break }
    }
    $starts = @(for ($i = $heads[0]; $i -lt $end; $i++) {
        if ($lines[$i] -cmatch '^ {4}\$s = Read-Host -AsSecureString\b') { $i }
    })
    if ($starts.Count -ne 1) {
        throw "$DocPath, THE PASSWORD: $($starts.Count) lines '    `$s = Read-Host -AsSecureString' (want 1): the snippet's start marker"
    }
    $block = New-Object 'System.Collections.Generic.List[string]'
    for ($i = $starts[0]; $i -lt $end -and $lines[$i] -match '^ {4}\S'; $i++) {
        $block.Add($lines[$i].Substring(4).TrimEnd())
    }
    $want = [ordered]@{
        'the password read hidden'      = '^\$s = Read-Host -AsSecureString\b'
        'SecureStringToBSTR'            = '^\$b = \[Runtime\.InteropServices\.Marshal\]::SecureStringToBSTR\(\$s\)$'
        'PtrToStringBSTR'               = '^\$p = \[Runtime\.InteropServices\.Marshal\]::PtrToStringBSTR\(\$b\)$'
        'the file in %TEMP%'            = '^\$f = "\$env:TEMP\\[^"\\]+"$'
        'UTF8Encoding($false)'          = '^\$u = New-Object Text\.UTF8Encoding\(\$false\)$'
        'WriteAllText'                  = '^\[IO\.File\]::WriteAllText\(\$f, \$p, \$u\)$'
        'LCSAS_PWFILE = $f'             = '^\$env:LCSAS_PWFILE = \$f$'
        'X:\restore.bat'                = '^[A-Za-z]:\\restore\.bat$'
        'Remove-Item $f (the end marker)' = '^Remove-Item \$f$'
    }
    $at = [ordered]@{}
    $missing = @()
    foreach ($k in $want.Keys) {
        $hits = @(for ($i = 0; $i -lt $block.Count; $i++) { if ($block[$i] -cmatch $want[$k]) { $i } })
        if ($hits.Count -ne 1) { $missing += "$k ($($hits.Count) lines)" } else { $at[$k] = $hits[0] }
    }
    if ($missing.Count -gt 0) {
        throw "$DocPath, THE PASSWORD: the PowerShell lines changed; not found once: $($missing -join '; ')`n$($block -join "`n")"
    }
    $order = @($at['WriteAllText'], $at['LCSAS_PWFILE = $f'], $at['X:\restore.bat'], $at['Remove-Item $f (the end marker)'])
    if ($at['Remove-Item $f (the end marker)'] -ne $block.Count - 1 -or
        (Compare-Object $order ($order | Sort-Object) -SyncWindow 0)) {
        throw "$DocPath, THE PASSWORD: the lines are not in the order write, LCSAS_PWFILE, restore.bat, Remove-Item:`n$($block -join "`n")"
    }
    foreach ($l in $block) {
        if ($l -match '[^\x20-\x7e]') { throw "$DocPath, THE PASSWORD: a line is not plain ASCII: $l" }
    }
    return [pscustomobject]@{
        Lines    = @($block)
        ReadHost = $at['the password read hidden']
        PwFile   = $at['LCSAS_PWFILE = $f']
        Restore  = $at['X:\restore.bat']
    }
}

# The doc's lines up to LCSAS_PWFILE, made into a script that runs with
# no keyboard: ONLY the Read-Host -AsSecureString source on the first
# line is replaced, by ConvertTo-SecureString on the password the leg
# puts in the environment (LCSAS_E2E_DOC_PW).  The doc's next lines run
# restore.bat and delete the file; the leg does both itself, to check the
# file between them.  The script ends by writing what it made to
# $env:LCSAS_E2E_DOC_RESULT, with the PowerShell version that ran it.
function New-DocPasswordScript {
    param($Doc, [string]$Path)
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $lines.Add('$ErrorActionPreference = ''Stop''')
    for ($i = 0; $i -le $Doc.PwFile; $i++) {
        $l = $Doc.Lines[$i]
        if ($i -eq $Doc.ReadHost) {
            # "$$" is one "$" in a .NET replacement string.
            $source = 'Read-Host -AsSecureString( -Prompt ("[^"]*"|''[^'']*''|\S+))?$'
            $plain = 'ConvertTo-SecureString -String $$env:LCSAS_E2E_DOC_PW -AsPlainText -Force'
            $new = [regex]::Replace($l, $source, $plain)
            if ($new -eq $l -or $new -notmatch '^\$s = ConvertTo-SecureString ') {
                throw "could not replace the Read-Host source in: $l"
            }
            Write-Host "doc line: $l"
            Write-Host "  run as: $new"
            $l = $new
        } else {
            Write-Host "doc line: $l"
        }
        $lines.Add($l)
    }
    $lines.Add('$r = [ordered]@{ ps = "$($PSVersionTable.PSVersion)"; edition = "$($PSVersionTable.PSEdition)"; f = $f; pwfile = $env:LCSAS_PWFILE }')
    $lines.Add('[IO.File]::WriteAllText($env:LCSAS_E2E_DOC_RESULT, (ConvertTo-Json -InputObject $r), (New-Object Text.UTF8Encoding($false)))')
    # ASCII: Windows PowerShell 5.1 reads a script with no BOM in the
    # ANSI code page.
    [IO.File]::WriteAllLines($Path, [string[]]$lines, [Text.Encoding]::ASCII)
}

# ---------------------------------------------------------------------
# Leg (G1, #608): the hidden-password route of RECOVER_WINDOWS.txt, on
# real Windows.
#   1. The doc's PowerShell lines, from the META disc, run under Windows
#      PowerShell 5.1 (powershell.exe, not pwsh) with only the Read-Host
#      source replaced.  They must write the password, byte for byte (no
#      BOM, no line break), to $env:TEMP\lcsas-pw.txt and set
#      LCSAS_PWFILE to it.
#   2. restore.bat from the META disc, as the doc's next line runs it (cwd
#      the user's folder, so it copies itself to the tools folder), with
#      that LCSAS_PWFILE and the set named in LCSAS_REPO.  It must restore
#      byte-exact with no password question, leave the reader's file as
#      it was (its first pass deletes %TEMP%\lcsas-pw-*.txt, which
#      lcsas-pw.txt must not match), and leave no lcsas-pw-* of its own.
# ---------------------------------------------------------------------
function Invoke-HiddenPasswordLeg {
    param(
        [string]$Fixture,
        [string]$BatRel,
        [string]$TargetName
    )
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false
    foreach ($v in @('LCSAS_NO_RELOCATE', 'LCSAS_RELOCATED', 'LCSAS_PWFILE', 'LCSAS_PASSWORD', 'LCSAS_PW', 'LCSAS_SHARES_DIR')) {
        Remove-Item "Env:$v" -ErrorAction SilentlyContinue
    }
    $sets = Get-FixtureSets -Fixture $Fixture
    $meta = Mount-FixtureIsos -Fixture $Fixture
    $bat = "${meta}:\$BatRel"
    if (-not (Test-Path -LiteralPath $bat)) { throw "no $bat on the meta disc" }
    $docPath = "${meta}:\recovery\docs\RECOVER_WINDOWS.txt"
    $doc = Get-DocPasswordLines -DocPath $docPath
    Write-Host "RECOVER_WINDOWS.txt on the META disc: the PowerShell lines of THE PASSWORD, $($doc.Lines.Count) lines"

    $pw = (Get-Content (Join-Path $Fixture 'repo.pw') -Raw).Trim()
    $docFile = Join-Path $env:TEMP 'lcsas-pw.txt'
    if (Test-Path -LiteralPath $docFile) { Remove-Item -Force -LiteralPath $docFile }
    $script = Join-Path $env:RUNNER_TEMP 'doc-password.ps1'
    $resultFile = Join-Path $env:RUNNER_TEMP 'doc-password.json'
    if (Test-Path -LiteralPath $resultFile) { Remove-Item -Force -LiteralPath $resultFile }
    New-DocPasswordScript -Doc $doc -Path $script

    $problems = New-Object 'System.Collections.Generic.List[string]'
    $ps51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $ps51)) { throw "no Windows PowerShell 5.1 at $ps51" }
    $env:LCSAS_E2E_DOC_PW = $pw
    $env:LCSAS_E2E_DOC_RESULT = $resultFile
    try {
        & $ps51 -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $script
        $psRc = $LASTEXITCODE
    } finally {
        Remove-Item Env:LCSAS_E2E_DOC_PW -ErrorAction SilentlyContinue
        Remove-Item Env:LCSAS_E2E_DOC_RESULT -ErrorAction SilentlyContinue
    }
    if ($psRc -ne 0 -or -not (Test-Path -LiteralPath $resultFile)) {
        throw "the doc's PowerShell lines failed under powershell.exe (exit $psRc)"
    }
    $made = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
    Write-Host "powershell.exe $($made.ps) ($($made.edition)) wrote $($made.f); LCSAS_PWFILE=$($made.pwfile)"
    if (-not ($made.ps -like '5.1*') -or $made.edition -ne 'Desktop') {
        $problems.Add("the doc's lines ran under PowerShell $($made.ps) $($made.edition), not Windows PowerShell 5.1")
    }
    if ($made.f -ne $docFile) { $problems.Add("the doc's lines wrote $($made.f), not $docFile") }
    if ($made.pwfile -ne $made.f) { $problems.Add("the doc's lines set LCSAS_PWFILE to $($made.pwfile), not to the file $($made.f)") }
    $want = [Text.Encoding]::ASCII.GetBytes($pw)
    $before = if (Test-Path -LiteralPath $docFile) { [IO.File]::ReadAllBytes($docFile) } else { $null }
    if (-not $before) {
        throw "the doc's lines left no $docFile"
    }
    if ([Convert]::ToBase64String($before) -ne [Convert]::ToBase64String($want)) {
        $problems.Add("$docFile holds $($before.Length) bytes, not the $($want.Length) bytes of the password alone (a BOM or a line break?)")
    }

    Clear-ToolsDir
    $env:LCSAS_REPO = $sets.restore.id
    $env:LCSAS_PWFILE = $made.pwfile
    $target = Join-Path $env:RUNNER_TEMP $TargetName
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    # The folder, then ENTERs: "another set?" (no) and the last pause.  No
    # password: a question for it reads these empty lines and gives up.
    $answers = Join-Path $env:RUNNER_TEMP "answers-$TargetName.txt"
    @($target, '', '', '', '', '', '', '') | Set-Content -Path $answers -Encoding ascii
    $outFile = Join-Path $env:RUNNER_TEMP "out-$TargetName.txt"
    $pwBefore = Get-TempNames 'lcsas-pw-*'
    Write-Host "Running (cwd $env:USERPROFILE, LCSAS_PWFILE=$($env:LCSAS_PWFILE), LCSAS_REPO=$($env:LCSAS_REPO)): $bat"
    Push-Location -LiteralPath $env:USERPROFILE
    try {
        cmd /c "`"$bat`" < `"$answers`" > `"$outFile`" 2>&1"
        $rc = $LASTEXITCODE
    } finally {
        Pop-Location
        Remove-Item Env:LCSAS_PWFILE -ErrorAction SilentlyContinue
    }
    $out = Get-Content $outFile -Raw
    Write-Host '----- restore.bat output -----'
    Write-Host $out
    Write-Host '----- end output -----'
    Write-Host "restore.bat exit code: $rc"

    if ($rc -ne 0) { $problems.Add("restore.bat exit code $rc (want 0)") }
    if ($out -cnotmatch 'Restore complete') { $problems.Add("'Restore complete' banner missing") }
    if ($out -notmatch 'using the password in the file LCSAS_PWFILE names') {
        $problems.Add('restore.bat did not take the password from LCSAS_PWFILE')
    }
    if ($out -cmatch 'Password: ') { $problems.Add('restore.bat asked for the password although LCSAS_PWFILE gave it') }
    if ($out -match 'That password is wrong|cannot read the password file') {
        $problems.Add("the password in the doc's file did not open the set")
    }
    if ($out -notmatch 'copied recovery files to') { $problems.Add("no 'copied recovery files to' message: the relocation did not happen") }
    if ($out -match 'trying tier 2') { $problems.Add('fell through to tier 2') }
    Assert-NoAccessDenied -Out $out -Problems $problems
    Assert-SetNamed -Out $out -Sets $sets -Problems $problems
    Test-TreeAgainstManifest -Fixture $Fixture -Target $target -Problems $problems
    # The reader's file is theirs: restore.bat copies it and deletes only
    # its copy.
    if (-not (Test-Path -LiteralPath $docFile)) {
        $problems.Add("restore.bat deleted the reader's password file $docFile")
    } elseif ([Convert]::ToBase64String([IO.File]::ReadAllBytes($docFile)) -ne [Convert]::ToBase64String($before)) {
        $problems.Add("restore.bat changed the reader's password file $docFile")
    }
    $pwLeft = @(Get-TempNames 'lcsas-pw-*' | Where-Object { $pwBefore -notcontains $_ })
    if ($pwLeft.Count -gt 0) { $problems.Add("password file(s) left in %TEMP%: $($pwLeft -join ', ')") }
    Assert-ToolsDirGone -Problems $problems
    # The doc's last line.
    Remove-Item -Force -LiteralPath $docFile -ErrorAction SilentlyContinue

    if ($problems.Count -gt 0) {
        throw ("hidden-password leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host "Hidden-password leg passed: the doc's lines under PowerShell $($made.ps) wrote the password exactly; restore.bat used it, restored byte-exact and kept the reader's file."
}

# ---------------------------------------------------------------------
# G3 (#607, F6): share cards typed into restore.bat on real Windows.
# ---------------------------------------------------------------------

# The share words of a printed card (`lcsas key split`'s -card.txt): the
# line after "THE SHARE WORDS".
function Get-CardWords {
    param([string]$Path)
    $lines = @([IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) -split "`r?`n")
    for ($i = 0; $i -lt $lines.Count - 1; $i++) {
        if ($lines[$i] -cmatch '^THE SHARE WORDS') {
            $words = @($lines[$i + 1].Trim() -split '\s+' | Where-Object { $_ })
            if ($words.Count -lt 20) { throw "$Path`: $($words.Count) share words after THE SHARE WORDS (a card has 20 or more)" }
            return ($words -join ' ')
        }
    }
    throw "$Path`: no THE SHARE WORDS line: not a share card"
}

# Every run of three words in a row from these cards.  A card kept on
# disk shows up as its words in a row; single share words are everyday
# English and turn up anywhere by chance (#617), so never look for one.
function Get-WordRuns {
    param([string[]]$Cards)
    $runs = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($c in $Cards) {
        $w = @($c -split ' ')
        for ($i = 0; $i -le $w.Count - 3; $i++) { [void]$runs.Add(($w[$i..($i + 2)] -join ' ')) }
    }
    return @($runs)
}

# The files under $Dir that hold any of $Runs (read as bytes, so the
# programs and the encrypted packs are searched too).
function Find-CardText {
    param(
        [string]$Dir,
        [string[]]$Runs
    )
    $hits = @()
    foreach ($f in @(Get-ChildItem -LiteralPath $Dir -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        try { $text = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($f.FullName)) }
        catch { Write-Host "cannot read $($f.FullName): $($_.Exception.Message)"; continue }
        foreach ($r in $Runs) {
            if ($text.Contains($r)) { $hits += $f.FullName; break }
        }
    }
    return $hits
}

# ---------------------------------------------------------------------
# Leg (G3, #607, F6): the set whose password is split 2-of-3, restored on
# ONE drive by typing two of its printed cards, as in the drill.  Like
# the single-drive leg: only the META disc is in at the start, the
# newest data disc goes into its letter at the newest-data-disc step, and
# each box gets the disc it names in that letter.
#   * The menu lists the set with "(password split: any 2 of 3 share
#     cards)" (Invoke-SetMenu checks it) and it is chosen from there.
#   * At "Words of card 1: " card 1 with ONE word changed to a word that
#     is no share word: lcsas-keyshare.exe names it and card 1 is asked
#     again.  Then card 1 right, and card 3 at "Words of card 2: ".
#   * The cards rebuild the password; "Password: " is never asked.
#   * At "Restore another backup set from these discs? [y/N]" (the tools
#     folder is still there): no three card words in a row in any file of
#     the tools folder, and no lcsas-shares-* folder in %TEMP%.  Then N.
#   * Exit 0, the set's tree byte-exact, nothing left in %TEMP%
#     (lcsas-shares-*, lcsas-pw-*), the tools folder gone, swaps <= discs.
# The card words are never written to the log.
# ---------------------------------------------------------------------
function Invoke-SplitCardsLeg {
    param(
        [string]$Fixture,
        [string]$BatRel,
        [string]$TargetName
    )
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false
    foreach ($v in @('LCSAS_NO_RELOCATE', 'LCSAS_RELOCATED', 'LCSAS_REPO', 'LCSAS_PWFILE', 'LCSAS_PASSWORD', 'LCSAS_PW', 'LCSAS_SHARES_DIR')) {
        Remove-Item "Env:$v" -ErrorAction SilentlyContinue
    }
    $sets = Get-FixtureSets -Fixture $Fixture
    $work = Get-SplitSet $sets
    $kn = [regex]::Match([string]$work.split, '^2-of-(\d+)$')
    if (-not $kn.Success -or [int]$kn.Groups[1].Value -lt 3 -or @($work.cards).Count -lt 3) {
        throw "the leg types cards 1 and 3 of a 2-of-N split (N >= 3); sets.json says $($work.name_cmd) is $($work.split) with $(@($work.cards).Count) cards"
    }
    $splitN = [int]$kn.Groups[1].Value
    $cards = @($work.cards | ForEach-Object { Get-CardWords -Path (Join-Path $Fixture $_) })
    $card1 = $cards[0]
    $card3 = $cards[2]
    # One word of card 1 made into no share word (no SLIP-39 word starts
    # with "zq", so no prefix of it is one either).
    $w = @($card1 -split ' ')
    $w[4] = 'zqzq'
    $typo = $w -join ' '
    $runs = Get-WordRuns -Cards @($card1, $card3)
    Write-Host "Set $($work.name_cmd): split $($work.split), Split ID $($work.split_id); typing cards 1 and 3 ($(@($card1 -split ' ').Count) words each)"

    $isos = @(Get-ChildItem -Path $Fixture -Filter '*.iso')
    $metaIso = ($isos | Where-Object { $_.Name -eq 'LCSAS_META.iso' }).FullName
    if (-not $metaIso) { throw 'LCSAS_META.iso not found in the fixture dir' }
    $dataIsos = @($isos | Where-Object { $_.Name -ne 'LCSAS_META.iso' } | ForEach-Object { $_.FullName })
    foreach ($iso in $isos) {
        Dismount-DiskImage -ImagePath $iso.FullName -ErrorAction SilentlyContinue | Out-Null
    }
    $packMap = Get-PackMap -DataIsos $dataIsos
    Clear-ToolsDir
    $tools = Get-ToolsDir

    $img = Mount-DiskImage -ImagePath $metaIso -PassThru
    $letter = [string](Get-Volume -DiskImage $img).DriveLetter
    if (-not $letter) { throw 'the meta ISO got no drive letter' }
    Write-Host "Only the meta ISO is mounted, at ${letter}:"
    $bat = "${letter}:\$BatRel"
    if (-not (Test-Path -LiteralPath $bat)) { throw "no $bat on the meta disc" }

    $target = Join-Path $env:RUNNER_TEMP $TargetName
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $outFile = Join-Path $env:RUNNER_TEMP "out-$TargetName.txt"
    if (Test-Path -LiteralPath $outFile) { Remove-Item -Force -LiteralPath $outFile }
    $pwBefore = Get-TempNames 'lcsas-pw-*'
    $sharesBefore = Get-TempNames 'lcsas-shares-*'

    $problems = New-Object 'System.Collections.Generic.List[string]'
    $proc = $null
    $result = @{ Boxes = 0; Swaps = 0; Current = $metaIso; Inserted = @() }
    $stepIso = $null
    $asked = $false
    $rc = 'n/a'
    try {
        Write-Host "Starting (no LCSAS_NO_RELOCATE, LCSAS_INTERACTIVE=on, no LCSAS_REPO): $bat"
        $proc = Start-RestoreProcess -Bat $bat -OutFile $outFile -WorkDir "${letter}:\"
        $stepIso = Invoke-NewestDiscStep -Proc $proc -OutFile $outFile -Letter $letter -Current $metaIso `
            -DataIsos $dataIsos -Problems $problems
        $result.Current = $stepIso
        Invoke-SetMenu -Proc $proc -OutFile $outFile -Sets $sets -Problems $problems -Choose $work

        $text = Wait-ForOutput -Path $outFile -Pattern 'Restore to which folder\?' -Proc $proc -Seconds 300
        if ($text -cnotmatch 'Restore to which folder\?') { throw "no folder prompt; output so far:`n$text" }
        $proc.StandardInput.WriteLine($target)

        $text = Wait-ForOutput -Path $outFile -Pattern 'Words of card 1: ' -Proc $proc -Seconds 120
        if ($text -cnotmatch 'Words of card 1: ') { throw "no question for card 1; output so far:`n$text" }
        Write-Host "card 1, with one word wrong"
        $proc.StandardInput.WriteLine($typo)
        $again = '(?s)Words of card 1: .*Words of card 1: '
        $text = Wait-ForOutput -Path $outFile -Pattern $again -Proc $proc -Seconds 120
        if ($text -cnotmatch $again) { throw "card 1 with a wrong word was not asked again; output so far:`n$text" }
        Write-Host "card 1 again, right"
        $proc.StandardInput.WriteLine($card1)
        $text = Wait-ForOutput -Path $outFile -Pattern 'Words of card 2: ' -Proc $proc -Seconds 120
        if ($text -cnotmatch 'Words of card 2: ') { throw "no question for card 2; output so far:`n$text" }
        Write-Host "card 3, at the question for card 2"
        $proc.StandardInput.WriteLine($card3)

        $result = Invoke-DiscBoxes -Proc $proc -OutFile $outFile -Letter $letter -Current $stepIso `
            -DataIsos $dataIsos -PackMap $packMap -Problems $problems
        $another = 'Restore another backup set from these discs\? \[y/N\]: '
        $text = Wait-ForOutput -Path $outFile -Pattern $another -Proc $proc -Seconds 120
        if ($text -cmatch $another) {
            $asked = $true
            # The tools folder is still there, with the pack cache and the
            # catalog copy: the cards must be in none of its files.
            if (-not (Test-Path -LiteralPath $tools)) {
                $problems.Add("the tools folder $tools is gone before the reader answered 'another set?'")
            } else {
                Write-ToolsDirReport -Dir $tools
                $hits = @(Find-CardText -Dir $tools -Runs $runs)
                if ($hits.Count -gt 0) { $problems.Add("share card words are in the tools folder: $($hits -join ', ')") }
            }
            $sharesNow = @(Get-TempNames 'lcsas-shares-*' | Where-Object { $sharesBefore -notcontains $_ })
            if ($sharesNow.Count -gt 0) { $problems.Add("typed cards still in %TEMP% after the restore: $($sharesNow -join ', ')") }
            Write-Host "another set?: N"
            $proc.StandardInput.WriteLine('N')
        } else {
            $problems.Add("no 'Restore another backup set from these discs? [y/N]' question")
        }
        $rc = Complete-RestoreProcess -Proc $proc -Problems $problems
    } finally {
        if ($proc -and -not $proc.HasExited) { & taskkill /PID $proc.Id /T /F | Out-Null }
    }
    $out = Read-SharedText $outFile
    Write-Host '----- restore.bat output -----'
    Write-Host $out
    Write-Host '----- end output -----'
    Write-Host "restore.bat exit code: $rc; disc boxes: $($result.Boxes); swaps at boxes: $($result.Swaps); data discs: $($dataIsos.Count)"

    if ("$rc" -ne '0') { $problems.Add("restore.bat exit code $rc (want 0)") }
    if ($out -cnotmatch 'Restore complete') { $problems.Add("'Restore complete' banner missing") }
    if ($out -notmatch 'copied recovery files to') { $problems.Add("no 'copied recovery files to' message: the relocation did not happen") }
    $intro = "The password for $($work.name_cmd) is split into $splitN share cards; you`r`nneed any 2 of them (Split ID $($work.split_id))."
    if (-not $out.Contains($intro)) { $problems.Add("no card introduction naming the set, 2 of $splitN and Split ID $($work.split_id) (C7)") }
    if ($out -notmatch "'zqzq' is not a share word") { $problems.Add("lcsas-keyshare.exe did not name the wrong word 'zqzq'") }
    if ($out -notmatch 'Share card 1 was not typed right') { $problems.Add('no line saying card 1 was not typed right') }
    if ($out -notmatch 'the share cards rebuilt the password') { $problems.Add("no 'the share cards rebuilt the password' line") }
    if ($out -match 'Words of card 3') { $problems.Add('asked for a third card of a 2-of-N split') }
    if ($out -cmatch 'Password: ') { $problems.Add("asked 'Password: ' for a set whose cards rebuilt it") }
    if ($out -match 'That password is wrong|does not open the backup|do not fit together') { $problems.Add('the rebuilt password was refused') }
    if ($out -match 'trying tier 2') { $problems.Add('fell through to tier 2') }
    Assert-NoAccessDenied -Out $out -Problems $problems
    Assert-SetNamed -Out $out -Sets $sets -Problems $problems -Set $work
    Test-TreeAgainstManifest -Fixture $Fixture -Target $target -Problems $problems -ManifestName $work.manifest
    $inserted = @(@($stepIso) + @($result.Inserted) | Where-Object { $_ })
    Assert-SwapsWithinDiscs -Inserted $inserted -DataIsos $dataIsos -What 'with the step' -Problems $problems
    $pwLeft = @(Get-TempNames 'lcsas-pw-*' | Where-Object { $pwBefore -notcontains $_ })
    if ($pwLeft.Count -gt 0) { $problems.Add("password file(s) left in %TEMP%: $($pwLeft -join ', ')") }
    $sharesLeft = @(Get-TempNames 'lcsas-shares-*' | Where-Object { $sharesBefore -notcontains $_ })
    if ($sharesLeft.Count -gt 0) { $problems.Add("typed cards left in %TEMP%: $($sharesLeft -join ', ')") }
    if (-not $asked) { $problems.Add('the tools folder was never searched for card words') }
    Assert-ToolsDirGone -Problems $problems

    foreach ($iso in $isos) {
        Dismount-DiskImage -ImagePath $iso.FullName -ErrorAction SilentlyContinue | Out-Null
    }
    if ($problems.Count -gt 0) {
        throw ("split-cards leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host "Split-cards leg passed: cards 1 (after a wrong word) and 3 rebuilt the password of $($work.name_cmd); tree byte-exact; no card left behind."
}
