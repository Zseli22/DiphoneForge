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
    is_tool = len(sys.argv) >= 2 and sys.argv[1] == "--_run-tool"
    from voicebuilder.__main__ import main

    if is_tool:
        try:
            res = main()
            return int(res) if isinstance(res, int) else 0
        except SystemExit as exc:
            code = exc.code
            if code is None:
                return 0
            if isinstance(code, int):
                return code
            print(str(code), file=sys.stderr, flush=True)
            return 1
        except Exception:
            import traceback

            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            return 1

    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
