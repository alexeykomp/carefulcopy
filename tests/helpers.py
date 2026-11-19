import faulthandler
import os
import tempfile
import time
import unittest

from carefulcopy import CopyOptions, Location, LocalBackend, register_backend, unregister_backend

# Watchdog: if the whole run hangs, dump every thread's stack and exit instead of
# blocking forever. (Imported by every test module.)
faulthandler.dump_traceback_later(120, exit=True)


class _FailingWriter:
    """Wraps a real file; raises after *limit* bytes have been written."""

    def __init__(self, fh, limit, delay=0.0):
        self._fh, self._left, self._delay = fh, limit, delay

    def write(self, data):
        if self._delay:
            time.sleep(self._delay)   # pretend the network is not instantaneous
        if self._left is not None and len(data) > self._left:
            self._fh.write(data[: self._left])
            self._fh.flush()
            self._left = 0
            raise ConnectionResetError("simulated connection drop")
        if self._left is not None:
            self._left -= len(data)
        return self._fh.write(data)

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()


class FlakyBackend(LocalBackend):
    """Local backend posing as a remote one; the first N writes die mid-way."""

    is_remote = True

    def __init__(self):
        self.failures = []  # list of byte limits, one per upcoming open_write
        self.write_delay = 0.0  # simulated per-write network latency (seconds)
        self.write_offsets = []  # offsets open_write was called with

    def open_write(self, loc, offset=0):
        self.write_offsets.append(offset)
        fh = super().open_write(loc, offset)
        limit = self.failures.pop(0) if self.failures else None
        if limit is None and not self.write_delay:
            return fh
        return _FailingWriter(fh, limit, self.write_delay)


class Base(unittest.TestCase):
    BLOCK = 1024

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.flaky = FlakyBackend()
        register_backend("flaky", lambda loc: self.flaky)
        self.addCleanup(unregister_backend, "flaky")

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def write_src(self, name="src.bin", size=10 * 1024, mtime=1_700_000_000):
        data = os.urandom(size)
        p = self.path(name)
        with open(p, "wb") as f:
            f.write(data)
        os.utime(p, (mtime, mtime))
        return p, data

    def opts(self, **kw):
        kw.setdefault("block_size", self.BLOCK)
        kw.setdefault("retry_delay", 0)
        kw.setdefault("lock_retry_interval", 0.02)
        return CopyOptions(**kw)

    def flaky_loc(self, *parts):
        return Location("flaky", "", __import__("pathlib").Path(self.path(*parts)))

    def listing(self, *parts):
        return sorted(os.listdir(self.path(*parts)) if parts else os.listdir(self.root))

    def read(self, p):
        with open(p, "rb") as f:
            return f.read()
