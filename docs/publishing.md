# Publishing a release

The add-on is distributed as a single `.nvda-addon` file attached to a GitHub
release. The source in the repository is the corresponding source for both
native binaries, which is what the GPL and AGPL require.

## One-time setup

- A 32-bit capable toolchain on `PATH` (`g++ -m32`, `gcc -m32`; w64devkit works).
- A vendor source for the engines: an existing installation of this add-on, or
  the official eSpeak NG 1.51 32-bit build plus the MBROLA binaries. Set
  `ESPEAK_MB_VENDOR`, or pass `-VendorSource`.

```powershell
$env:ESPEAK_MB_VENDOR = "$env:APPDATA\nvda\addons\espeakWitMbrola\synthDrivers"
```

## Build

```powershell
.\build.ps1
```

This compiles `espeakMbrolaHost.exe` and `mbrola_stream.dll` from source, stages
the add-on, adds the voice packages, and writes
`dist\espeakWitMbrola-<version>.nvda-addon`. It prints the SHA256.

If the toolchain is missing, `-SkipCompile` reuses the vendor's prebuilt copies
and prints a warning. Do not publish an artifact built that way: the source
obligation is only satisfied by a build that actually compiles the two binaries.

## Verify before publishing

1. Install the artifact into NVDA and restart.
2. Check that the voice list contains only the voices whose databases are
   present, and that `mb-usd16` and `mb-usd` both speak.
3. Compare the installed folder with the build:

   ```powershell
   .\tools\compare-install.ps1
   ```

   It should report that the installed add-on matches the reference exactly.
4. Synthesise through the built host without starting NVDA:

   ```powershell
   python tools\smoke_mbrola_host.py `
     --engine-dir "$env:APPDATA\nvda\addons\espeakWitMbrola\synthDrivers" `
     --voice mb-usd16 --output build\check-usd16.wav
   python tools\smoke_mbrola_host.py `
     --engine-dir "$env:APPDATA\nvda\addons\espeakWitMbrola\synthDrivers" `
     --voice mb-usd --output build\check-usd.wav
   ```

   Both must report PCM at 16000 and 22050 Hz with index markers.

## Bump the version

Edit `addon/manifest.ini` only. `build.ps1` reads the version from there, and so
does NVDA. NVDA will not reinstall the same version number over itself, so any
content change needs a new one.

Update the status table in `README.md` if the change alters what is shipped.

## Tag and release

```powershell
git tag -a v3.0 -m "DiphoneForge 3.0: eSpeak NG with MBROLA, USD16 and USD"
git push origin main --follow-tags
& "C:\Users\vangi\Tools\gh\bin\gh.exe" release create v3.0 `
  dist\espeakWitMbrola-3.0.nvda-addon `
  --repo Zseli22/DiphoneForge `
  --title "eSpeak NG with MBROLA 3.0" `
  --notes-file docs\release-notes-3.0.md
```

Attach the artifact and its `.sha256` file. Do not attach the vendor engines,
the eSpeak data, or any database other than the two built here.

## What a release must not contain

- `Voice_audio/` or anything derived from it in audio form.
- `output/` in any form; the audit data is already in `docs/database/`.
- Third-party MBROLA databases. The add-on supports them; it does not ship them.
- A `.nvda-addon` built with `-SkipCompile`, unless the release notes say the
  binaries were not rebuilt.

## Source obligations

- The two native binaries are rebuilt by `build.ps1` from `addon/synthDrivers/`
  and `tools/mbrola-src/`, including the one patch described in
  `addon/README.md`. Keep both in the repository at the tag.
- eSpeak NG and MBROLA are redistributed unmodified; point at the upstream
  repositories and versions in `NOTICE.md`.
- The voice databases are CC BY 4.0 and carry their attribution in
  `voices/*/PROVENANCE.md`.
