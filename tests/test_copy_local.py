import os
import threading
import unittest

from carefulcopy import (CopyError, CopyOptions, Location, LocalBackend, SourceChangedError, copy_file)

from helpers import Base


class BasicTests(Base):
    def test_copies_content_and_mtime_without_leftovers(self):
        src, data = self.write_src()
        dst = self.path("out", "nested", "dst.bin")
        self.assertTrue(copy_file(src, dst, options=self.opts()))
        self.assertEqual(self.read(dst), data)
        self.assertEqual(int(os.stat(dst).st_mtime), 1_700_000_000)
        self.assertEqual(self.listing("out", "nested"), ["dst.bin"])

    def test_empty_file(self):
        src, _ = self.write_src(size=0)
        dst = self.path("dst.bin")
        self.assertTrue(copy_file(src, dst, options=self.opts()))
        self.assertEqual(os.path.getsize(dst), 0)

    def test_progress_is_monotonic_and_ends_at_total(self):
        src, data = self.write_src(size=5000)
        seen = []
        copy_file(src, self.path("d"), options=self.opts(), progress=lambda s, c, t: seen.append((c, t)))
        self.assertEqual(seen[0][0], 0)
        self.assertEqual(seen[-1], (5000, 5000))
        self.assertEqual([c for c, _ in seen], sorted(c for c, _ in seen))

    def test_overwrite_flag(self):
        src, data = self.write_src()
        dst = self.path("dst.bin")
        with open(dst, "wb") as f:
            f.write(b"old")
        with self.assertRaises(FileExistsError):
            copy_file(src, dst, options=self.opts(overwrite=False))
        self.assertEqual(self.read(dst), b"old")
        self.assertTrue(copy_file(src, dst, options=self.opts()))
        self.assertEqual(self.read(dst), data)

    def test_missing_source_is_not_retried(self):
        calls = []
        with self.assertRaises(FileNotFoundError):
            copy_file(self.path("nope"), self.path("d"), options=self.opts(),
                      on_failure=lambda *a: calls.append(a) or True)
        self.assertEqual(calls, [])

    def test_directory_source_rejected(self):
        with self.assertRaises(IsADirectoryError):
            copy_file(self.root, self.path("d"), options=self.opts())

    def test_direct_write_threshold_skips_part_file(self):
        src, data = self.write_src(size=100)
        flaky_dst = self.flaky_loc("d.bin")
        copy_file(src, flaky_dst, options=self.opts(direct_write_threshold=1024))
        self.assertEqual(self.read(self.path("d.bin")), data)
        self.assertEqual(self.flaky.write_offsets, [0])


class ResumeTests(Base):
    def test_resumes_existing_part(self):
        src, data = self.write_src(size=10 * 1024)
        dst = self.path("dst.bin")
        with open(self.path(self.part_name(src, "dst.bin")), "wb") as f:
            f.write(data[:4096])
        seen = []
        copy_file(src, dst, options=self.opts(), progress=lambda s, c, t: seen.append(c))
        self.assertEqual(seen[0], 4096)  # started from the existing offset
        self.assertEqual(self.read(dst), data)
        self.assertEqual(self.listing(), ["dst.bin", "src.bin"])

    def test_oversized_part_restarts_from_zero(self):
        src, data = self.write_src(size=2048)
        with open(self.path(self.part_name(src, "dst.bin")), "wb") as f:
            f.write(b"x" * 9999)
        seen = []
        copy_file(src, self.path("dst.bin"), options=self.opts(), progress=lambda s, c, t: seen.append(c))
        self.assertEqual(seen[0], 0)
        self.assertEqual(self.read(self.path("dst.bin")), data)

    def test_resume_disabled(self):
        src, data = self.write_src(size=4096)
        with open(self.path(self.part_name(src, "dst.bin")), "wb") as f:
            f.write(b"junk" * 256)
        seen = []
        copy_file(src, self.path("dst.bin"), options=self.opts(resume=False),
                  progress=lambda s, c, t: seen.append(c))
        self.assertEqual(seen[0], 0)
        self.assertEqual(self.read(self.path("dst.bin")), data)

    def test_stale_parts_of_other_versions_are_removed(self):
        src, data = self.write_src(size=2048)
        stale = self.path("dst.bin.part.111-222")
        unrelated = self.path("dst.bin.part2")
        for p in (stale, unrelated):
            open(p, "wb").close()
        copy_file(src, self.path("dst.bin"), options=self.opts())
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(unrelated))

    def test_stale_parts_kept_when_locks_disabled(self):
        src, _ = self.write_src(size=2048)
        stale = self.path("dst.bin.part.111-222")
        open(stale, "wb").close()
        copy_file(src, self.path("dst.bin"), options=self.opts(use_locks=False))
        self.assertTrue(os.path.exists(stale))


class ResumeSafetyTests(Base):
    def abort_midway(self, src, dst, at=6000, **kw):
        ev = threading.Event()
        copy_file(src, dst, abort=ev, options=self.opts(keep_partial_on_abort=True, **kw),
                  progress=lambda s, c, t: ev.set() if c >= at else None)
        return [n for n in os.listdir(os.path.dirname(dst)) if ".part." in n]

    def test_a_different_source_with_the_same_size_and_mtime_is_never_resumed(self):
        a, A = self.write_src("a.bin", size=10 * 1024)
        b, B = self.write_src("b.bin", size=10 * 1024)     # same size, same mtime, other content
        self.assertNotEqual(A, B)
        dst = self.path("d.bin")
        self.assertEqual(len(self.abort_midway(a, dst)), 1)       # A's copy dies half way
        seen = []
        copy_file(b, dst, options=self.opts(), progress=lambda s, c, t: seen.append(c))
        self.assertEqual(seen[0], 0)                               # A's partial file was not touched
        self.assertEqual(self.read(dst), B)                        # (it used to yield A's first bytes + B's rest)
        self.assertEqual([n for n in os.listdir(self.root) if ".part." in n], [])

    def test_the_same_file_from_another_path_starts_over(self):
        a, A = self.write_src("a.bin", size=10 * 1024)
        c = self.path("copy-of-a.bin")
        with open(c, "wb") as f:
            f.write(A)
        os.utime(c, (1_700_000_000, 1_700_000_000))
        dst = self.path("d.bin")
        self.abort_midway(a, dst)
        seen = []
        copy_file(c, dst, options=self.opts(), progress=lambda s, c_, t: seen.append(c_))
        self.assertEqual(seen[0], 0)                               # accepted trade-off: path is part of identity
        self.assertEqual(self.read(dst), A)

    def test_the_part_name_encodes_size_mtime_and_source(self):
        from carefulcopy.copier import part_filename
        n1 = part_filename("d", "/x/a", 10, 5)
        self.assertNotEqual(n1, part_filename("d", "/x/b", 10, 5))
        self.assertNotEqual(n1, part_filename("d", "/x/a", 11, 5))
        self.assertNotEqual(n1, part_filename("d", "/x/a", 10, 6))
        self.assertEqual(n1, part_filename("d", "/x/a", 10, 5))

    def test_sub_second_mtime_changes_are_noticed(self):
        a, _ = self.write_src("a.bin", size=4096)
        before = self.part_name(a, "d")
        os.utime(a, ns=(1_700_000_000_000_000_000, 1_700_000_000_500_000_000))
        self.assertNotEqual(before, self.part_name(a, "d"))

    def test_healthy_partial_file_is_resumed_without_restarting(self):
        src, data = self.write_src(size=10 * 1024)
        dst = self.path("d.bin")
        self.abort_midway(src, dst, at=6000)
        seen = []
        copy_file(src, dst, options=self.opts(), progress=lambda s, c, t: seen.append(c))
        self.assertGreaterEqual(seen[0], 6000)
        self.assertNotIn(0, seen[1:])
        self.assertEqual(self.read(dst), data)

    def corrupt_tail(self, part_path, n=300):
        size = os.path.getsize(part_path)
        with open(part_path, "r+b") as f:
            f.seek(size - n)
            f.write(b"\0" * n)
        return size

    def test_a_torn_tail_is_detected_and_the_copy_restarts_from_zero(self):
        src, data = self.write_src(size=10 * 1024)
        dst = self.path("d.bin")
        (part,) = self.abort_midway(src, dst)
        size = self.corrupt_tail(self.path(part))
        seen = []
        copy_file(src, dst, options=self.opts(), progress=lambda s, c, t: seen.append(c))
        self.assertEqual(seen[0], size)
        self.assertIn(0, seen[1:])
        self.assertEqual(self.read(dst), data)

    def test_verification_can_be_switched_off_and_then_corruption_slips_through(self):
        src, data = self.write_src(size=10 * 1024)
        dst = self.path("d.bin")
        (part,) = self.abort_midway(src, dst)
        self.corrupt_tail(self.path(part))
        copy_file(src, dst, options=self.opts(resume_verify="none"))
        self.assertNotEqual(self.read(dst), data)                  # the documented price of "none"

    def test_only_the_configured_tail_is_compared(self):
        src, data = self.write_src(size=10 * 1024)
        dst = self.path("d.bin")
        (part,) = self.abort_midway(src, dst)
        p = self.path(part)
        size = os.path.getsize(p)
        with open(p, "r+b") as f:                                  # damage far from the tail
            f.seek(100)
            f.write(b"\xff" * 50)
        copy_file(src, dst, options=self.opts(resume_verify_bytes=256))
        self.assertNotEqual(self.read(dst), data)                  # outside the window: not noticed
        self.assertGreaterEqual(size, 6000)

    def test_invalid_verify_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            CopyOptions(resume_verify="sometimes")

    def test_file_info_derives_nanoseconds_when_the_backend_has_none(self):
        from carefulcopy import FileInfo
        self.assertEqual(FileInfo(1, 1700000000.5).mtime_key(), 1_700_000_000_500_000_000)
        self.assertEqual(FileInfo(1, 1.0, mtime_ns=7).mtime_key(), 7)


class RetryTests(Base):
    def test_transient_failure_resumes_and_succeeds(self):
        src, data = self.write_src(size=10 * 1024)
        self.flaky.failures = [3500]
        dst = self.flaky_loc("dst.bin")
        self.assertTrue(copy_file(src, dst, options=self.opts()))
        self.assertEqual(self.read(self.path("dst.bin")), data)
        self.assertEqual(self.flaky.write_offsets, [0, 3500])  # second attempt resumed
        self.assertEqual(self.listing(), ["dst.bin", "src.bin"])

    def test_repeated_failures_keep_making_progress(self):
        src, data = self.write_src(size=10 * 1024)
        self.flaky.failures = [2000, 2000]
        copy_file(src, self.flaky_loc("dst.bin"), options=self.opts(retries=3))
        self.assertEqual(self.flaky.write_offsets, [0, 2000, 4000])
        self.assertEqual(self.read(self.path("dst.bin")), data)

    def test_retries_exhausted_raises_and_keeps_part(self):
        src, _ = self.write_src(size=10 * 1024)
        self.flaky.failures = [1000, 1000]
        with self.assertRaises(CopyError) as cm:
            copy_file(src, self.flaky_loc("dst.bin"), options=self.opts(retries=2))
        self.assertIsInstance(cm.exception.__cause__, ConnectionResetError)
        self.assertFalse(os.path.exists(self.path("dst.bin")))
        self.assertTrue(any(".part." in n for n in self.listing()))

    def test_on_failure_true_grants_more_attempts(self):
        src, data = self.write_src(size=10 * 1024)
        self.flaky.failures = [1000, 1000, 1000]
        asked = []
        ok = copy_file(src, self.flaky_loc("dst.bin"), options=self.opts(retries=2),
                       on_failure=lambda s, e: asked.append(s) or True)
        self.assertTrue(ok)
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.read(self.path("dst.bin")), data)

    def test_on_failure_false_raises(self):
        src, _ = self.write_src(size=10 * 1024)
        self.flaky.failures = [1000, 1000]
        with self.assertRaises(CopyError):
            copy_file(src, self.flaky_loc("dst.bin"), options=self.opts(retries=2),
                      on_failure=lambda s, e: False)

    def test_non_transient_error_propagates_unchanged(self):
        src, _ = self.write_src()
        self.flaky.failures = []
        orig = self.flaky.open_write

        def boom(loc, offset=0):
            raise PermissionError("nope")
        self.flaky.open_write = boom
        with self.assertRaises(PermissionError):
            copy_file(src, self.flaky_loc("dst.bin"), options=self.opts())
        self.flaky.open_write = orig


class AbortTests(Base):
    def test_abort_before_start(self):
        src, _ = self.write_src()
        self.assertFalse(copy_file(src, self.path("d"), abort=True, options=self.opts()))
        self.assertEqual(self.listing(), ["src.bin"])

    def test_abort_midway_cleans_up(self):
        src, _ = self.write_src(size=10 * 1024)
        ev = threading.Event()
        res = copy_file(src, self.path("d"), abort=ev, options=self.opts(),
                        progress=lambda s, c, t: ev.set() if c >= 2048 else None)
        self.assertFalse(res)
        self.assertEqual(self.listing(), ["src.bin"])

    def test_abort_can_keep_partial_for_later_resume(self):
        src, data = self.write_src(size=10 * 1024)
        ev = threading.Event()
        copy_file(src, self.path("d"), abort=ev, options=self.opts(keep_partial_on_abort=True),
                  progress=lambda s, c, t: ev.set() if c >= 2048 else None)
        parts = [n for n in self.listing() if ".part." in n]
        self.assertEqual(len(parts), 1)
        seen = []
        copy_file(src, self.path("d"), options=self.opts(), progress=lambda s, c, t: seen.append(c))
        self.assertGreaterEqual(seen[0], 2048)
        self.assertEqual(self.read(self.path("d")), data)

    def test_abort_during_retry_wait(self):
        src, _ = self.write_src(size=10 * 1024)
        self.flaky.failures = [1000]
        ev = threading.Event()
        threading.Timer(0.2, ev.set).start()
        res = copy_file(src, self.flaky_loc("dst.bin"), abort=ev,
                        options=self.opts(retry_delay=30))
        self.assertFalse(res)


class SourceChangeTests(Base):
    def test_modification_during_copy_is_detected(self):
        src, _ = self.write_src(size=10 * 1024)
        state = {"done": False}

        def touch(s, c, t):
            if c >= 2048 and not state["done"]:
                state["done"] = True
                os.utime(src, (1_800_000_000, 1_800_000_000))
        with self.assertRaises(SourceChangedError):
            copy_file(src, self.path("d"), options=self.opts(), progress=touch)
        self.assertFalse(os.path.exists(self.path("d")))
        self.assertEqual([n for n in self.listing() if ".part." in n], [])

    def test_modification_between_retries_is_detected(self):
        src, _ = self.write_src(size=10 * 1024)
        self.flaky.failures = [3000]  # first attempt drops the connection
        orig = self.flaky.open_write
        calls = {"n": 0}

        def open_write(loc, offset=0):
            fh = orig(loc, offset)
            calls["n"] += 1
            if calls["n"] == 1:  # source changes before the retry starts
                os.utime(src, (1_800_000_000, 1_800_000_000))
            return fh
        self.flaky.open_write = open_write
        with self.assertRaises(SourceChangedError):
            copy_file(src, self.flaky_loc("dst.bin"), options=self.opts())


class LockTests(Base):
    def test_waits_then_times_out_when_locked(self):
        src, _ = self.write_src()
        dst = self.path("dst.bin")
        be = LocalBackend()
        with be.lock(Location.local(dst)):
            with self.assertRaises(CopyError) as cm:
                copy_file(src, dst, options=self.opts(lock_wait_timeout=0.2))
            self.assertIn("locked", str(cm.exception))
        self.assertFalse(os.path.exists(dst))

    def test_proceeds_once_lock_released(self):
        src, data = self.write_src()
        dst = self.path("dst.bin")
        be = LocalBackend()
        cm = be.lock(Location.local(dst))
        cm.__enter__()
        threading.Timer(0.2, lambda: cm.__exit__(None, None, None)).start()
        self.assertTrue(copy_file(src, dst, options=self.opts(lock_wait_timeout=5)))
        self.assertEqual(self.read(dst), data)
        self.assertEqual(self.listing(), ["dst.bin", "src.bin"])

    def test_abort_while_waiting_for_lock(self):
        src, _ = self.write_src()
        dst = self.path("dst.bin")
        ev = threading.Event()
        threading.Timer(0.2, ev.set).start()
        with LocalBackend().lock(Location.local(dst)):
            self.assertFalse(copy_file(src, dst, abort=ev, options=self.opts(lock_wait_timeout=30)))

    def test_lock_file_removed_after_copy(self):
        src, _ = self.write_src()
        copy_file(src, self.path("dst.bin"), options=self.opts())
        self.assertNotIn("dst.bin.lock", self.listing())


class ThrottleHookTests(Base):
    """The engine's side of the throttle contract."""

    class Rec:
        def __init__(self):
            self.acquired, self.reports, self.entered, self.exited = [], [], 0, 0

        def worker(self, src, dst):
            rec = self

            class Ctx:
                def __enter__(self_):
                    rec.entered += 1
                    return rec

                def __exit__(self_, *exc):
                    rec.exited += 1
            return Ctx()

        def acquire(self, n, abort=None):
            self.acquired.append(n)

        def report(self, n, s):
            self.reports.append((n, s))

    def test_acquire_per_data_block_and_periodic_reports(self):
        src, _ = self.write_src(size=10 * 1024)
        rec = self.Rec()
        copy_file(src, self.flaky_loc("d"), throttle=rec, options=self.opts(feedback_after=4096))
        self.assertEqual(rec.acquired, [1024] * 10)
        self.assertEqual([n for n, _ in rec.reports], [4096, 4096])
        self.assertEqual((rec.entered, rec.exited), (1, 1))

    def test_no_reports_for_purely_local_copies(self):
        src, _ = self.write_src(size=10 * 1024)
        rec = self.Rec()
        copy_file(src, self.path("d"), throttle=rec, options=self.opts(feedback_after=1024))
        self.assertEqual(rec.reports, [])

    def test_worker_is_released_when_copy_fails(self):
        src, _ = self.write_src(size=10 * 1024)
        rec = self.Rec()
        self.flaky.failures = [1000]
        copy_file(src, self.flaky_loc("d"), throttle=rec, options=self.opts())
        self.assertEqual(rec.entered, 2)  # one per attempt
        self.assertEqual(rec.exited, 2)


if __name__ == "__main__":
    unittest.main()
