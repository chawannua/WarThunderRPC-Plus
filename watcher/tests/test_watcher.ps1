#Requires -Version 5.1
<#
.SYNOPSIS
    Behavioral test suite for wtrpc-watcher.exe.

.DESCRIPTION
    Compiles the watcher plus two tiny throwaway helper binaries (a fake
    "game" process and a fake "child" process) into an isolated temp
    directory, then drives them through short poll/grace/backoff intervals
    to prove the watcher's process-supervision behavior end to end.

.PARAMETER Gcc
    Path to gcc.exe. Falls back to "gcc" on PATH.

.EXAMPLE
    pwsh -File watcher\tests\test_watcher.ps1
#>
param(
    [string]$Gcc = "C:/Users/Chawan.CHAWANNUA/AppData/Local/Microsoft/WinGet/Packages/BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe/mingw64/bin/gcc.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Gcc)) {
    $Gcc = "gcc"
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$WatcherSrc = Join-Path $RepoRoot "watcher\wtrpc-watcher.c"
$WatcherRc = Join-Path $RepoRoot "watcher\wtrpc-watcher.rc"

$script:FailCount = 0
$script:PassCount = 0

function Pass([string]$name) {
    $script:PassCount++
    Write-Host "[PASS] $name" -ForegroundColor Green
}

function Fail([string]$name, [string]$detail) {
    $script:FailCount++
    Write-Host "[FAIL] $name - $detail" -ForegroundColor Red
}

function Assert-True([bool]$cond, [string]$name, [string]$detail = "") {
    if ($cond) { Pass $name } else { Fail $name $detail }
}

# ---------------------------------------------------------------------------
# Build everything into an isolated temp dir.
# ---------------------------------------------------------------------------

$TestRoot = Join-Path $env:TEMP ("wtrpc-watcher-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TestRoot | Out-Null
$InstallDir = Join-Path $TestRoot "install"
New-Item -ItemType Directory -Path $InstallDir | Out-Null
$AppDataDir = Join-Path $TestRoot "appdata"
New-Item -ItemType Directory -Path $AppDataDir | Out-Null

Write-Host "Test root: $TestRoot"

$WatcherExe = Join-Path $InstallDir "wtrpc-watcher.exe"
$FakeGameExe = Join-Path $TestRoot "fakegame.exe"
$FakeChildExe = Join-Path $TestRoot "fakechild.exe"
$ResFile = Join-Path $TestRoot "wtrpc-watcher.res"

$WindresCandidate = Join-Path (Split-Path -Parent $Gcc) "windres.exe"
$Windres = if (Test-Path -LiteralPath $WindresCandidate) { $WindresCandidate } else { "windres" }

Write-Host "Compiling watcher..."
& $Windres $WatcherRc -O coff -o $ResFile
if ($LASTEXITCODE -ne 0) { throw "windres failed" }

& $Gcc -O2 -s -Wall -Wextra -Werror -municode -mwindows $WatcherSrc $ResFile -o $WatcherExe
if ($LASTEXITCODE -ne 0) { throw "gcc failed to build watcher" }

Write-Host "Compiling fakegame..."
& $Gcc -O2 -s -Wall -Wextra -Werror -mconsole (Join-Path $PSScriptRoot "fakegame.c") -o $FakeGameExe
if ($LASTEXITCODE -ne 0) { throw "gcc failed to build fakegame" }

Write-Host "Compiling fakechild..."
& $Gcc -O2 -s -Wall -Wextra -Werror -mconsole (Join-Path $PSScriptRoot "fakechild.c") -o $FakeChildExe
if ($LASTEXITCODE -ne 0) { throw "gcc failed to build fakechild" }

$watcherSize = (Get-Item $WatcherExe).Length
Write-Host ("Watcher exe size: {0:N0} bytes" -f $watcherSize)

# ---------------------------------------------------------------------------
# Shared test parameters (short so the suite runs quickly).
# ---------------------------------------------------------------------------

$IntervalMs = 300
$GraceMs = 1200
$BackoffBaseMs = 900
$env:APPDATA = $AppDataDir

$ChildLog = Join-Path $TestRoot "childlog.txt"
$CtlFile = Join-Path $TestRoot "ctl.txt"
New-Item -ItemType File -Path $ChildLog -Force | Out-Null
# Start in exit0 mode so the child voluntarily exits and lets the relaunch /
# backoff tests observe real exit->relaunch transitions instead of having to
# externally kill an otherwise long-lived child.
"exit0" | Set-Content -LiteralPath $CtlFile -NoNewline
$env:WTRPC_TEST_LOG = $ChildLog
$env:WTRPC_TEST_CTL = $CtlFile

function Set-Mode([string]$mode) {
    $mode | Set-Content -LiteralPath $CtlFile -NoNewline
}

function Get-ChildLogLines {
    if (Test-Path -LiteralPath $ChildLog) {
        return ,@(Get-Content -LiteralPath $ChildLog)
    }
    return ,@()
}

function Wait-For([scriptblock]$condition, [int]$timeoutMs, [int]$pollMs = 50) {
    $deadline = (Get-Date).AddMilliseconds($timeoutMs)
    while ((Get-Date) -lt $deadline) {
        if (& $condition) { return $true }
        Start-Sleep -Milliseconds $pollMs
    }
    return & $condition
}

$StartLineRe = "^start pid=\d+ managed=\d+ tick=\d+$"
$ExitLineRe = "^exit pid=\d+ rc=-?\d+ tick=\d+$"

function Get-StartLines([string[]]$lines) {
    # Filter to fully-formed lines only: the log file can be caught mid-write
    # by a concurrent poll, producing a truncated trailing line.
    # The leading comma prevents PowerShell from unwrapping a single-element
    # (or empty) result back into a bare scalar/$null on return.
    return ,@($lines | Where-Object { $_ -match $StartLineRe })
}

function Get-ExitLines([string[]]$lines) {
    return ,@($lines | Where-Object { $_ -match $ExitLineRe })
}

function Parse-Fields([string]$line) {
    $fields = @{}
    foreach ($tok in ($line -split "\s+")) {
        if ($tok -match "^(\w+)=(.+)$") { $fields[$matches[1]] = $matches[2] }
    }
    return $fields
}

# ---------------------------------------------------------------------------
# Session A: game appears -> child launched with --managed (test a),
# relaunch after exit 0 (test b), backoff + doubling after exit 1 (test c),
# second-instance no-op (test e), --stop stops watcher and child (test f).
# ---------------------------------------------------------------------------

$gameA = Start-Process -FilePath $FakeGameExe -WindowStyle Hidden -PassThru
$watcherA = Start-Process -FilePath $WatcherExe -WindowStyle Hidden -PassThru -ArgumentList @(
    "--game", "fakegame.exe",
    "--child", $FakeChildExe,
    "--interval", $IntervalMs,
    "--grace", $GraceMs,
    "--backoff-base", $BackoffBaseMs
)

# --- (a) child launched when game appears, with --managed ---
$gotFirstStart = Wait-For { (Get-StartLines (Get-ChildLogLines)).Count -ge 1 } 5000
$startLines = Get-StartLines (Get-ChildLogLines)
if ($gotFirstStart -and $startLines.Count -ge 1) {
    $f = Parse-Fields $startLines[0]
    Assert-True ($f.managed -eq "1") "a) child launched with --managed" "managed=$($f.managed) line='$($startLines[0])'"
} else {
    Fail "a) child launched when game appears" "no start line observed within timeout"
}

# --- (b) relaunch after exit 0 ---
Set-Mode "exit0"
$beforeCount = (Get-StartLines (Get-ChildLogLines)).Count
$gotRelaunch = Wait-For { (Get-StartLines (Get-ChildLogLines)).Count -ge ($beforeCount + 2) } 5000
if ($gotRelaunch) {
    $lines = Get-StartLines (Get-ChildLogLines)
    $exitLines = Get-ExitLines (Get-ChildLogLines)
    $lastExit = Parse-Fields $exitLines[-1]
    $newStart = Parse-Fields $lines[-1]
    $delay = [int]$newStart.tick - [int]$lastExit.tick
    Assert-True ($lastExit.rc -eq "0" -and $delay -lt ($IntervalMs * 4)) "b) relaunch promptly after exit 0" "rc=$($lastExit.rc) delayMs=$delay"
} else {
    Fail "b) relaunch after exit 0" "no relaunch observed within timeout"
}

# --- (c) backoff after non-zero exit, then doubling ---
Set-Mode "exit1"
$beforeCount = (Get-StartLines (Get-ChildLogLines)).Count
$gotBackoff1 = Wait-For { (Get-StartLines (Get-ChildLogLines)).Count -ge ($beforeCount + 2) } 6000
$delay1 = -1
if ($gotBackoff1) {
    $exitLines = Get-ExitLines (Get-ChildLogLines)
    $starts = Get-StartLines (Get-ChildLogLines)
    $lastExit = Parse-Fields $exitLines[-1]
    $newStart = Parse-Fields $starts[-1]
    $delay1 = [int]$newStart.tick - [int]$lastExit.tick
}
Assert-True ($gotBackoff1 -and $delay1 -ge ($BackoffBaseMs * 0.7)) "c) backoff after non-zero exit" "delay1Ms=$delay1 expected>=~$BackoffBaseMs"

# it's still set to exit1, so it will crash again after the relaunch -> doubled backoff
$beforeCount = (Get-StartLines (Get-ChildLogLines)).Count
$gotBackoff2 = Wait-For { (Get-StartLines (Get-ChildLogLines)).Count -ge ($beforeCount + 2) } 8000
$delay2 = -1
if ($gotBackoff2) {
    $exitLines = Get-ExitLines (Get-ChildLogLines)
    $starts = Get-StartLines (Get-ChildLogLines)
    $lastExit = Parse-Fields $exitLines[-1]
    $newStart = Parse-Fields $starts[-1]
    $delay2 = [int]$newStart.tick - [int]$lastExit.tick
}
Assert-True ($gotBackoff2 -and $delay2 -gt ($delay1 * 1.4)) "c) backoff doubles on repeated crash" "delay1Ms=$delay1 delay2Ms=$delay2"

# let it succeed and stay up for the rest of session A
Set-Mode "sleep"
$beforeCount = (Get-StartLines (Get-ChildLogLines)).Count
Wait-For { (Get-StartLines (Get-ChildLogLines)).Count -ge ($beforeCount + 1) } 8000 | Out-Null
Start-Sleep -Milliseconds ($IntervalMs * 3)
$stableStartCount = (Get-StartLines (Get-ChildLogLines)).Count
Start-Sleep -Milliseconds ($IntervalMs * 3)
Assert-True ((Get-StartLines (Get-ChildLogLines)).Count -eq $stableStartCount) "c) child stays up once healthy (no spurious relaunch)" "startCount changed"

# --- (e) second instance exits immediately ---
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$second = Start-Process -FilePath $WatcherExe -WindowStyle Hidden -PassThru -ArgumentList @(
    "--game", "fakegame.exe", "--child", $FakeChildExe
) -Wait
$sw.Stop()
Assert-True (($second.ExitCode -eq 0) -and ($sw.ElapsedMilliseconds -lt 3000)) "e) second instance exits immediately" "exitCode=$($second.ExitCode) elapsedMs=$($sw.ElapsedMilliseconds)"

# --- (f) --stop stops watcher and child ---
$startsBeforeStop = Get-StartLines (Get-ChildLogLines)
$lastChildPid = [int](Parse-Fields $startsBeforeStop[-1]).pid

Start-Process -FilePath $WatcherExe -WindowStyle Hidden -Wait -ArgumentList @("--stop")

$watcherStopped = Wait-For { $watcherA.HasExited } 11000
$childStopped = Wait-For { -not (Get-Process -Id $lastChildPid -ErrorAction SilentlyContinue) } 11000
Assert-True $watcherStopped "f) --stop makes the watcher exit" "watcher still running"
Assert-True $childStopped "f) --stop also stops the child" "child pid $lastChildPid still running"

if (-not $gameA.HasExited) { Stop-Process -Id $gameA.Id -Force -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------------------
# Session B: kill the watcher process itself -> child dies via job object
# (test g).
# ---------------------------------------------------------------------------

Set-Mode "sleep"
$gameB = Start-Process -FilePath $FakeGameExe -WindowStyle Hidden -PassThru
$watcherB = Start-Process -FilePath $WatcherExe -WindowStyle Hidden -PassThru -ArgumentList @(
    "--game", "fakegame.exe",
    "--child", $FakeChildExe,
    "--interval", $IntervalMs,
    "--grace", $GraceMs,
    "--backoff-base", $BackoffBaseMs
)

$beforeCount = (Get-StartLines (Get-ChildLogLines)).Count
$gotStart = Wait-For { (Get-StartLines (Get-ChildLogLines)).Count -ge ($beforeCount + 1) } 5000
$childPidB = -1
if ($gotStart) {
    $lines = Get-StartLines (Get-ChildLogLines)
    $childPidB = [int](Parse-Fields $lines[-1]).pid
}

if ($childPidB -gt 0) {
    Stop-Process -Id $watcherB.Id -Force -ErrorAction SilentlyContinue
    $childDiedWithWatcher = Wait-For { -not (Get-Process -Id $childPidB -ErrorAction SilentlyContinue) } 5000
    Assert-True $childDiedWithWatcher "g) killing the watcher kills the child (job object)" "child pid $childPidB still running"
} else {
    Fail "g) killing the watcher kills the child (job object)" "child never started for session B"
}

if (-not $gameB.HasExited) { Stop-Process -Id $gameB.Id -Force -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------------------
# Session C: game closes -> child terminated after grace period (test d).
# ---------------------------------------------------------------------------

Set-Mode "sleep"
$gameC = Start-Process -FilePath $FakeGameExe -WindowStyle Hidden -PassThru
$watcherC = Start-Process -FilePath $WatcherExe -WindowStyle Hidden -PassThru -ArgumentList @(
    "--game", "fakegame.exe",
    "--child", $FakeChildExe,
    "--interval", $IntervalMs,
    "--grace", $GraceMs,
    "--backoff-base", $BackoffBaseMs
)

$beforeCount = (Get-StartLines (Get-ChildLogLines)).Count
$gotStart = Wait-For { (Get-StartLines (Get-ChildLogLines)).Count -ge ($beforeCount + 1) } 5000
$childPidC = -1
if ($gotStart) {
    $lines = Get-StartLines (Get-ChildLogLines)
    $childPidC = [int](Parse-Fields $lines[-1]).pid
}

if ($childPidC -gt 0) {
    Stop-Process -Id $gameC.Id -Force -ErrorAction SilentlyContinue

    # Should still be alive before the grace period elapses.
    Start-Sleep -Milliseconds ([int]($GraceMs * 0.4))
    $aliveDuringGrace = [bool](Get-Process -Id $childPidC -ErrorAction SilentlyContinue)

    $diedAfterGrace = Wait-For { -not (Get-Process -Id $childPidC -ErrorAction SilentlyContinue) } ($GraceMs * 2 + 3000)
    Assert-True ($aliveDuringGrace -and $diedAfterGrace) "d) child terminated after game closes + grace" "aliveDuringGrace=$aliveDuringGrace diedAfterGrace=$diedAfterGrace"
} else {
    Fail "d) child terminated after game closes + grace" "child never started for session C"
}

Start-Process -FilePath $WatcherExe -WindowStyle Hidden -Wait -ArgumentList @("--stop") | Out-Null

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

foreach ($p in @($gameA, $watcherA, $gameB, $watcherB, $gameC, $watcherC)) {
    if ($p -and -not $p.HasExited) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
}
Get-Process -Name "fakegame", "fakechild" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

if ($env:WTRPC_TEST_KEEP -ne "1") {
    Remove-Item -LiteralPath $TestRoot -Recurse -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "Kept test root: $TestRoot"
}

Write-Host ""
Write-Host "Results: $script:PassCount passed, $script:FailCount failed" -ForegroundColor $(if ($script:FailCount -eq 0) { "Green" } else { "Red" })

if ($script:FailCount -gt 0) {
    exit 1
}
exit 0
