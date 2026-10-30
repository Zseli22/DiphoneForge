# Add-on source

This folder is the complete source of the NVDA add-on. Nothing else in the
repository is packaged. Build it with `..\build.ps1`, which copies this folder,
adds the third-party engines and the voice packages from `..\voices`, compiles
the two native binaries, and writes `..\dist\espeakWitMbrola-<version>.nvda-addon`.

The files here are:

- `manifest.ini` — add-on metadata. `minimumNVDAVersion` is 2026.1.0 because the
  64-bit to 32-bit bridge depends on that API.
- `installTasks.py` — keeps databases and descriptors the user added themselves
  across updates, without overwriting files the new version ships.
- `synthDrivers/espeakwithmbrola.py` — the synth driver, derived from NVDA's
  eSpeak driver.
- `synthDrivers/_espeak_mb.py` — the bridge to the 32-bit helper: process
  startup, framed messaging, PCM feeding, index marks, cancellation.
- `synthDrivers/espeak_mbrola_host.cpp` — the 32-bit helper, built to
  `espeakMbrolaHost.exe`.
- `doc/en/readme.html` — the documentation NVDA shows.

Two binaries are produced from source and are not committed: the helper above
and `mbrola_stream.dll`, which is built from `..\tools\mbrola-src`.

## Architecture

NVDA's 64-bit process runs the Python driver and owns the audio player. The
helper is a 32-bit process that loads `espeak.dll` and `mbrola.dll`, generates
`.pho`, rewrites durations to the requested rate, and streams 20 ms PCM blocks
back over stdio with marker offsets. Audio for an utterance starts flowing a few
milliseconds after the text arrives, while the rest is still being synthesised.

`mbrola_stream.dll` is an optional in-memory build of the same MBROLA sources
with the `LibOneChannel` API. The host uses it when present and falls back to
`mbrola.exe` and a temporary file otherwise; set `ESPEAK_MB_MBROLA_EXE=1` to
force the executable path.

Full detail, including the index-mark and interruption design, is in
`..\AGENTS.md`.

## Native build commands

The helper:

```powershell
g++ -m32 -std=c++17 -O2 -Wall -Wextra -Wno-cast-function-type `
    -o build\bin\espeakMbrolaHost.exe addon\synthDrivers\espeak_mbrola_host.cpp
```

The streaming engine, from `tools\mbrola-src`:

```powershell
gcc -m32 -O2 -w -fpermissive -DDLL -DROMDATABASE_STORE -DROMDATABASE_INIT `
    -IParser -IStandalone -IMisc -ILibOneChannel -ILibMultiChannel -IEngine -IDatabase `
    -include Parser/fifo.h -include Parser/input_fifo.h -include Parser/phonbuff.h `
    -shared -o ..\..\build\bin\mbrola_stream.dll `
    LibOneChannel/onechannel.c Misc/g711.c Parser/input_fifo.c Parser/fifo.c `
    Engine/mbrola.c Engine/diphone.c Parser/phone.c Parser/parser_input.c `
    Parser/input_file.c Parser/phonbuff.c Misc/audio.c Misc/vp_error.c `
    Misc/mbralloc.c Misc/common.c Database/database.c Database/database_old.c `
    Database/diphone_info.c Database/little_big.c Database/hash_tab.c `
    Database/zstring_list.c Database/rom_handling.c Database/rom_database.c `
    VisualC/mbrolalib/mbrola.def -lm
```

`build.ps1` runs both and falls back to a prebuilt copy from the vendor folder
if the 32-bit toolchain is missing.
