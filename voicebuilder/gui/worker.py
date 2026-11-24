"""Thread-safe background task runner with wx.CallAfter progress, logging, and cancellation."""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable, Optional

import wx

from ..downloader import CancelFn, CancelledError, ProgressFn


class BackgroundTaskRunner:
    """Runs long pipeline operations off the wx UI thread without freezing the GUI."""

    def __init__(
        self,
        on_start: Callable[[str], None],
        on_progress: Callable[[str, Optional[float]], None],
        on_finish: Callable[[bool, str], None],
    ) -> None:
        self._on_start = on_start
        self._on_progress = on_progress
        self._on_finish = on_finish
        self._lock = threading.Lock()
        self._busy = False
        self._cancel_event = threading.Event()

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy

    def request_cancel(self) -> None:
        self._cancel_event.set()
        wx.CallAfter(self._on_progress, "Cancelling operation...", None)

    def run(
        self,
        title: str,
        fn: Callable[[ProgressFn, CancelFn], Any],
        *,
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._cancel_event.clear()

        wx.CallAfter(self._on_start, title)

        def _progress(msg: str, frac: Optional[float] = None) -> None:
            wx.CallAfter(self._on_progress, msg, frac)

        def _cancel() -> bool:
            return self._cancel_event.is_set()

        def _target() -> None:
            try:
                result = fn(_progress, _cancel)
            except CancelledError as exc:
                def _done_cancel() -> None:
                    with self._lock:
                        self._busy = False
                    self._on_finish(False, str(exc))
                    if on_error:
                        on_error(exc)
                wx.CallAfter(_done_cancel)
            except Exception as exc:
                tb = traceback.format_exc()
                def _done_err() -> None:
                    with self._lock:
                        self._busy = False
                    self._on_progress(tb, None)
                    self._on_finish(False, f"{title} failed: {exc}")
                    if on_error:
                        on_error(exc)
                wx.CallAfter(_done_err)
            else:
                def _done_ok() -> None:
                    with self._lock:
                        self._busy = False
                    self._on_finish(True, f"{title} complete.")
                    if on_success:
                        on_success(result)
                wx.CallAfter(_done_ok)

        threading.Thread(target=_target, name=f"vb-worker-{title}", daemon=True).start()
        return True
