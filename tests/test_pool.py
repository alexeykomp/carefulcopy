import threading
import time
import unittest

from carefulcopy import ConnectionPool
from carefulcopy.pool import PoolClosedError, PoolTimeoutError

import helpers  # noqa: F401  (installs the hang watchdog)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class PoolTests(unittest.TestCase):
    def make(self, **kw):
        self.created, self.closed = [], []

        def factory():
            obj = {"id": len(self.created)}
            self.created.append(obj)
            return obj
        kw.setdefault("auto_reap", False)
        pool = ConnectionPool(factory, self.closed.append, **kw)
        self.addCleanup(pool.close)
        return pool

    def test_lifo_reuse_prefers_the_warmest(self):
        pool = self.make(size=3)
        a, b = pool.acquire(), pool.acquire()
        pool.release(a)
        pool.release(b)
        self.assertIs(pool.acquire(), b)
        self.assertIs(pool.acquire(), a)
        self.assertEqual(len(self.created), 2)

    def test_blocks_when_exhausted_until_release(self):
        pool = self.make(size=1)
        first = pool.acquire()
        got = []
        t = threading.Thread(target=lambda: got.append(pool.acquire()))
        t.start()
        time.sleep(0.15)
        self.assertEqual(got, [])
        pool.release(first)
        t.join(2)
        self.assertEqual(got, [first])

    def test_acquire_timeout(self):
        pool = self.make(size=1)
        pool.acquire()
        with self.assertRaises(PoolTimeoutError):
            pool.acquire(timeout=0.1)

    def test_discard_frees_the_slot_and_closes(self):
        pool = self.make(size=1)
        a = pool.acquire()
        pool.discard(a)
        self.assertEqual(self.closed, [a])
        b = pool.acquire(timeout=0.5)
        self.assertIsNot(a, b)
        self.assertEqual(pool.stats()["total"], 1)

    def test_factory_failure_does_not_leak_a_slot(self):
        calls = []

        def factory():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return object()
        pool = ConnectionPool(factory, size=1, auto_reap=False)
        with self.assertRaises(RuntimeError):
            pool.acquire()
        pool.acquire(timeout=0.5)   # would time out if the slot leaked

    def test_reap_closes_only_stale_idle_objects(self):
        clk = Clock()
        pool = self.make(size=3, idle_timeout=10, clock=clk)
        a, b = pool.acquire(), pool.acquire()
        pool.release(a)
        clk.t += 6
        pool.release(b)
        clk.t += 6                       # a idle 12s, b idle 6s
        self.assertEqual(pool.reap(), 1)
        self.assertEqual(self.closed, [a])
        self.assertEqual(pool.stats(), {"total": 1, "idle": 1, "size": 3})

    def test_stale_object_is_not_handed_out(self):
        clk = Clock()
        pool = self.make(size=2, idle_timeout=10, clock=clk)
        a = pool.acquire()
        pool.release(a)
        clk.t += 11
        b = pool.acquire()
        self.assertIsNot(a, b)
        self.assertEqual(self.closed, [a])

    def test_close(self):
        pool = self.make(size=2)
        a, b = pool.acquire(), pool.acquire()
        pool.release(a)
        pool.close()
        self.assertEqual(self.closed, [a])
        with self.assertRaises(PoolClosedError):
            pool.acquire()
        pool.release(b)                  # late return after close: destroyed, not pooled
        self.assertEqual(self.closed, [a, b])

    def test_background_reaper_closes_idle_objects(self):
        pool = self.make(size=1, idle_timeout=0.05, reap_interval=0.02, auto_reap=True)
        pool.release(pool.acquire())
        deadline = time.time() + 3
        while not self.closed and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.closed), 1)

    def test_invalid_size(self):
        with self.assertRaises(ValueError):
            ConnectionPool(dict, size=0)


if __name__ == "__main__":
    unittest.main()
