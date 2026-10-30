#Requires -Version 5.1
<#
.SYNOPSIS
	Compares the installed add-on with the build staging folder and reports drift.

.DESCRIPTION
	Directory trees are compared file by file with SHA256. Files that exist on
	one side only are listed separately from files whose contents differ, so a
	user-added voice is easy to distinguish from a stale binary.
	__pycache__ and .pyc files are ignored.

.PARAMETER Installed
	Installed add-on folder. Defaults to the espeakWitMbrola folder in the NVDA
	configuration directory.

.PARAMETER Reference
	Folder to compare against. Defaults to build\stage.

.EXAMPLE
	.\tools\compare-install.ps1
#>
[CmdletBinding()]
param(
	[string]$Installed,
	[string]$Reference
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

if (-not $Installed) {
	$Installed = Join-Path $env:APPDATA "nvda\addons\espeakWitMbrola"
}
if (-not $Reference) {
	$Reference = Join-Path $root "build\stage"
}

foreach ($pair in @(@("installed", $Installed), @("reference", $Reference))) {
	if (-not (Test-Path $pair[1])) {
		throw "The $($pair[0]) folder does not exist: $($pair[1])"
	}
}

function Get-Tree([string]$base) {
	$result = @{}
	Get-ChildItem $base -Recurse -File | Where-Object {
		$_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne ".pyc"
	} | ForEach-Object {
		$relative = $_.FullName.Substring($base.Length + 1)
		$result[$relative] = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
	}
	return $result
}

$installedTree = Get-Tree $Installed
$referenceTree = Get-Tree $Reference

$onlyInstalled = $installedTree.Keys | Where-Object { -not $referenceTree.ContainsKey($_) } | Sort-Object
$onlyReference = $referenceTree.Keys | Where-Object { -not $installedTree.ContainsKey($_) } | Sort-Object
$different = $installedTree.Keys |
	Where-Object { $referenceTree.ContainsKey($_) -and $installedTree[$_] -ne $referenceTree[$_] } |
	Sort-Object

Write-Host ""
Write-Host "Installed: $Installed"
Write-Host "Reference: $Reference"
Write-Host ("{0} files installed, {1} files in the reference" -f $installedTree.Count, $referenceTree.Count)
Write-Host ""

if ($onlyInstalled) {
	Write-Host "Only in the installed add-on (user additions or leftovers):" -ForegroundColor Yellow
	$onlyInstalled | ForEach-Object { Write-Host "  $_" }
	Write-Host ""
}
if ($onlyReference) {
	Write-Host "Missing from the installed add-on:" -ForegroundColor Yellow
	$onlyReference | ForEach-Object { Write-Host "  $_" }
	Write-Host ""
}
if ($different) {
	Write-Host "Different contents:" -ForegroundColor Yellow
	$different | ForEach-Object { Write-Host "  $_" }
	Write-Host ""
}

if (-not $onlyInstalled -and -not $onlyReference -and -not $different) {
	Write-Host "The installed add-on matches the reference exactly." -ForegroundColor Green
} else {
	Write-Host "The installed add-on does not match the reference yet." -ForegroundColor Yellow
}
