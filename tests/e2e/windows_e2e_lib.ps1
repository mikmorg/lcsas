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

# --repo is a metadata\win folder (keys/index/snapshots, no data\), so
# EVERY pack must come from a drive restore.bat hands to lcsas-restore.exe
# (it names them: "drives searched for discs:").  Proving
# that more than one mounted data disc holds packs therefore proves, with
# the byte-exact tree check, that a restore consumed more than one disc.
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
function Test-TreeAgainstManifest {
    param(
        [string]$Fixture,
        [string]$Target,
        [System.Collections.Generic.List[string]]$Problems
    )
    $manifest = Get-Content (Join-Path $Fixture 'manifest.json') -Raw | ConvertFrom-Json
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

# Describe a relocated copy found under %TEMP%.
function Write-RamDirReport {
    param([string]$RamDir)
    $files = @(Get-ChildItem -LiteralPath $RamDir -Recurse -File -Force -ErrorAction SilentlyContinue)
    $bytes = [int64](($files | Measure-Object -Property Length -Sum).Sum)
    Write-Host ("RAMDIR {0}: {1} files, {2:N1} MB" -f $RamDir, $files.Count, ($bytes / 1MB))
    foreach ($d in (Get-ChildItem -LiteralPath $RamDir -Recurse -Directory -Depth 2 -ErrorAction SilentlyContinue)) {
        $n = @(Get-ChildItem -LiteralPath $d.FullName -Recurse -File -Force -ErrorAction SilentlyContinue).Count
        Write-Host ("  {0}  ({1} files)" -f $d.FullName.Substring($RamDir.Length), $n)
    }
}

# #545: restore.bat removes its relocated copy.  Fail on any new one, and
# remove it so a later leg starts clean.
function Assert-NoRamDirLeft {
    param(
        [string[]]$New,
        [System.Collections.Generic.List[string]]$Problems
    )
    foreach ($n in $New) {
        $dir = Join-Path $env:TEMP $n
        Write-Host "LEFT BEHIND by restore.bat: $dir"
        Write-RamDirReport -RamDir $dir
        $Problems.Add("restore.bat left its relocated copy behind: $dir")
        Remove-Item -Recurse -Force -LiteralPath $dir -ErrorAction SilentlyContinue
    }
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
# happened, that the tier-1 binary ran from the relocated copy, that the
# restored tree is byte-exact, and that the script removed the copy and
# its password file from %TEMP% (#545).
# ---------------------------------------------------------------------
function Invoke-RelocationLeg {
    param(
        [string]$Fixture,
        [string]$BatRel,        # path of restore.bat relative to the meta-disc root
        [string]$TargetName
    )
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false
    Remove-Item Env:LCSAS_NO_RELOCATE -ErrorAction SilentlyContinue
    Remove-Item Env:LCSAS_RELOCATED -ErrorAction SilentlyContinue

    $meta = Find-MetaDrive
    if (-not $meta) { throw 'meta ISO not mounted (no drive holds restore.bat)' }
    $bat = "${meta}:\$BatRel"
    if (-not (Test-Path -LiteralPath $bat)) { throw "no $bat on the meta disc" }
    Assert-MultiDisc

    $target = Join-Path $env:RUNNER_TEMP $TargetName
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $pw = (Get-Content (Join-Path $Fixture 'repo.pw') -Raw).Trim()
    $answers = Join-Path $env:RUNNER_TEMP "answers-$TargetName.txt"
    @($target, $pw, '', '', '', '', '', '') | Set-Content -Path $answers -Encoding ascii
    $outFile = Join-Path $env:RUNNER_TEMP "out-$TargetName.txt"

    $ramBefore = Get-TempNames 'lcsas-restore-*'
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

    # 1. relocation happened: the script's own message names the copy.
    $m = [regex]::Match($out, 'copied recovery files to (.+)')
    $ramDir = $null
    if ($m.Success) {
        $ramDir = $m.Groups[1].Value.Trim()
        Write-Host "Relocation message seen; RAMDIR = $ramDir"
        # #545: the relaunch line removes the copy once it returns.
        if (Test-Path -LiteralPath $ramDir) {
            Write-RamDirReport -RamDir $ramDir
            $problems.Add("the relocated copy $ramDir is still there after the run")
        }
    } else {
        $problems.Add("no 'copied recovery files to' message: the relocation did not happen")
    }
    $ramNew = @(Get-TempNames 'lcsas-restore-*' | Where-Object { $ramBefore -notcontains $_ })
    Write-Host "New lcsas-restore-* dirs under %TEMP% after the run: $($ramNew -join ', ')"

    # 2. the tier-1 binary that ran came from the relocated copy, so the
    #    bin tree was mirrored (the copy is gone by now to look in).
    $t1 = [regex]::Match($out, '\[tier 1\] running (.+)')
    if ($t1.Success) {
        Write-Host "Tier-1 binary: $($t1.Groups[1].Value.Trim())"
        if ($t1.Groups[1].Value -notmatch 'lcsas-restore-\d+-\d+') {
            $problems.Add('tier-1 binary did not run from the relocated copy')
        }
    } else {
        $problems.Add("no '[tier 1] running' line: the relocated copy had no lcsas-restore.exe (the bin tree was not mirrored)")
    }

    # 3. #610: tier 1 got a COPY of a disc's catalog.db, made in the
    #    relocated copy's own folder, never the file on the disc (an open
    #    file holds the disc in its drive).  Every ISO is mounted here, so
    #    restore.bat finds a catalog.db at launch.
    $cat = [regex]::Match($out, 'using catalog ([A-Z]:\\catalog\.db) \(a copy in (.+)\)')
    if ($cat.Success) {
        Write-Host "Catalog: $($cat.Groups[1].Value), copy in $($cat.Groups[2].Value)"
        if ($cat.Groups[2].Value -notmatch 'lcsas-restore-\d+-\d+\\recovery$') {
            $problems.Add("the catalog copy is not in the relocated copy: $($cat.Groups[2].Value)")
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

    # 5. nothing left behind in %TEMP%: the password file, or the copy (#545).
    $pwLeft = @(Get-TempNames 'lcsas-pw-*' | Where-Object { $pwBefore -notcontains $_ })
    if ($pwLeft.Count -gt 0) { $problems.Add("password file(s) left in %TEMP%: $($pwLeft -join ', ')") }
    Assert-NoRamDirLeft -New $ramNew -Problems $problems

    if ($problems.Count -gt 0) {
        throw ("relocation leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host 'Relocation leg passed: copy made, tier 1 ran from it with a local catalog copy, tree byte-exact, copy removed.'
}

# ---------------------------------------------------------------------
# Leg (#545): the wrong-password case of the windows-e2e.yml step, but
# relocated.  The relaunch line now runs commands after the relocated copy
# returns, and any of them can replace its exit code, so a failure must
# still come back as 77 through the relocation, with the copy removed.
# Every other relocated leg expects 0, which a lost exit code also gives.
# ---------------------------------------------------------------------
function Invoke-RelocatedWrongPasswordLeg {
    param(
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

    $target = Join-Path $env:RUNNER_TEMP $TargetName
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $answers = Join-Path $env:RUNNER_TEMP "answers-$TargetName.txt"
    @($target, 'definitely-the-wrong-password', '', '', '', '', '', '') |
        Set-Content -Path $answers -Encoding ascii
    $outFile = Join-Path $env:RUNNER_TEMP "out-$TargetName.txt"

    $ramBefore = Get-TempNames 'lcsas-restore-*'
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
    $ramNew = @(Get-TempNames 'lcsas-restore-*' | Where-Object { $ramBefore -notcontains $_ })
    Assert-NoRamDirLeft -New $ramNew -Problems $problems

    if ($problems.Count -gt 0) {
        throw ("relocated wrong-password leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host 'Relocated wrong-password leg passed: exit 77 through the relocation, copy removed.'
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

    $target = Join-Path $env:RUNNER_TEMP $TargetName
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $pw = (Get-Content (Join-Path $Fixture 'repo.pw') -Raw).Trim()
    $outFile = Join-Path $env:RUNNER_TEMP "out-$TargetName.txt"
    if (Test-Path -LiteralPath $outFile) { Remove-Item -Force -LiteralPath $outFile }
    $ramBefore = Get-TempNames 'lcsas-restore-*'

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

    $ramNew = @(Get-TempNames 'lcsas-restore-*' | Where-Object { $ramBefore -notcontains $_ })
    Assert-NoRamDirLeft -New $ramNew -Problems $problems

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

# ---------------------------------------------------------------------
# Leg (#604, #617): ONE optical drive.  Only the meta ISO is mounted.
# restore.bat relocates (no LCSAS_NO_RELOCATE), and every data disc then
# goes into the meta disc's own drive letter, one at a time, as for a
# reader with one drive.  Up to rc5 restore.bat never gave tier 1 that
# letter, so this restore could not finish.
#
# How a disc box is spotted: tier 1 prints the box on stderr, which the
# cmd /c string sends to $outFile.  Its first line is
# "| Pack <16 hex>... is required for the next file." and it ends with
# the bare prompt "> ".  The leg counts the lines that hold
# 'is required for the next file'; one more of them, with the file
# ending in "> ", is a new box.  The 16-hex pack prefix is looked up in a
# map made at the start (each data ISO mounted once, its data\ listed):
# the first box names no disc, because the meta disc has no catalog.
#
# stdin: restore.bat's two `set /p` prompts (folder, password) get one
# line each, written only once the prompt is in $outFile; then one empty
# line (ENTER) per box, after the swap.  LCSAS_INTERACTIVE=on makes tier
# 1 ask although its stdin is a pipe.  stdin is closed after the final
# banner, which ends the `pause`.
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

    $isos = @(Get-ChildItem -Path $Fixture -Filter '*.iso')
    $metaIso = ($isos | Where-Object { $_.Name -eq 'LCSAS_META.iso' }).FullName
    if (-not $metaIso) { throw 'LCSAS_META.iso not found in the fixture dir' }
    $dataIsos = @($isos | Where-Object { $_.Name -ne 'LCSAS_META.iso' } | ForEach-Object { $_.FullName })
    if ($dataIsos.Count -lt 2) { throw "need >= 2 data ISOs for a swap; the fixture has $($dataIsos.Count)" }

    # An empty machine: nothing mounted.
    foreach ($iso in $isos) {
        Dismount-DiskImage -ImagePath $iso.FullName -ErrorAction SilentlyContinue | Out-Null
    }

    # Which data ISO holds which pack.
    $packMap = @{}
    foreach ($iso in $dataIsos) {
        $img = Mount-DiskImage -ImagePath $iso -PassThru
        $l = [string](Get-Volume -DiskImage $img).DriveLetter
        $names = @(Get-ChildItem -LiteralPath "${l}:\data" -Recurse -File -ErrorAction SilentlyContinue |
                   ForEach-Object { $_.Name })
        Write-Host ("{0}: {1} packs" -f (Split-Path $iso -Leaf), $names.Count)
        $packMap[$iso] = $names
        Dismount-DiskImage -ImagePath $iso | Out-Null
    }

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
    $ramBefore = Get-TempNames 'lcsas-restore-*'
    $pwBefore  = Get-TempNames 'lcsas-pw-*'

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'cmd.exe'
    $psi.Arguments = "/c `"`"$bat`" > `"$outFile`" 2>&1`""
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.StandardInputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = "${letter}:\"
    $psi.EnvironmentVariables['LCSAS_INTERACTIVE'] = 'on'

    $problems = New-Object 'System.Collections.Generic.List[string]'
    $proc = $null
    $current = $metaIso
    $boxes = 0
    $swaps = 0
    $maxBoxes = $dataIsos.Count + 2
    try {
        Write-Host "Starting (no LCSAS_NO_RELOCATE, LCSAS_INTERACTIVE=on): $bat"
        $proc = [System.Diagnostics.Process]::Start($psi)

        $text = Wait-ForOutput -Path $outFile -Pattern 'Restore to which folder\?' -Proc $proc -Seconds 300
        if ($text -cnotmatch 'Restore to which folder\?') { throw "no folder prompt; output so far:`n$text" }
        $proc.StandardInput.WriteLine($target)
        $text = Wait-ForOutput -Path $outFile -Pattern 'Password: ' -Proc $proc -Seconds 120
        if ($text -cnotmatch 'Password: ') { throw "no password prompt; output so far:`n$text" }
        $proc.StandardInput.WriteLine($pw)

        $seen = 0
        $deadline = (Get-Date).AddSeconds(900)
        while ((Get-Date) -lt $deadline) {
            $text = Read-SharedText $outFile
            if ($proc.HasExited) { break }
            if ($text -cmatch 'Restore complete' -or $text -cmatch 'The restore stopped') { break }
            $n = [regex]::Matches($text, 'is required for the next file').Count
            if ($n -le $seen -or -not $text.EndsWith('> ')) {
                Start-Sleep -Milliseconds 300
                continue
            }
            $seen = $n
            $boxes++
            if ($boxes -gt $maxBoxes) {
                $problems.Add("more than $maxBoxes disc boxes for $($dataIsos.Count) data discs; typed q")
                $proc.StandardInput.WriteLine('q')
                continue
            }
            $packs = [regex]::Matches($text, '\| Pack ([0-9a-f]{16})\.\.\.')
            if ($packs.Count -eq 0) {
                $problems.Add("box ${boxes} names no pack; typed q")
                $proc.StandardInput.WriteLine('q')
                continue
            }
            $prefix = $packs[$packs.Count - 1].Groups[1].Value
            $next = $null
            foreach ($iso in $dataIsos) {
                if (@($packMap[$iso] | Where-Object { $_.StartsWith($prefix) }).Count -gt 0) { $next = $iso }
            }
            if (-not $next) {
                $problems.Add("box ${boxes} asks for pack $prefix, which is on no data ISO; typed q")
                $proc.StandardInput.WriteLine('q')
                continue
            }
            if ($next -ne $current) {
                Switch-DiscInLetter -Letter $letter -Current $current -Iso $next
                $current = $next
                $swaps++
            }
            $isoName = Split-Path $next -Leaf
            Write-Host "box ${boxes}: pack $prefix is on $isoName, now in ${letter}: (swaps so far: $swaps); ENTER"
            $proc.StandardInput.WriteLine('')
        }
        # The final banner is up (or the script ended): close stdin, which
        # ends its `pause`.
        try { $proc.StandardInput.Close() } catch { Write-Host "stdin close: $($_.Exception.Message)" }
        if (-not $proc.WaitForExit(120000)) {
            $problems.Add('restore.bat did not exit within 120 s after its last box')
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
    Write-Host "restore.bat exit code: $rc; disc boxes: $boxes; swaps: $swaps; data discs: $($dataIsos.Count)"

    if ($out -notmatch 'copied recovery files to') {
        $problems.Add("no 'copied recovery files to' message: the relocation did not happen")
    }
    if ("$rc" -ne '0') { $problems.Add("restore.bat exit code $rc (want 0)") }
    if ($out -cnotmatch 'Restore complete') { $problems.Add("'Restore complete' banner missing") }
    if ($swaps -lt 1) { $problems.Add('no disc was swapped: the leg proved nothing') }
    if ($boxes -gt $dataIsos.Count + 1) {
        $problems.Add("$boxes disc boxes for $($dataIsos.Count) data discs (want at most $($dataIsos.Count + 1))")
    }
    if ($out -match '/Volumes') { $problems.Add('a disc box lists the POSIX folder /Volumes on Windows') }
    if ($out -match 'trying tier 2') { $problems.Add('fell through to tier 2') }
    # TODO(#611): the relocation's write probe prints "Access is denied."
    # on every start from a disc (F1).  rc6 PR B2 fixes it; then make this
    # a problem instead of a warning.
    if ($out -match 'Access is denied') {
        Write-Host "::warning::restore.bat printed 'Access is denied.' while relocating (#611, known; fixed in PR B2)"
    }
    Test-TreeAgainstManifest -Fixture $Fixture -Target $target -Problems $problems

    $pwLeft = @(Get-TempNames 'lcsas-pw-*' | Where-Object { $pwBefore -notcontains $_ })
    if ($pwLeft.Count -gt 0) { $problems.Add("password file(s) left in %TEMP%: $($pwLeft -join ', ')") }
    $ramNew = @(Get-TempNames 'lcsas-restore-*' | Where-Object { $ramBefore -notcontains $_ })
    Assert-NoRamDirLeft -New $ramNew -Problems $problems

    # Leave no image mounted for the steps after this one.
    foreach ($iso in $isos) {
        Dismount-DiskImage -ImagePath $iso.FullName -ErrorAction SilentlyContinue | Out-Null
    }

    if ($problems.Count -gt 0) {
        throw ("single-drive leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host "Single-drive leg passed: $swaps swap(s) in ${letter}:, $boxes box(es), tree byte-exact."
}
