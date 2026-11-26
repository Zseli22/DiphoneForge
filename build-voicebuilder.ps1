<#
.SYNOPSIS
    Builds the standalone portable Windows executable bundle for DiphoneForge Voice Builder.
.DESCRIPTION
    Freezes `voicebuilder/` (plus bundled pipeline tools) with PyInstaller (--onedir --windowed),
    copies the required native binaries (eSpeak NG, MBROLATOR, MBROLA, 32-bit preview engine),
    data files, and licence notices into `dist\DiphoneForge-VoiceBuilder-<version>-win64\`,
    and creates a ready-to-upload release archive `dist\DiphoneForge-VoiceBuilder-<version>-win64.zip`.

    Never bundles `Voice_audio/`, `output/`, `cache/`, or shipped/third-party MBROLA voice databases.
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$EngineSource = "",
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

if (-not $Version) {
    $initPy = Get-Content (Join-Path $RepoRoot "voicebuilder\__init__.py") -Raw
    if ($initPy -match '__version__\s*=\s*"([^"]+)"') {
        $Version = $Matches[1]
    } else {
        $Version = "0.1.0"
    }
}

Write-Host "=== Building DiphoneForge Voice Builder v$Version ===" -ForegroundColor Cyan

# 1. Verify required native tool binaries
$EspeakDir = Join-Path $RepoRoot "tools\espeak-ng"
$RequiredEspeak = @(
    (Join-Path $EspeakDir "espeak-ng.exe"),
    (Join-Path $EspeakDir "libespeak-ng.dll"),
    (Join-Path $EspeakDir "espeak-ng-data")
)
foreach ($p in $RequiredEspeak) {
    if (-not (Test-Path $p)) { throw "Missing eSpeak NG component: $p" }
}

$MbrolatorBins = @{
    "anaf0.exe"          = Join-Path $RepoRoot "tools\mbrolator-src\AnaMBE\Obj\anaf0.exe"
    "anambe.exe"         = Join-Path $RepoRoot "tools\mbrolator-src\AnaMBE\Obj\anambe.exe"
    "resynth.exe"        = Join-Path $RepoRoot "tools\mbrolator-src\Resynthesis\resynth.exe"
    "database_build.exe" = Join-Path $RepoRoot "tools\mbrolator-src\Resynthesis\database_build.exe"
    "mbrola.exe"         = Join-Path $RepoRoot "tools\mbrola-src\Bin\mbrola.exe"
}
foreach ($kv in $MbrolatorBins.GetEnumerator()) {
    if (-not (Test-Path $kv.Value)) { throw "Missing MBROLATOR/MBROLA binary ($($kv.Key)): $($kv.Value)" }
}

# Locate 32-bit live preview engine (espeakMbrolaHost.exe + espeak.dll + mbrola.dll + espeak-ng-data)
$EngineCandidates = @()
if ($EngineSource) { $EngineCandidates += $EngineSource }
$EngineCandidates += @(
    (Join-Path $RepoRoot "build\stage\synthDrivers"),
    (Join-Path $RepoRoot "addon\synthDrivers"),
    (Join-Path $env:APPDATA "nvda\addons\espeakWitMbrola\synthDrivers")
)
$PreviewEngine = $null
foreach ($cand in $EngineCandidates) {
    if ($cand -and
        (Test-Path (Join-Path $cand "espeakMbrolaHost.exe")) -and
        (Test-Path (Join-Path $cand "espeak.dll")) -and
        (Test-Path (Join-Path $cand "mbrola.dll")) -and
        (Test-Path (Join-Path $cand "espeak-ng-data"))) {
        $PreviewEngine = $cand
        break
    }
}
if (-not $PreviewEngine) {
    throw "32-bit preview engine not found. Run .\build.ps1 first or pass -EngineSource."
}
Write-Host "Using preview engine from: $PreviewEngine"

# 2. Freeze Python GUI + worker entry point via PyInstaller (--onedir --windowed)
$PyWork = Join-Path $RepoRoot "build\vb-pyinstaller-work"
$PyDist = Join-Path $RepoRoot "build\vb-pyinstaller-dist"
$PySpec = Join-Path $RepoRoot "build\vb-pyinstaller-spec"
foreach ($d in @($PyWork, $PyDist, $PySpec)) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
    New-Item -ItemType Directory -Path $d | Out-Null
}

$PyArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--name", "DiphoneForgeVoiceBuilder",
    "--distpath", $PyDist,
    "--workpath", $PyWork,
    "--specpath", $PySpec,
    "--paths", (Join-Path $RepoRoot "tools"),
    "--hidden-import", "plan_word_corpus",
    "--hidden-import", "build_word_mbrola_db",
    "--hidden-import", "resegment_from_whole_alignment",
    "--hidden-import", "smoke_mbrola_host",
    "--hidden-import", "voicebuilder.dictionary",
    "--collect-all", "_sounddevice_data",
    "--collect-all", "_soundfile_data",
    "--exclude-module", "tkinter",
    "--exclude-module", "matplotlib",
    "--exclude-module", "PIL",
    "--exclude-module", "PyQt5",
    "--exclude-module", "PyQt6",
    "--exclude-module", "PySide6",
    (Join-Path $RepoRoot "voicebuilder\gui_entry.py")
)

Write-Host "Running PyInstaller..." -ForegroundColor Yellow
& python @PyArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

# 3. Assemble clean portable distribution folder
$BundleName = "DiphoneForge-VoiceBuilder-$Version-win64"
$DistRoot = Join-Path $RepoRoot "dist"
$OutDir = Join-Path $DistRoot $BundleName
if (-not (Test-Path $DistRoot)) { New-Item -ItemType Directory -Path $DistRoot | Out-Null }
if (Test-Path $OutDir) { Remove-Item -Recurse -Force $OutDir }

Write-Host "Assembling portable bundle in $OutDir..." -ForegroundColor Yellow
Copy-Item -Recurse -Force (Join-Path $PyDist "DiphoneForgeVoiceBuilder") $OutDir

# data/language_catalog.json
$OutData = Join-Path $OutDir "data"
New-Item -ItemType Directory -Path $OutData | Out-Null
Copy-Item (Join-Path $RepoRoot "data\language_catalog.json") (Join-Path $OutData "language_catalog.json")

# tools/espeak-ng/
$OutEspeak = Join-Path $OutDir "tools\espeak-ng"
New-Item -ItemType Directory -Path $OutEspeak | Out-Null
Copy-Item (Join-Path $EspeakDir "espeak-ng.exe") $OutEspeak
Copy-Item (Join-Path $EspeakDir "libespeak-ng.dll") $OutEspeak
Copy-Item -Recurse -Force (Join-Path $EspeakDir "espeak-ng-data") (Join-Path $OutEspeak "espeak-ng-data")

# tools/mbrolator/
$OutMbrolator = Join-Path $OutDir "tools\mbrolator"
New-Item -ItemType Directory -Path $OutMbrolator | Out-Null
foreach ($kv in $MbrolatorBins.GetEnumerator()) {
    Copy-Item $kv.Value (Join-Path $OutMbrolator $kv.Key)
}
if (Test-Path (Join-Path $RepoRoot "tools\mfa-packages.txt")) {
    Copy-Item (Join-Path $RepoRoot "tools\mfa-packages.txt") (Join-Path $OutDir "tools\mfa-packages.txt")
}

# engine/ (32-bit live speech preview engine — excluding shipped voices from espeak-ng-data/mbrola)
$OutEngine = Join-Path $OutDir "engine"
New-Item -ItemType Directory -Path $OutEngine | Out-Null
foreach ($f in @("espeakMbrolaHost.exe", "espeak.dll", "mbrola.dll", "mbrola.exe", "mbrola_stream.dll")) {
    $src = Join-Path $PreviewEngine $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $OutEngine $f) }
}
Copy-Item -Recurse -Force (Join-Path $PreviewEngine "espeak-ng-data") (Join-Path $OutEngine "espeak-ng-data")
$EngMbrola = Join-Path $OutEngine "espeak-ng-data\mbrola"
if (Test-Path $EngMbrola) { Remove-Item -Recurse -Force $EngMbrola }
New-Item -ItemType Directory -Path $EngMbrola | Out-Null

# Licences, NOTICE, and Quick-Start README
Copy-Item -Recurse -Force (Join-Path $RepoRoot "LICENSES") (Join-Path $OutDir "LICENSES")
Copy-Item (Join-Path $RepoRoot "NOTICE.md") (Join-Path $OutDir "NOTICE.md")

$QuickStart = @"
DiphoneForge Voice Builder v$Version (Windows 64-bit Portable)
==============================================================

Build your own MBROLA speech synthesizer voice for NVDA in 36 languages.

QUICK START
-----------
1. Extract this entire folder somewhere convenient (avoid OneDrive-synced folders).
2. Double-click `DiphoneForgeVoiceBuilder.exe` to open the accessible stepped wizard.
3. On Step 0 (Home), click "New Project..." (Alt+P) to choose your language, voice ID,
   recording workflow (Built-in PC Mic Studio OR External Recorder Import), and sample
   rate (22.05 kHz default, or 16 kHz).
4. Word lists (~0.5 MB), Montreal Forced Aligner runtime (~2.4 GB one-time download),
   and language acoustic models (~15-60 MB each) are downloaded automatically on demand
   into the portable `cache\` folder next to the executable.

KEYBOARD SHORTCUTS (NVDA / Screen-Reader Friendly)
--------------------------------------------------
- Alt+N : Next step
- Alt+B : Back step
- Alt+H : Home (Step 0)
- Alt+C : Cancel active background task
- Step 2 (Mic Studio): Space on Record button (or F5 / Ctrl+Space anywhere) starts &
  stops recording; P plays eSpeak pronunciation reference; L listens to your take;
  Enter accepts and advances.
- Step 4 (Take Picker): Type to search diphones; Enter jumps into Recorded Takes;
  W plays full word; D plays diphone cut; Enter pins take; R resets to Auto;
  Escape saves and closes.

SOURCE CODE & LICENSING
-----------------------
See NOTICE.md and LICENSES\ in this folder.
Full corresponding source code (including the GPL/AGPL components):
  https://github.com/Zseli22/DiphoneForge
"@
Set-Content -Path (Join-Path $OutDir "README.txt") -Value $QuickStart -Encoding UTF8

$BundleBytes = (Get-ChildItem -Recurse -File $OutDir | Measure-Object -Property Length -Sum).Sum
Write-Host ("Portable folder ready: {0} ({1:N1} MB uncompressed)" -f $OutDir, ($BundleBytes / 1MB)) -ForegroundColor Green

# 4. Create distributable zip archive
if (-not $SkipZip) {
    $ZipPath = Join-Path $DistRoot "$BundleName.zip"
    if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
    Write-Host "Creating release zip: $ZipPath ..." -ForegroundColor Yellow
    Compress-Archive -Path $OutDir -DestinationPath $ZipPath -CompressionLevel Optimal
    $ZipBytes = (Get-Item $ZipPath).Length
    Write-Host ("Release zip ready: {0} ({1:N1} MB)" -f $ZipPath, ($ZipBytes / 1MB)) -ForegroundColor Green
}
