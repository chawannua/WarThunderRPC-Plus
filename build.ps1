#Requires -Version 5.1
<#
.SYNOPSIS
    One-command build: PyInstaller app bundle + watcher.exe + Inno Setup
    installer.

.DESCRIPTION
    Produces dist\WarThunderRPC-Plus-Setup-<version>.exe from a clean
    checkout. Safe to re-run; build\ and dist\ are wiped/recreated.

.PARAMETER Gcc
    Path to gcc.exe (MinGW-w64) used to compile the watcher. Falls back to
    "gcc" on PATH if the default install path isn't present.

.PARAMETER Iscc
    Path to ISCC.exe (Inno Setup Compiler). Falls back to "iscc" on PATH if
    the default install path isn't present.

.EXAMPLE
    pwsh -File build.ps1
#>
param(
    [string]$Gcc = "C:/Users/Chawan.CHAWANNUA/AppData/Local/Microsoft/WinGet/Packages/BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe/mingw64/bin/gcc.exe",
    [string]$Iscc = "C:/Users/Chawan.CHAWANNUA/AppData/Local/Programs/Inno Setup 6/ISCC.exe"
)

$ErrorActionPreference = "Stop"

function Resolve-Tool([string]$path, [string]$fallbackName) {
    if (Test-Path -LiteralPath $path) { return $path }
    Write-Host "  (not found at default path, falling back to '$fallbackName' on PATH)"
    return $fallbackName
}

$Gcc = Resolve-Tool $Gcc "gcc"
$Iscc = Resolve-Tool $Iscc "iscc"
$Windres = Join-Path (Split-Path -Parent $Gcc) "windres.exe"
if (-not (Test-Path -LiteralPath $Windres)) { $Windres = "windres" }

$RepoRoot = $PSScriptRoot
$BuildDir = Join-Path $RepoRoot "build"
$DistDir = Join-Path $RepoRoot "dist"

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

$initContent = Get-Content -LiteralPath (Join-Path $RepoRoot "wtrpc\__init__.py") -Raw
if ($initContent -notmatch '__version__\s*=\s*"([^"]+)"') {
    throw "Could not find __version__ in wtrpc\__init__.py"
}
$Version = $matches[1]
Write-Host "Version: $Version"

Write-Host "Cleaning build\ and dist\..."
Remove-Item -LiteralPath $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $DistDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $BuildDir | Out-Null
New-Item -ItemType Directory -Path $DistDir | Out-Null

# ---------------------------------------------------------------------------
# 1. PyInstaller onedir bundle
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "=== Building wtrpc with PyInstaller ==="

$pyiWorkPath = Join-Path $BuildDir "pyinstaller-work"
$pyiDistPath = Join-Path $BuildDir "pyinstaller-dist"
$pyiSpecPath = Join-Path $BuildDir "pyinstaller-spec"

$pyiArgs = @(
    "-m", "PyInstaller",
    (Join-Path $RepoRoot "packaging\wtrpc_entry.py"),
    "--name", "wtrpc",
    "--onedir",
    "--noconsole",
    "--icon", (Join-Path $RepoRoot "logo.ico"),
    "--clean",
    "--noconfirm",
    "--workpath", $pyiWorkPath,
    "--distpath", $pyiDistPath,
    "--specpath", $pyiSpecPath,
    "--add-data", "$(Join-Path $RepoRoot 'logo.ico');.",
    "--add-data", "$(Join-Path $RepoRoot 'logo.png');."
)

# pytesseract imports these opportunistically, so whatever happens to be
# installed on the build machine gets swept in: ~40 MB of installer and
# ~450 MB of committed memory the app never touches.
foreach ($module in @("numpy", "pandas", "scipy", "matplotlib", "tkinter",
                      "cryptography", "dateutil", "setuptools", "pytest")) {
    $pyiArgs += @("--exclude-module", $module)
}

# Bundle pytesseract only if it's actually available in this environment;
# it's an optional dependency (weapon-HUD OCR) that PyInstaller's static
# analysis can't see through the try/except import in wtrpc/weapon_ocr.py.
python -c "import pytesseract" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "pytesseract is importable; bundling it."
    $pyiArgs += @("--hidden-import", "pytesseract")
} else {
    Write-Host "pytesseract not importable; skipping (weapon OCR will be unavailable in this build)."
}

python @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$wtrpcAppDir = Join-Path $pyiDistPath "wtrpc"
if (-not (Test-Path -LiteralPath (Join-Path $wtrpcAppDir "wtrpc.exe"))) {
    throw "PyInstaller did not produce wtrpc.exe at $wtrpcAppDir"
}

# ---------------------------------------------------------------------------
# 2. Watcher
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "=== Building wtrpc-watcher.exe ==="

$watcherBuildDir = Join-Path $BuildDir "watcher"
New-Item -ItemType Directory -Path $watcherBuildDir | Out-Null

$watcherRes = Join-Path $watcherBuildDir "wtrpc-watcher.res"
$watcherExe = Join-Path $watcherBuildDir "wtrpc-watcher.exe"

& $Windres (Join-Path $RepoRoot "watcher\wtrpc-watcher.rc") -O coff -o $watcherRes
if ($LASTEXITCODE -ne 0) { throw "windres failed with exit code $LASTEXITCODE" }

& $Gcc -O2 -s -Wall -Wextra -Werror -municode -mwindows `
    (Join-Path $RepoRoot "watcher\wtrpc-watcher.c") $watcherRes -o $watcherExe
if ($LASTEXITCODE -ne 0) { throw "gcc failed to build the watcher with exit code $LASTEXITCODE" }

Write-Host ("Watcher exe: {0} ({1:N0} bytes)" -f $watcherExe, (Get-Item $watcherExe).Length)

# ---------------------------------------------------------------------------
# 3. Installer (Inno Setup)
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "=== Building installer with Inno Setup ==="

& $Iscc "/DAppVersion=$Version" `
    "/DWtrpcDistDir=$wtrpcAppDir" `
    "/DWatcherExe=$watcherExe" `
    "/O$DistDir" `
    (Join-Path $RepoRoot "installer\WarThunderRPC-Plus.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }

$setupExe = Join-Path $DistDir "WarThunderRPC-Plus-Setup-$Version.exe"
if (-not (Test-Path -LiteralPath $setupExe)) {
    throw "Expected setup exe not found at $setupExe"
}

Write-Host ""
Write-Host ("Setup: {0} ({1:N0} bytes)" -f $setupExe, (Get-Item $setupExe).Length) -ForegroundColor Green
