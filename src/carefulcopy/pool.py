"""A small thread-safe pool for expensive, reusable objects (connections)."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Generic, List, Optional, Tuple, TypeVar

logger = logging.getLogger("carefulcopy.pool")

T = TypeVar("T")


class PoolClosedError(RuntimeError):
    pass


class PoolTimeoutError(TimeoutError):
    pass


class ConnectionPool(Generic[T]):
    """Lazily creates up to *size* objects, hands them out LIFO (the "warmest"
    first) and closes the ones idle for longer than *idle_timeout*.

    * ``acquire()`` blocks while all *size* objects are in use.
    * ``release(obj)`` returns a healthy object, ``discard(obj)`` destroys a
      broken one and frees its slot.
    * A background thread calls :meth:`reap` periodically; call :meth:`close`
      (or use the pool as a context manager) to stop it.
    """

    def __init__(self, factory: Callable[[], T], closer: Optional[Callable[[T], None]] = None,
                 size: int = 8, idle_timeout: float = 30.0, reap_interval: float = 5.0,
                 clock: Callable[[], float] = time.monotonic, auto_reap: bool = True) -> None:
        if size < 1:
            raise ValueError("size must be >= 1")
        self._factory, self._closer = factory, closer
        self.size, self.idle_timeout = size, idle_timeout
        self._clock = clock
        self._cond = threading.Condition(threading.Lock())
        self._idle: Deque[Tuple[T, float]] = deque()
        self._total = 0
        self._closed = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if auto_reap and idle_timeout > 0:
            self._thread = threading.Thread(
                target=self._reap_loop, args=(reap_interval,), daemon=True, name="carefulcopy-pool-reaper")
            self._thread.start()

    # -- public API -------------------------------------------------------
    def acquire(self, timeout: Optional[float] = None) -> T:
        deadline = None if timeout is None else time.monotonic() + timeout
        stale: List[T] = []
        try:
            with self._cond:
                while True:
                    if self._closed:
                        raise PoolClosedError("pool is closed")
                    while self._idle:
                        obj, ts = self._idle.pop()              # newest first
                        if self.idle_timeout > 0 and self._clock() - ts > self.idle_timeout:
                            stale.append(obj)
                            self._total -= 1
                            continue
                        return obj
                    if self._total < self.size:
                        self._total += 1                        # reserve the slot, create outside the lock
                        break
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise PoolTimeoutError(f"no free connection after {timeout:g}s (pool size {self.size})")
                    self._cond.wait(remaining)
        finally:
            self._close_all(stale)
        try:
            return self._factory()
        except BaseException:
            with self._cond:
                self._total -= 1
                self._cond.notify()
            raise

    def release(self, obj: T) -> None:
        with self._cond:
            if not self._closed:
                self._idle.append((obj, self._clock()))
                self._cond.notify()
                return
            self._total -= 1
        self._close_all([obj])

    def discard(self, obj: T) -> None:
        with self._cond:
            self._total -= 1
            self._cond.notify()
        self._close_all([obj])

    def reap(self) -> int:
        """Close objects idle for longer than ``idle_timeout``. Returns how many."""
        if self.idle_timeout <= 0:
            return 0
        now = self._clock()
        victims: List[T] = []
        with self._cond:
            while self._idle and now - self._idle[0][1] > self.idle_timeout:
                victims.append(self._idle.popleft()[0])
                self._total -= 1
            if victims:
                self._cond.notify_all()
        self._close_all(victims)
        return len(victims)

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            victims = [o for o, _ in self._idle]
            self._idle.clear()
            self._total -= len(victims)
            self._cond.notify_all()
        self._stop.set()
        self._close_all(victims)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)

    def stats(self) -> Dict[str, int]:
        with self._cond:
            return {"total": self._total, "idle": len(self._idle), "size": self.size}

    def __enter__(self) -> "ConnectionPool[T]":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- internals --------------------------------------------------------
    def _reap_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.reap()
            except Exception:  # pragma: no cover - never let the reaper die
                logger.exception("pool reaper failed")

    def _close_all(self, objs: List[T]) -> None:
        # Always called without holding the lock: closing may touch the network.
        for obj in objs:
            if self._closer is None:
                continue
            try:
                self._closer(obj)
            except Exception as exc:
                logger.warning("error closing pooled object: %s", exc)
