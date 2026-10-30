#Requires -Version 5.1
<#
.SYNOPSIS
	Builds dist\<name>-<version>.nvda-addon from addon/, the voice packages in
	voices/, and a folder containing the third-party engines.

.DESCRIPTION
	The add-on folder holds only what this project authors. The eSpeak NG and
	MBROLA binaries and eSpeak's data are taken from -VendorSource, so they are
	never committed here. Both native binaries are compiled from the sources in
	this repository when a 32-bit capable toolchain is available; otherwise the
	prebuilt copies from -VendorSource are reused and a warning is printed.

.PARAMETER VendorSource
	Folder containing espeak.dll, mbrola.dll, mbrola.exe and espeak-ng-data.
	Defaults to $env:ESPEAK_MB_VENDOR, then to the installed add-on.

.PARAMETER SkipCompile
	Do not run the compilers; copy espeakMbrolaHost.exe and mbrola_stream.dll
	from -VendorSource.

.EXAMPLE
	.\build.ps1

.EXAMPLE
	.\build.ps1 -VendorSource "C:\src\espeak-ng-1.51\espeak-ng-data" -SkipCompile
#>
[CmdletBinding()]
param(
	[string]$VendorSource = $env:ESPEAK_MB_VENDOR,
	[switch]$SkipCompile
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {

# --- read the manifest -------------------------------------------------------

$manifestText = Get-Content "addon\manifest.ini" -Raw
function Get-ManifestValue([string]$key) {
	$match = [regex]::Match($manifestText, "(?m)^\s*$([regex]::Escape($key))\s*=\s*(.+)$")
	if (-not $match.Success) { throw "addon\manifest.ini does not define '$key'." }
	return $match.Groups[1].Value.Trim().Trim('"')
}

$name = Get-ManifestValue "name"
$version = Get-ManifestValue "version"
Write-Host "Building $name $version" -ForegroundColor Cyan

# --- locate the vendor engines ----------------------------------------------

if (-not $VendorSource) {
	$VendorSource = Join-Path $env:APPDATA "nvda\addons\espeakWitMbrola\synthDrivers"
}
if (-not (Test-Path $VendorSource)) {
	throw "Vendor source not found: $VendorSource`nPass -VendorSource, or set ESPEAK_MB_VENDOR."
}
$required = @("espeak.dll", "mbrola.dll", "mbrola.exe", "espeak-ng-data")
foreach ($item in $required) {
	if (-not (Test-Path (Join-Path $VendorSource $item))) {
		throw "Vendor source is missing '$item': $VendorSource"
	}
}
Write-Host "Vendor source: $VendorSource"

# --- clean and stage ---------------------------------------------------------

$stage = "build\stage"
$bin = "build\bin"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage, $bin, "dist" | Out-Null

Copy-Item "addon\*" $stage -Recurse -Force
Remove-Item (Join-Path $stage "README.md") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $stage "synthDrivers\*.cpp") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $stage "synthDrivers\__pycache__") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $stage "synthDrivers\*.exe") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $stage "synthDrivers\*.dll") -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force (Join-Path $stage "synthDrivers\espeak-ng-data\mbrola") | Out-Null

# eSpeak data, minus the databases: those come from voices/ only.
$dataSource = Join-Path $VendorSource "espeak-ng-data"
$dataTarget = Join-Path $stage "synthDrivers\espeak-ng-data"
robocopy $dataSource $dataTarget /E /XD mbrola /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -gt 7) { throw "Copying eSpeak data failed with code $LASTEXITCODE." }

Copy-Item (Join-Path $VendorSource "espeak.dll") (Join-Path $stage "synthDrivers")
Copy-Item (Join-Path $VendorSource "mbrola.dll") (Join-Path $stage "synthDrivers")
Copy-Item (Join-Path $VendorSource "mbrola.exe") (Join-Path $stage "synthDrivers")

# --- compile the native parts ------------------------------------------------

$hostExe = Join-Path $bin "espeakMbrolaHost.exe"
$streamDll = Join-Path $bin "mbrola_stream.dll"

function Test-Toolchain {
	foreach ($tool in @("g++", "gcc")) {
		if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { return $false }
	}
	return $true
}

$compiled = $false
if (-not $SkipCompile) {
	if (-not (Test-Toolchain)) {
		Write-Warning "g++/gcc not found; using prebuilt binaries from the vendor source."
	} else {
		Write-Host "Compiling espeakMbrolaHost.exe"
		& g++ -m32 -std=c++17 -O2 -Wall -Wextra -Wno-cast-function-type `
			-o $hostExe "addon\synthDrivers\espeak_mbrola_host.cpp"
		if ($LASTEXITCODE -ne 0) { throw "g++ failed with code $LASTEXITCODE." }

		Write-Host "Compiling mbrola_stream.dll"
		$streamOut = Join-Path (Get-Location).Path "build\bin\mbrola_stream.dll"
		Push-Location "tools\mbrola-src"
		try {
			& gcc -m32 -O2 -w -fpermissive -DDLL -DROMDATABASE_STORE -DROMDATABASE_INIT `
				-IParser -IStandalone -IMisc -ILibOneChannel -ILibMultiChannel -IEngine -IDatabase `
				-include Parser/fifo.h -include Parser/input_fifo.h -include Parser/phonbuff.h `
				-shared -o $streamOut `
				LibOneChannel/onechannel.c Misc/g711.c Parser/input_fifo.c Parser/fifo.c `
				Engine/mbrola.c Engine/diphone.c Parser/phone.c Parser/parser_input.c `
				Parser/input_file.c Parser/phonbuff.c Misc/audio.c Misc/vp_error.c `
				Misc/mbralloc.c Misc/common.c Database/database.c Database/database_old.c `
				Database/diphone_info.c Database/little_big.c Database/hash_tab.c `
				Database/zstring_list.c Database/rom_handling.c Database/rom_database.c `
				VisualC/mbrolalib/mbrola.def -lm
			if ($LASTEXITCODE -ne 0) { throw "gcc failed with code $LASTEXITCODE." }
		} finally {
			Pop-Location
		}
		$compiled = $true
	}
}

if (-not $compiled) {
	foreach ($pair in @(
		@("espeakMbrolaHost.exe", $hostExe),
		@("mbrola_stream.dll", $streamDll)
	)) {
		$from = Join-Path $VendorSource $pair[0]
		if (-not (Test-Path $from)) { throw "No prebuilt $($pair[0]) in the vendor folder and compilation was skipped." }
		Copy-Item $from $pair[1] -Force
	}
}

Copy-Item $hostExe (Join-Path $stage "synthDrivers") -Force
Copy-Item $streamDll (Join-Path $stage "synthDrivers") -Force

# --- add the voice packages --------------------------------------------------

function Read-VoiceManifest([string]$path) {
	$result = @{}
	foreach ($line in Get-Content $path) {
		$line = $line.Trim()
		if (-not $line -or $line.StartsWith("#")) { continue }
		$index = $line.IndexOf("=")
		if ($index -lt 1) { continue }
		$result[$line.Substring(0, $index).Trim()] = $line.Substring($index + 1).Trim()
	}
	return $result
}

$voiceCount = 0
foreach ($package in Get-ChildItem "voices" -Directory) {
	$id = $package.Name
	$metaPath = Join-Path $package.FullName "$id.voice"
	if (-not (Test-Path $metaPath)) { throw "Voice package '$id' has no $id.voice manifest." }
	$meta = Read-VoiceManifest $metaPath
	foreach ($key in @("database", "descriptor", "phtrans")) {
		if (-not $meta[$key]) { throw "Voice package '$id' does not define '$key'." }
	}

	$database = Join-Path $package.FullName $meta["database"]
	$descriptor = Join-Path $package.FullName $meta["descriptor"]
	$phtrans = Join-Path $package.FullName $meta["phtrans"]
	foreach ($file in @($database, $descriptor, $phtrans)) {
		if (-not (Test-Path $file)) { throw "Voice package '$id' is missing $(Split-Path $file -Leaf)." }
	}

	Copy-Item $database (Join-Path $stage "synthDrivers\espeak-ng-data\mbrola\$($meta['database'])") -Force
	Copy-Item $phtrans (Join-Path $stage "synthDrivers\espeak-ng-data\mbrola_ph\$($meta['phtrans'])") -Force
	Copy-Item $descriptor (Join-Path $stage "synthDrivers\espeak-ng-data\voices\mb\$($meta['descriptor'])") -Force

	$docVoice = Join-Path $stage "doc\voices\$id"
	New-Item -ItemType Directory -Force $docVoice | Out-Null
	Copy-Item $metaPath $docVoice -Force
	foreach ($extra in @("PROVENANCE.md")) {
		$candidate = Join-Path $package.FullName $extra
		if (Test-Path $candidate) { Copy-Item $candidate $docVoice -Force }
	}
	$voiceCount++
	Write-Host ("  voice {0}: {1} at {2} Hz" -f $id, $meta["display_name"], $meta["sample_rate"])
}

if ($voiceCount -eq 0) { throw "No voice packages found in voices\." }

# --- verify and package ------------------------------------------------------

foreach ($path in @(
	"manifest.ini",
	"installTasks.py",
	"doc\en\readme.html",
	"synthDrivers\_espeak_mb.py",
	"synthDrivers\espeakwithmbrola.py",
	"synthDrivers\espeak.dll",
	"synthDrivers\mbrola.dll",
	"synthDrivers\mbrola.exe",
	"synthDrivers\mbrola_stream.dll",
	"synthDrivers\espeakMbrolaHost.exe"
)) {
	if (-not (Test-Path (Join-Path $stage $path))) { throw "Staged add-on is missing $path." }
}

$output = "dist\$name-$version.nvda-addon"
if (Test-Path $output) { Remove-Item $output -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$level = [System.IO.Compression.CompressionLevel]::Optimal
[System.IO.Compression.ZipFile]::CreateFromDirectory(
	(Resolve-Path $stage).Path, (Join-Path (Get-Location) $output), $level, $false)

$file = Get-Item $output
$hash = (Get-FileHash $output -Algorithm SHA256).Hash.ToLower()
$size = [math]::Round($file.Length / 1MB, 2)
Write-Host ""
Write-Host ("Built {0} ({1} MB, {2} voices, compiled from source: {3})" -f `
	$output, $size, $voiceCount, $compiled) -ForegroundColor Green
Write-Host "SHA256 $hash"
Write-Host "Next: install it with NVDA's Add-on Store or by opening the file, then restart NVDA."

}
finally {
	Pop-Location
}
