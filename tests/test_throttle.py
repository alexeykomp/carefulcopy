import threading
import time
import unittest
from pathlib import Path, PurePosixPath

from carefulcopy import (AdaptiveThrottle, CopyOptions, FixedRateThrottle, Location, TokenBucket,
                         copy_file)

from helpers import Base

MiB = 1024 * 1024
LOCAL = Location.local("/tmp/a")
NAS = Location("smb", "nas", PurePosixPath("/share/a"))
NAS2 = Location("smb", "other", PurePosixPath("/share/a"))


class FakeClock:
    """Virtual time: sleeping just moves the clock forward."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class TokenBucketTests(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock()

    def test_request_larger_than_bucket_is_charged_in_full(self):
        """Regression: only `capacity` bytes used to be charged, so with
        block_size > burst the configured rate was not enforced at all."""
        b = self.bucket(1000, 100)
        start = self.clk.t
        for _ in range(5):
            b.consume(1000)                      # 10x the bucket
        self.assertAlmostEqual((self.clk.t - start), 4.0, delta=0.2)   # 5000 B at 1000 B/s, first one free-ish

    def test_tiny_shortfall_does_not_stall_the_clock(self):
        """Regression: a ~1e-16 s wait is lost to float rounding on a virtual clock,
        so the clock never advanced and consume() looped forever."""
        b = self.bucket(8.5 * MiB, 2 * MiB)
        b._tokens = MiB - 1e-9          # a hair short of one block
        b.consume(MiB)                  # must return (the suite watchdog catches a hang)
        self.assertGreater(self.clk.t, 1000.0)

    def bucket(self, rate, capacity):
        return TokenBucket(rate, capacity, self.clk, self.clk.sleep)

    def test_burst_then_paced(self):
        b = self.bucket(1000, 1000)
        start = self.clk.t
        b.consume(1000)
        self.assertEqual(self.clk.t, start)             # burst is free
        b.consume(500)
        self.assertAlmostEqual(self.clk.t - start, 0.5, delta=0.06)

    def test_zero_rate_is_unlimited(self):
        b = self.bucket(0, 10)
        start = self.clk.t
        for _ in range(100):
            b.consume(10_000)
        self.assertEqual(self.clk.t, start)

    def test_request_larger_than_capacity_still_completes(self):
        b = self.bucket(1000, 100)
        b.consume(10_000)  # clamped to capacity, must not spin forever

    def test_abort_interrupts_wait(self):
        b = self.bucket(1, 1)
        b.consume(1)
        start = self.clk.t
        b.consume(1_000_000, abort=lambda: self.clk.t - start > 0.2)
        self.assertLess(self.clk.t - start, 1.0)

    def test_rate_change_applies_to_future_refill(self):
        b = self.bucket(100, 100)
        b.consume(100)
        self.clk.t += 1          # +100 tokens at the old rate
        b.set_rate(1000)
        start = self.clk.t
        b.consume(100)           # already available
        self.assertEqual(self.clk.t, start)


class FixedRateTests(unittest.TestCase):
    def test_caps_combined_throughput(self):
        clk = FakeClock()
        t = FixedRateThrottle(1 * MiB, burst=64 * 1024, clock=clk, sleep=clk.sleep)
        start = clk.t
        with t.worker(LOCAL, NAS) as w1, t.worker(NAS, LOCAL) as w2:   # shared bucket
            for _ in range(20):
                w1.acquire(256 * 1024)
                w2.acquire(256 * 1024)
        moved = 40 * 256 * 1024
        self.assertAlmostEqual(moved / (clk.t - start), 1 * MiB, delta=0.1 * MiB)

    def test_local_copies_are_not_limited_by_default(self):
        clk = FakeClock()
        t = FixedRateThrottle(10, clock=clk, sleep=clk.sleep)
        start = clk.t
        with t.worker(LOCAL, Location.local("/tmp/b")) as w:
            w.acquire(10 * MiB)
        self.assertEqual(clk.t, start)
        limited = FixedRateThrottle(1000, limit_local=True, clock=clk, sleep=clk.sleep)
        with limited.worker(LOCAL, Location.local("/tmp/b")) as w:
            w.acquire(1000)
            w.acquire(1000)
        self.assertGreater(clk.t, start)

    def test_invalid_rate(self):
        with self.assertRaises(ValueError):
            FixedRateThrottle(0)


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock()

    def throttle(self, **kw):
        return AdaptiveThrottle(clock=self.clk, sleep=self.clk.sleep, **kw)

    # -- learning ---------------------------------------------------------
    def test_unlimited_until_first_sample(self):
        t = self.throttle()
        start = self.clk.t
        with t.worker(LOCAL, NAS) as w:
            for _ in range(100):
                w.acquire(8 * MiB)
        self.assertEqual(self.clk.t, start)
        self.assertEqual(t.rate_for(LOCAL, NAS), 0.0)

    def test_single_worker_settles_near_headroom_of_link_speed(self):
        """Simulated 10 MiB/s link, 1 MiB blocks: long-run speed should be ~85 %."""
        link = 10 * MiB
        block = 1 * MiB
        t = self.throttle(burst=2 * MiB)
        with t.worker(LOCAL, NAS) as w:
            net, sampled = 0.0, 0
            marks = []
            for i in range(300):
                w.acquire(block)
                self.clk.t += block / link        # the network I/O itself
                net += block / link
                sampled += block
                if sampled >= 8 * MiB:
                    w.report(sampled, net)
                    net, sampled = 0.0, 0
                if i in (99, 299):
                    marks.append((self.clk.t, (i + 1) * block))
        (t0, b0), (t1, b1) = marks
        speed = (b1 - b0) / (t1 - t0)
        self.assertAlmostEqual(speed / link, 0.85, delta=0.04)
        self.assertAlmostEqual(t.rate_for(LOCAL, NAS) / link, 0.85, delta=0.01)

    def test_headroom_is_configurable_and_max_rate_caps(self):
        t = self.throttle(headroom=0.5)
        with t.worker(LOCAL, NAS) as w:
            w.report(10 * MiB, 1.0)
        self.assertAlmostEqual(t.rate_for(LOCAL, NAS), 5 * MiB)
        capped = self.throttle(max_rate=2 * MiB)
        with capped.worker(LOCAL, NAS) as w:
            w.report(10 * MiB, 1.0)
        self.assertEqual(capped.rate_for(LOCAL, NAS), 2 * MiB)

    # -- several workers --------------------------------------------------
    def test_estimate_scales_with_number_of_workers(self):
        t = self.throttle()
        with t.worker(LOCAL, NAS) as a, t.worker(LOCAL, NAS) as b, t.worker(NAS, LOCAL):
            a.report(10 * MiB, 2.0)   # 5 MiB/s each while sharing
            b.report(10 * MiB, 2.0)
            # 3 workers registered, the 3rd has no sample yet -> assume the same speed
            self.assertAlmostEqual(t.rate_for(LOCAL, NAS), 5 * MiB * 3 * 0.85)
            self.assertEqual(t.active_workers(), 3)
        self.assertEqual(t.active_workers(), 0)

    def test_moving_average_over_window(self):
        t = self.throttle(window=2, headroom=1.0)
        with t.worker(LOCAL, NAS) as w:
            for mib_per_s in (10, 20, 30):
                w.report(mib_per_s * MiB, 1.0)
        self.assertAlmostEqual(t.rate_for(LOCAL, NAS), 25 * MiB)  # mean of last two

    def test_learned_rate_survives_between_files(self):
        t = self.throttle()
        with t.worker(LOCAL, NAS) as w:
            w.report(10 * MiB, 1.0)
        rate = t.rate_for(LOCAL, NAS)
        self.assertGreater(rate, 0)
        self.clk.t += 30
        with t.worker(LOCAL, NAS) as w:       # next file, same channel
            w.acquire(1)
            self.assertEqual(t.rate_for(LOCAL, NAS), rate)

    def test_stale_measurements_reset_to_learning(self):
        t = self.throttle(sample_ttl=60)
        with t.worker(LOCAL, NAS) as w:
            w.report(10 * MiB, 1.0)
            self.clk.t += 61
            w.acquire(1)
        self.assertEqual(t.rate_for(LOCAL, NAS), 0.0)

    # -- channels ---------------------------------------------------------
    def test_directions_share_a_channel_by_default(self):
        t = self.throttle()
        with t.worker(LOCAL, NAS) as w:
            w.report(10 * MiB, 1.0)
        self.assertGreater(t.rate_for(NAS, LOCAL), 0)
        self.assertEqual(t.rate_for(LOCAL, NAS2), 0)      # other host: other channel

    def test_separate_directions(self):
        t = self.throttle(separate_directions=True)
        with t.worker(LOCAL, NAS) as w:
            w.report(10 * MiB, 1.0)
        self.assertGreater(t.rate_for(LOCAL, NAS), 0)
        self.assertEqual(t.rate_for(NAS, LOCAL), 0)

    def test_local_to_local_is_never_throttled(self):
        t = self.throttle()
        with t.worker(LOCAL, Location.local("/tmp/b")) as w:
            w.report(10 * MiB, 1.0)
            w.acquire(10**9)
        self.assertEqual(t.active_workers(), 0)

    # -- statistics for a UI ----------------------------------------------
    def test_speeds_by_direction_use_wall_clock_throughput(self):
        t = self.throttle()
        with t.worker(LOCAL, NAS) as up, t.worker(NAS, LOCAL) as down:
            self.clk.t += 2.0
            up.report(8 * MiB, 1.0)       # 8 MiB in 2 s wall time -> 4 MiB/s
            down.report(2 * MiB, 0.5)     # 2 MiB in 2 s wall time -> 1 MiB/s
            s = t.speeds()
        self.assertAlmostEqual(s.upload, 4 * MiB)
        self.assertAlmostEqual(s.download, 1 * MiB)
        self.assertAlmostEqual(s.total, 5 * MiB)
        self.assertEqual(t.speeds().total, 0.0)    # workers gone

    def test_ignores_degenerate_samples(self):
        t = self.throttle()
        with t.worker(LOCAL, NAS) as w:
            w.report(0, 1.0)
            w.report(1000, 0.0)
        self.assertEqual(t.rate_for(LOCAL, NAS), 0.0)

    def test_invalid_headroom(self):
        for bad in (0, -1, 1.5):
            with self.assertRaises(ValueError):
                AdaptiveThrottle(headroom=bad)


class EngineIntegrationTests(Base):
    def test_fixed_rate_really_slows_the_copy(self):
        src, data = self.write_src(size=100 * 1024)
        t = FixedRateThrottle(400 * 1024, burst=4 * 1024)
        start = time.monotonic()
        copy_file(src, self.flaky_loc("d"), throttle=t, options=self.opts())
        elapsed = time.monotonic() - start
        self.assertGreater(elapsed, 0.2)          # 100 KiB at 400 KiB/s = 0.25 s
        self.assertEqual(self.read(self.path("d")), data)

    def test_abort_is_prompt_even_with_a_crawling_limit(self):
        src, _ = self.write_src(size=100 * 1024)
        t = FixedRateThrottle(100, burst=100)     # 100 B/s: a block would take ~10 s
        ev = threading.Event()
        threading.Timer(0.2, ev.set).start()
        start = time.monotonic()
        ok = copy_file(src, self.flaky_loc("d"), throttle=t, abort=ev, options=self.opts())
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual(self.listing(), ["src.bin"])

    def test_adaptive_learns_from_a_real_copy(self):
        src, data = self.write_src(size=64 * 1024)
        t = AdaptiveThrottle()
        self.flaky.write_delay = 0.002   # local disk is too fast: samples <1 ms are ignored
        copy_file(src, self.flaky_loc("d"), throttle=t, options=self.opts(feedback_after=8 * 1024))
        self.assertEqual(self.read(self.path("d")), data)
        self.assertGreater(t.rate_for(Location.local(src), self.flaky_loc("d")), 0)

    def test_parallel_copies_share_one_throttle(self):
        files = [self.write_src(name=f"s{i}.bin", size=32 * 1024) for i in range(4)]
        t = AdaptiveThrottle()
        errors = []

        def run(i):
            try:
                copy_file(files[i][0], self.flaky_loc(f"d{i}.bin"), throttle=t,
                          options=self.opts(feedback_after=4 * 1024))
            except Exception as exc:       # pragma: no cover
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
        [th.start() for th in threads]
        [th.join(10) for th in threads]
        self.assertEqual(errors, [])
        for i, (_, data) in enumerate(files):
            self.assertEqual(self.read(self.path(f"d{i}.bin")), data)
        self.assertEqual(t.active_workers(), 0)


if __name__ == "__main__":
    unittest.main()
