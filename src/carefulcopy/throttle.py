"""Bandwidth shaping.

Two ready-made throttles are provided:

* :class:`FixedRateThrottle` - a hard cap, e.g. "never exceed 20 MB/s".
* :class:`AdaptiveThrottle` - learns what the link can do and then uses only a
  fraction of it (default 85 %), so copying stays polite to other users of the
  same network. It is the successor of the original ``BandwidthManager``.

Both are meant to be created **once** and shared by every ``copy_file`` call
(and every thread) that should compete for the same bandwidth::

    throttle = AdaptiveThrottle()
    copy_file(a, b, throttle=throttle)       # from any number of threads

The engine only relies on the small :class:`Throttle` protocol, so you can
supply your own implementation too.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, Deque, Dict, Iterator, Optional, Protocol, Set, Tuple

from .abort import is_aborted
from .location import Location

logger = logging.getLogger("carefulcopy.throttle")

MiB = 1024 * 1024


# --------------------------------------------------------------------------
# Protocols used by the copy engine
# --------------------------------------------------------------------------
class ThrottleWorker(Protocol):
    """Handle for one file transfer, valid inside ``Throttle.worker()``."""

    def acquire(self, nbytes: int, abort: Any = None) -> None:
        """Block until *nbytes* may be written. Must return early if *abort* fires."""

    def report(self, nbytes: int, seconds: float) -> None:
        """Feed back a measurement: *nbytes* crossed the network in *seconds*
        of pure I/O time (waiting inside ``acquire`` is **not** included)."""


class Throttle(Protocol):
    def worker(self, src: Location, dst: Location) -> ContextManager[ThrottleWorker]:
        """Register a transfer from *src* to *dst* for the duration of the ``with``."""


class _NullWorker:
    def acquire(self, nbytes: int, abort: Any = None) -> None:
        return None

    def report(self, nbytes: int, seconds: float) -> None:
        return None


_NULL_WORKER = _NullWorker()


# --------------------------------------------------------------------------
# Token bucket
# --------------------------------------------------------------------------
class TokenBucket:
    """Thread-safe token bucket. ``rate == 0`` means "unlimited"."""

    def __init__(self, rate: float = 0.0, capacity: float = 16 * MiB,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = self.capacity
        self._last = clock()

    def set_rate(self, rate: float) -> None:
        with self._lock:
            self._refill()
            self.rate = max(0.0, float(rate))

    def reset(self) -> None:
        with self._lock:
            self._tokens = self.capacity
            self._last = self._clock()

    def consume(self, nbytes: int, abort: Any = None,
                min_nap: float = 0.001, max_nap: float = 0.05) -> None:
        # A request larger than the bucket could never be fully "in stock", so we
        # only wait until a bucket's worth is available but charge the whole
        # amount: the balance goes negative (debt) and the next caller waits it off.
        # This keeps the long-run rate exact even when block_size > burst.
        need = min(float(nbytes), self.capacity)
        while True:
            with self._lock:
                if self.rate <= 0:
                    return
                self._refill()
                if self._tokens >= need:
                    self._tokens -= float(nbytes)
                    return
                wait = (need - self._tokens) / self.rate
            if is_aborted(abort):
                return
            # Never nap for a vanishing amount of time: it would only busy-spin
            # (and with a coarse or virtual clock the clock might not move at all).
            self._sleep(min(max(wait, min_nap), max_nap))

    def _refill(self) -> None:
        now = self._clock()
        if self.rate > 0 and now > self._last:
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now


# --------------------------------------------------------------------------
# Fixed-rate throttle
# --------------------------------------------------------------------------
class _BucketWorker:
    def __init__(self, bucket: TokenBucket) -> None:
        self._bucket = bucket

    def acquire(self, nbytes: int, abort: Any = None) -> None:
        self._bucket.consume(nbytes, abort)

    def report(self, nbytes: int, seconds: float) -> None:
        return None


class FixedRateThrottle(Throttle):
    """Cap the *combined* speed of all transfers using this object.

    Args:
        bytes_per_second: the cap.
        burst: bucket size in bytes (default: one second's worth).
        limit_local: also throttle local-to-local copies (default: only
            transfers that touch a remote backend are limited).
    """

    def __init__(self, bytes_per_second: float, burst: Optional[float] = None,
                 limit_local: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if bytes_per_second <= 0:
            raise ValueError("bytes_per_second must be positive")
        self._bucket = TokenBucket(bytes_per_second, burst or bytes_per_second, clock, sleep)
        self._worker = _BucketWorker(self._bucket)
        self.limit_local = limit_local

    def set_rate(self, bytes_per_second: float) -> None:
        """Change the cap on the fly (0 disables limiting)."""
        self._bucket.set_rate(bytes_per_second)

    def worker(self, src: Location, dst: Location) -> ContextManager[ThrottleWorker]:
        if src.is_local and dst.is_local and not self.limit_local:
            return nullcontext(_NULL_WORKER)
        return nullcontext(self._worker)


# --------------------------------------------------------------------------
# Adaptive throttle
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Speeds:
    """Measured throughput in bytes/second."""

    download: float  # remote -> local
    upload: float    # local -> remote

    @property
    def total(self) -> float:
        return self.download + self.upload


class _AdaptiveWorker:
    def __init__(self, channel: "_Channel", direction: str, now: float) -> None:
        self._channel = channel
        self.direction = direction
        self.link_speed = 0.0      # bytes per second of pure network time
        self.throughput = 0.0      # bytes per second of wall-clock time
        self.sample_ts: Optional[float] = None
        self.last_report = now

    def acquire(self, nbytes: int, abort: Any = None) -> None:
        self._channel.check_stale()
        self._channel.bucket.consume(nbytes, abort)

    def report(self, nbytes: int, seconds: float) -> None:
        self._channel.record(self, nbytes, seconds)


class _Channel:
    """Bandwidth state shared by all transfers between the same two endpoints."""

    def __init__(self, owner: "AdaptiveThrottle") -> None:
        self.owner = owner
        self.lock = threading.Lock()
        self.bucket = TokenBucket(0.0, owner.burst, owner.clock, owner.sleep)
        self.workers: Set[_AdaptiveWorker] = set()
        self.estimates: Deque[Tuple[float, float]] = deque(maxlen=owner.window)
        self.last_sample: Optional[float] = None
        self.rate = 0.0  # 0 = still learning

    def record(self, w: _AdaptiveWorker, nbytes: int, seconds: float) -> None:
        if seconds < 1e-3 or nbytes <= 0:
            return
        o = self.owner
        now = o.clock()
        with self.lock:
            wall = now - w.last_report
            w.last_report = now
            w.link_speed = nbytes / seconds
            w.throughput = nbytes / wall if wall > 0 else w.link_speed
            w.sample_ts = now
            self.last_sample = now

            # Capacity estimate: mean per-worker link speed, scaled to the number
            # of transfers currently sharing the channel.
            fresh = [x.link_speed for x in self.workers
                     if x.sample_ts is not None and now - x.sample_ts <= o.sample_ttl]
            estimate = (sum(fresh) / len(fresh)) * max(1, len(self.workers))
            self.estimates.append((estimate, now))
            recent = [e for e, ts in self.estimates if now - ts <= o.sample_ttl]
            rate = (sum(recent) / len(recent)) * o.headroom
            if o.max_rate:
                rate = min(rate, o.max_rate)
            self.rate = rate
            n_workers = len(self.workers)
        self.bucket.set_rate(rate)
        logger.debug("channel: %d worker(s), link %.0f KB/s, limit %.0f KB/s",
                     n_workers, estimate / 1024, rate / 1024)

    def check_stale(self) -> None:
        """Forget what we learned if no fresh measurements arrived for a while
        (the network may have changed): go back to unlimited 'learning' mode."""
        now = self.owner.clock()
        with self.lock:
            if self.last_sample is None or now - self.last_sample <= self.owner.sample_ttl:
                return
            self.estimates.clear()
            self.last_sample = None
            self.rate = 0.0
        self.bucket.set_rate(0.0)
        self.bucket.reset()
        logger.debug("channel: measurements are stale, back to learning mode")


class AdaptiveThrottle(Throttle):
    """Learn the link speed, then use only ``headroom`` of it.

    Transfers between the same pair of endpoints (e.g. this machine and
    ``smb://nas``) form a *channel* with one shared token bucket. Every
    ``feedback_after`` bytes the engine reports how fast the network was
    during pure I/O; the throttle estimates the channel's total capacity from
    those samples, smooths it over the last ``window`` estimates and caps
    the channel at ``headroom * capacity``. Until the first sample arrives the
    channel is unlimited ("learning").

    Local-to-local copies are never throttled.

    Args:
        headroom: fraction of the measured capacity to use (0 < x <= 1).
        window: number of recent estimates averaged.
        sample_ttl: seconds after which measurements are considered stale.
        burst: token bucket size in bytes.
        max_rate: optional hard ceiling in bytes/second on top of adaptation.
        separate_directions: give upload and download their own channel
            (default: they share one, which suits half-duplex links like Wi-Fi).
    """

    def __init__(self, headroom: float = 0.85, window: int = 8, sample_ttl: float = 60.0,
                 burst: float = 16 * MiB, max_rate: Optional[float] = None,
                 separate_directions: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not 0 < headroom <= 1:
            raise ValueError("headroom must be in (0, 1]")
        self.headroom, self.window, self.sample_ttl = headroom, window, sample_ttl
        self.burst, self.max_rate = burst, max_rate
        self.separate_directions = separate_directions
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._channels: Dict[Tuple, _Channel] = {}

    # -- engine interface -------------------------------------------------
    @contextmanager
    def worker(self, src: Location, dst: Location) -> Iterator[ThrottleWorker]:
        if src.is_local and dst.is_local:
            yield _NULL_WORKER
            return
        channel = self._channel_for(src, dst)
        w = _AdaptiveWorker(channel, "upload" if src.is_local else "download", self.clock())
        with channel.lock:
            channel.workers.add(w)
        try:
            yield w
        finally:
            with channel.lock:
                channel.workers.discard(w)

    # -- introspection (safe to call from a GUI thread) --------------------
    def rate_for(self, src: Location, dst: Location) -> float:
        """Current limit of the channel in bytes/s (0 while still learning)."""
        with self._lock:
            ch = self._channels.get(self._key(src, dst))
        return ch.rate if ch else 0.0

    def speeds(self, max_age: float = 10.0) -> Speeds:
        """Actual throughput of running transfers, by direction."""
        now = self.clock()
        down = up = 0.0
        with self._lock:
            channels = list(self._channels.values())
        for ch in channels:
            with ch.lock:
                for w in ch.workers:
                    if w.sample_ts is not None and now - w.sample_ts <= max_age:
                        if w.direction == "upload":
                            up += w.throughput
                        else:
                            down += w.throughput
        return Speeds(download=down, upload=up)

    def active_workers(self) -> int:
        with self._lock:
            channels = list(self._channels.values())
        total = 0
        for ch in channels:
            with ch.lock:
                total += len(ch.workers)
        return total

    # -- internals --------------------------------------------------------
    @staticmethod
    def _endpoint(loc: Location) -> Tuple[str, str]:
        return ("file", "") if loc.is_local else (loc.scheme, loc.netloc)

    def _key(self, src: Location, dst: Location) -> Tuple:
        a, b = self._endpoint(src), self._endpoint(dst)
        return (a, b) if self.separate_directions else tuple(sorted((a, b)))

    def _channel_for(self, src: Location, dst: Location) -> _Channel:
        key = self._key(src, dst)
        with self._lock:
            ch = self._channels.get(key)
            if ch is None:
                ch = self._channels[key] = _Channel(self)
            return ch
