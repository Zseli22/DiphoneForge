"""Top-level entry point for the frozen DiphoneForge Voice Builder executable.

Calls `multiprocessing.freeze_support()` before anything else so `ProcessPoolExecutor`
inside `build_word_mbrola_db` works cleanly in frozen Windows PyInstaller builds.
"""

from __future__ import annotations

import multiprocessing
import sys


def _main() -> int:
    multiprocessing.freeze_support()
    # Ensure line-buffered UTF-8 output when invoked as a redirected `--_run-tool` worker
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass
    from voicebuilder.__main__ import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
