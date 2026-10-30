# Vendored third-party sources

`tools/mbrola-src` and `tools/mbrolator-src` are complete copies of the upstream
projects, with the local Windows patches already applied. They are committed as
plain source, not as git submodules, because a submodule cannot carry a local
modification and the AGPL requires the patched source to travel with the binary
we ship.

| Directory | Upstream | Commit | Local changes |
| --- | --- | --- | --- |
| `mbrola-src` | https://github.com/numediart/MBROLA.git | `274dead162f2` | `Parser/input_fifo.c`, `Parser/input_fifo.h`: `close_InputFifo` exported so the DLL build links. |
| `mbrolator-src` | https://github.com/numediart/MBROLATOR.git | `3444f6ae1089` | `Resynthesis/Src/resynth.c`: binary-mode output. `Resynthesis/Src/database_build.c`: Windows-safe `date` and `rm`. New files `wincompat.c` / `wincompat.h`: `drand48` and the C23 prototype. |

Both patches are in `tools/patches/` as diffs against those commits, so they can
be reapplied to a fresh clone:

```powershell
git clone https://github.com/numediart/MBROLA.git mbrola-src
git -C mbrola-src checkout 274dead162f2
git -C mbrola-src apply ..\patches\mbrola-src.patch
```

Build notes that are not in the patches:

- AnaMBE must be compiled with `-DLITTLE_ENDIAN`.
- `resynth` must be compiled with `-O0`; `-O2` miscompiles the harmonic matching
  into NaN amplitudes and silent gaps. It is also roughly 200 times faster at
  `-O0` on this codebase, because the optimised build produced pathologically
  slow output.
- The streaming engine needs `-DDLL -DROMDATABASE_STORE -DROMDATABASE_INIT` and
  the include list in `addon/README.md`.

The build outputs (`Obj/`, `Bin/`, `*.exe`) are not committed; they are rebuilt
with the commands in `AGENTS.md` and `addon/README.md`.
