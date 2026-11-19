"""Normalisation of the many ways a caller may signal "please stop"."""
from __future__ import annotations

import time
from typing import Any


def is_aborted(trigger: Any) -> bool:
    """Return True if *trigger* says the operation should stop.

    Accepted forms:

    * ``None``                                   -> never aborted
    * ``bool``                                   -> its value
    * ``threading.Event`` / ``multiprocessing.Event`` (anything with ``is_set()``)
    * ``multiprocessing.Value`` and similar (anything with ``.value``)
    * a zero-argument callable returning a truthy value
    """
    if trigger is None:
        return False
    if isinstance(trigger, bool):
        return trigger
    is_set = getattr(trigger, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if hasattr(trigger, "value"):
        return bool(trigger.value)
    if callable(trigger):
        return bool(trigger())
    return bool(trigger)


def sleep_abortable(seconds: float, trigger: Any, step: float = 0.1) -> bool:
    """Sleep up to *seconds*, waking early if *trigger* fires.

    Returns True if the sleep was cut short by an abort request.
    """
    deadline = time.monotonic() + seconds
    while True:
        if is_aborted(trigger):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(step, remaining))
