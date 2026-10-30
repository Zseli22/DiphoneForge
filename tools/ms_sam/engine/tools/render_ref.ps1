# Render reference audio with the real Microsoft Sam (must run under 32-bit PowerShell).
# Usage: SysWOW64 powershell -File render_ref.ps1 -Text "..." -Out path.wav [-Fmt 22]
# SAPI format ids: 18 = 16kHz 16-bit mono, 22 = 22kHz 16-bit mono, 34 = 44kHz 16-bit mono
param(
  [Parameter(Mandatory)][string]$Text,
  [Parameter(Mandatory)][string]$Out,
  [int]$Fmt = 22,
  [string]$Voice = "Microsoft Sam"
)
if ([IntPtr]::Size -ne 4) { throw "run under 32-bit PowerShell (SysWOW64)" }
$v = New-Object -ComObject SAPI.SpVoice
$v.Voice = $v.GetVoices("Name=$Voice").Item(0)
$s = New-Object -ComObject SAPI.SpFileStream
$s.Format.Type = $Fmt
$s.Open($Out, 3, $false)
$v.AudioOutputStream = $s
[void]$v.Speak($Text, 0)
$s.Close()
"wrote $Out with " + $v.Voice.GetDescription()
