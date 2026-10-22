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

# restore.bat prints nothing about the --pack-search drives it hands to
# lcsas-restore.exe, and --repo is X:\metadata\win (keys/index/snapshots,
# no data\), so EVERY pack must come from a --pack-search drive.  Proving
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

# Describe the relocated copy restore.bat leaves under %TEMP%.
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
# happened, that the tier-1 binary ran from the relocated copy, and that
# the restored tree is byte-exact.  Reports (does not fail on) what the
# script leaves behind in %TEMP%.
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
        if (Test-Path -LiteralPath $ramDir) {
            Write-RamDirReport -RamDir $ramDir
            $exe = Join-Path $ramDir 'recovery\bin\x86_64-pc-windows-gnu\lcsas-restore.exe'
            if (-not (Test-Path -LiteralPath $exe)) {
                $problems.Add("relocated copy has no lcsas-restore.exe at $exe (the bin tree was not mirrored)")
            }
        } else {
            $problems.Add("relocation message named $ramDir but it does not exist")
        }
    } else {
        $problems.Add("no 'copied recovery files to' message: the relocation did not happen")
    }
    $ramNew = @(Get-TempNames 'lcsas-restore-*' | Where-Object { $ramBefore -notcontains $_ })
    Write-Host "New lcsas-restore-* dirs under %TEMP% after the run: $($ramNew -join ', ')"

    # 2. the tier-1 binary that ran came from the relocated copy.
    $t1 = [regex]::Match($out, '\[tier 1\] running (.+)')
    if ($t1.Success) {
        Write-Host "Tier-1 binary: $($t1.Groups[1].Value.Trim())"
        if ($t1.Groups[1].Value -notmatch 'lcsas-restore-\d+-\d+') {
            $problems.Add('tier-1 binary did not run from the relocated copy')
        }
    }

    # 3. completion + exit status + byte-exact tree.
    if ($rc -ne 0) { $problems.Add("restore.bat exit code $rc (want 0)") }
    if ($out -notmatch 'Recovery complete') { $problems.Add("'Recovery complete' banner missing") }
    Test-TreeAgainstManifest -Fixture $Fixture -Target $target -Problems $problems

    # 4. what the script leaves behind (report; the script does not claim to
    #    clean the relocated copy, so that is not a failure).
    $pwLeft = @(Get-TempNames 'lcsas-pw-*' | Where-Object { $pwBefore -notcontains $_ })
    if ($pwLeft.Count -gt 0) { $problems.Add("password file(s) left in %TEMP%: $($pwLeft -join ', ')") }
    foreach ($n in $ramNew) {
        Write-Host "LEFT BEHIND by restore.bat (not cleaned up): $(Join-Path $env:TEMP $n)"
        Remove-Item -Recurse -Force -LiteralPath (Join-Path $env:TEMP $n) -ErrorAction SilentlyContinue
    }

    if ($problems.Count -gt 0) {
        throw ("relocation leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host 'Relocation leg passed: copy made, tier 1 ran from it, tree byte-exact.'
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
    # The outer instance's last line (`exit /b %ERRORLEVEL%`) lives on the
    # disc that was just ejected, so the exit status is reported, not
    # asserted; the contract is that the RESTORE finishes.  Bug #532: once
    # that is fixed, the two NOTEs below become problems.
    if ($out -notmatch 'Recovery complete') { $problems.Add("'Recovery complete' banner missing after the eject") }
    if ($out -notmatch '\[lcsas-restore\] restore complete') { $problems.Add('tier-1 restore did not report completion after the eject') }
    if ($out -match 'The system cannot find the path specified') {
        Write-Host 'NOTE: the outer cmd could not re-read restore.bat from the ejected disc after the relocated copy finished ("The system cannot find the path specified."); the restore itself completed.'
    }
    if ($rc -ne 0) {
        Write-Host "NOTE: exit code is $rc although the restore completed (the outer instance's last line, exit /b %ERRORLEVEL%, lived on the ejected disc)."
    }
    Test-TreeAgainstManifest -Fixture $Fixture -Target $target -Problems $problems

    $ramNew = @(Get-TempNames 'lcsas-restore-*' | Where-Object { $ramBefore -notcontains $_ })
    foreach ($n in $ramNew) {
        Write-Host "LEFT BEHIND by restore.bat (not cleaned up): $(Join-Path $env:TEMP $n)"
        Remove-Item -Recurse -Force -LiteralPath (Join-Path $env:TEMP $n) -ErrorAction SilentlyContinue
    }

    if ($problems.Count -gt 0) {
        throw ("eject leg failed:`n  - " + ($problems -join "`n  - "))
    }
    Write-Host 'Eject leg passed: the relocated restore survived the meta-disc eject.'
}
