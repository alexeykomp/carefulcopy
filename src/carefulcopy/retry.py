"""Default classification of "worth retrying" errors."""
from __future__ import annotations

import errno

from .errors import TransientError

# errno values that indicate a network/connection problem rather than a
# permanent condition such as "permission denied" or "no such file".
_TRANSIENT_ERRNOS = frozenset(
    getattr(errno, name)
    for name in (
        "ECONNRESET", "ECONNABORTED", "ECONNREFUSED", "EPIPE", "ETIMEDOUT",
        "ENETUNREACH", "ENETDOWN", "ENETRESET", "EHOSTUNREACH", "EHOSTDOWN",
        "EADDRNOTAVAIL", "ESTALE",
    )
    if hasattr(errno, name)
)


def is_transient_error(exc: BaseException) -> bool:
    """Default heuristic used by backends that do not know better."""
    if isinstance(exc, TransientError):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, BrokenPipeError)):
        return True
    if isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS:
        return True
    return False


# --------------------------------------------------------------------------
# One question for many failing copies
# --------------------------------------------------------------------------
import logging as _logging
import threading as _threading
from typing import Callable as _Callable, List as _List, Optional as _Optional

_log = _logging.getLogger("carefulcopy.retry")


class BatchedFailurePrompt:
    """Use as ``on_failure=`` when many files are copied in parallel.

    When a connection drops, *every* running copy fails at about the same time.
    Instead of N dialogs, the first failure opens a short collection window
    (``window`` seconds); everything that fails meanwhile is gathered and
    ``ask(sources)`` is called **once**. Its answer (retry or not) is given to
    all waiting copies.

    ``ask`` runs on a helper thread. A GUI application should marshal it to
    its UI thread itself (e.g. with a Qt signal and a ``threading.Event``).
    ``before``/``after`` bracket the question - handy for pausing new
    connections while the dialog is open (``backend.pause_new_connections`` /
    ``resume_new_connections``).
    """

    def __init__(self, ask: _Callable[[_List[str]], bool], window: float = 5.0,
                 before: _Optional[_Callable[[], None]] = None,
                 after: _Optional[_Callable[[], None]] = None) -> None:
        self._ask, self._window, self._before, self._after = ask, window, before, after
        self._lock = _threading.Lock()
        self._pending: _List["_Waiter"] = []
        self._timer: _Optional[_threading.Timer] = None

    def __call__(self, source: str, exc: BaseException) -> bool:
        waiter = _Waiter(source)
        with self._lock:
            self._pending.append(waiter)
            if self._timer is None:
                self._timer = _threading.Timer(self._window, self._flush)
                self._timer.daemon = True
                self._timer.start()
        waiter.event.wait()
        return waiter.retry

    def _flush(self) -> None:
        with self._lock:
            batch, self._pending, self._timer = self._pending, [], None
        sources = list(dict.fromkeys(w.source for w in batch))  # unique, in order
        retry = False
        try:
            if self._before:
                self._before()
            try:
                retry = bool(self._ask(sources))
            except Exception:
                _log.exception("the failure prompt raised; treating as 'do not retry'")
        finally:
            try:
                if self._after:
                    self._after()
            finally:
                for w in batch:
                    w.retry = retry
                    w.event.set()


class _Waiter:
    def __init__(self, source: str) -> None:
        self.source = source
        self.event = _threading.Event()
        self.retry = False
