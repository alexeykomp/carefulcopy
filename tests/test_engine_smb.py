import os
import threading
import time
import unittest

from carefulcopy import (AuthenticationError, BatchedFailurePrompt, CopyError, CopyOptions, StaticCredentials,
                         copy_file)

from helpers_smb import SMBCase

MTIME = 1_700_000_000


class CopyOverSMBTests(SMBCase):
    def test_upload_download_and_server_side_copy(self):
        src, data = self.write_src(size=10 * 1024)
        up = self.uri("out/nested/d.bin")
        self.assertTrue(copy_file(src, up, options=self.opts()))
        self.assertEqual(self.server.get("nas", "share", "out/nested/d.bin"), data)
        self.assertEqual(int(self.server.files[("nas", "share", "out", "nested", "d.bin")].mtime), MTIME)
        self.assertEqual(self.server.names("nas", "share", "out/nested"), ["d.bin"])   # no .part, no .lock

        down = self.path("down.bin")
        self.assertTrue(copy_file(up, down, options=self.opts()))
        self.assertEqual(self.read(down), data)
        self.assertEqual(int(os.stat(down).st_mtime), MTIME)

        self.assertTrue(copy_file(up, self.uri("copy.bin"), options=self.opts()))
        self.assertEqual(self.server.get("nas", "share", "copy.bin"), data)
        self.assertEqual(self.server.handles, [])

    def test_file_in_share_root(self):
        src, data = self.write_src(size=2048)
        copy_file(src, self.uri("root.bin"), options=self.opts())
        self.assertEqual(self.server.get("nas", "share", "root.bin"), data)

    def test_existing_destination_stays_intact_until_the_final_rename(self):
        src, data = self.write_src(size=10 * 1024)
        self.server.put("nas", "share", "d.bin", b"OLD CONTENT")
        seen = []

        def spy(_s, copied, total):
            if 0 < copied < total:
                seen.append((self.server.get("nas", "share", "d.bin"),
                             [n for n in self.server.names("nas", "share") if ".part." in n]))
        copy_file(src, self.uri("d.bin"), options=self.opts(), progress=spy)
        self.assertTrue(seen)
        for content, parts in seen:
            self.assertEqual(content, b"OLD CONTENT")
            self.assertEqual(len(parts), 1)
        self.assertEqual(self.server.get("nas", "share", "d.bin"), data)

    def test_overwrite_false(self):
        src, _ = self.write_src()
        self.server.put("nas", "share", "d.bin", b"keep")
        with self.assertRaises(FileExistsError):
            copy_file(src, self.uri("d.bin"), options=self.opts(overwrite=False))
        self.assertEqual(self.server.get("nas", "share", "d.bin"), b"keep")

    def test_missing_source_on_the_server_is_not_retried(self):
        with self.assertRaises(FileNotFoundError):
            copy_file(self.uri("nope.bin"), self.path("x"), options=self.opts())
        self.assertEqual(len(self.server.register_calls), 1)


class ResumeOverSMBTests(SMBCase):
    def test_connection_drop_mid_write_resumes_on_a_fresh_connection(self):
        src, data = self.write_src(size=10 * 1024)
        self.server.write_failures = [3500]           # the first open of the .part file dies after 3500 bytes
        self.assertTrue(copy_file(src, self.uri("d.bin"), options=self.opts()))
        self.assertEqual(self.server.write_failures, [])           # the failure really was consumed
        self.assertEqual(self.server.get("nas", "share", "d.bin"), data)
        part_opens = [(m, s) for p, m, s in self.server.open_log if ".part." in p]
        self.assertEqual([m for m, _ in part_opens], ["wb", "r+b"])       # fresh, then resumed
        self.assertGreaterEqual(self.server.reset_calls, 1)               # broken connection destroyed
        self.assertEqual(self.server.names("nas", "share"), ["d.bin"])    # part and lock cleaned up
        self.assertEqual(self.server.handles, [])

    def test_several_drops_in_a_row(self):
        src, data = self.write_src(size=12 * 1024)
        self.server.write_failures = [2000, 2000]
        copy_file(src, self.uri("d.bin"), options=self.opts(retries=4))
        self.assertEqual(self.server.write_failures, [])
        self.assertEqual(self.server.get("nas", "share", "d.bin"), data)
        modes = [m for p, m, s in self.server.open_log if ".part." in p]
        self.assertEqual(modes, ["wb", "r+b", "r+b"])             # fresh, resumed, resumed again
        self.assertEqual(self.server.reset_calls, 2)              # each broken connection destroyed

    def test_resume_from_a_part_left_by_an_earlier_run(self):
        src, data = self.write_src(size=10 * 1024)
        self.server.put("nas", "share", f"d.bin.part.{len(data)}-{MTIME}", data[:6000])
        seen = []
        copy_file(src, self.uri("d.bin"), options=self.opts(), progress=lambda s, c, t: seen.append(c))
        self.assertEqual(seen[0], 6000)
        self.assertEqual(self.server.get("nas", "share", "d.bin"), data)

    def test_abort_can_keep_the_partial_file_for_a_later_resume(self):
        src, data = self.write_src(size=10 * 1024)
        ev = threading.Event()
        res = copy_file(src, self.uri("d.bin"), abort=ev, options=self.opts(keep_partial_on_abort=True),
                        progress=lambda s, c, t: ev.set() if c >= 3072 else None)
        self.assertFalse(res)
        names = self.server.names("nas", "share")
        self.assertEqual([n for n in names if ".part." in n].__len__(), 1)
        self.assertNotIn("d.bin.lock", names)
        seen = []
        copy_file(src, self.uri("d.bin"), options=self.opts(), progress=lambda s, c, t: seen.append(c))
        self.assertGreaterEqual(seen[0], 3072)
        self.assertEqual(self.server.get("nas", "share", "d.bin"), data)

    def test_abort_cleans_up_by_default(self):
        src, _ = self.write_src(size=10 * 1024)
        ev = threading.Event()
        copy_file(src, self.uri("d.bin"), abort=ev, options=self.opts(),
                  progress=lambda s, c, t: ev.set() if c >= 3072 else None)
        self.assertEqual(self.server.names("nas", "share"), [])
        self.assertEqual(self.server.handles, [])

    def test_stale_part_of_another_version_is_removed(self):
        src, _ = self.write_src(size=2048)
        self.server.put("nas", "share", "d.bin.part.1-2", b"junk")
        self.server.put("nas", "share", "d.bin.part2", b"unrelated")
        copy_file(src, self.uri("d.bin"), options=self.opts())
        self.assertEqual(self.server.names("nas", "share"), ["d.bin", "d.bin.part2"])


class LockingOverSMBTests(SMBCase):
    def holder(self, rel):
        """Hold the destination lock in another thread (as another process would)."""
        locked, release = threading.Event(), threading.Event()

        def run():
            with self.backend.lock(self.loc(rel)):
                locked.set()
                release.wait(10)
        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(locked.wait(5))
        return release, t

    def test_waits_for_the_other_writer_then_proceeds(self):
        src, data = self.write_src()
        release, t = self.holder("d.bin")
        threading.Timer(0.3, release.set).start()
        started = time.time()
        self.assertTrue(copy_file(src, self.uri("d.bin"), options=self.opts(lock_wait_timeout=10)))
        self.assertGreaterEqual(time.time() - started, 0.25)
        t.join(5)
        self.assertEqual(self.server.get("nas", "share", "d.bin"), data)

    def test_gives_up_after_the_lock_timeout(self):
        src, _ = self.write_src()
        release, t = self.holder("d.bin")
        try:
            with self.assertRaises(CopyError) as cm:
                copy_file(src, self.uri("d.bin"), options=self.opts(lock_wait_timeout=0.3))
            self.assertIn("locked", str(cm.exception))
        finally:
            release.set()
            t.join(5)
        self.assertIsNone(self.server.get("nas", "share", "d.bin"))


class AuthOverSMBTests(SMBCase):
    def test_wrong_password_fails_fast_without_retries(self):
        b = self.make_backend(StaticCredentials("alice", "WRONG"))
        self.addCleanup(b.close)
        from carefulcopy import register_backend
        register_backend("smb", lambda loc: b)
        src, _ = self.write_src()
        with self.assertRaises(AuthenticationError):
            copy_file(src, self.uri("d.bin"), options=self.opts(retries=5))
        self.assertEqual(len(self.server.register_calls), 1)


class ConcurrencyOverSMBTests(SMBCase):
    def run_with_deadline(self, fn, seconds=5):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        t.join(seconds)
        return not t.is_alive()

    def test_one_copy_needs_only_one_connection(self):
        """Lock file, .part file and metadata calls of one attempt share a single connection;
        with a pool of one, a copy that grabbed a second connection would wait on itself forever."""
        b = self.make_backend(pool_size=1)
        self.addCleanup(b.close)
        from carefulcopy import register_backend
        register_backend("smb", lambda loc: b)
        src, data = self.write_src(size=4096)
        self.assertTrue(self.run_with_deadline(lambda: copy_file(src, self.uri("d.bin"), options=self.opts())),
                        "copy deadlocked: it needs more than one pooled connection")
        self.assertEqual(self.server.get("nas", "share", "d.bin"), data)

    def test_smb_to_smb_also_needs_only_one_connection(self):
        b = self.make_backend(pool_size=1)
        self.addCleanup(b.close)
        from carefulcopy import register_backend
        register_backend("smb", lambda loc: b)
        self.server.put("nas", "share", "a.bin", b"x" * 3000)
        self.assertTrue(self.run_with_deadline(lambda: copy_file(self.uri("a.bin"), self.uri("b.bin"), options=self.opts())),
                        "server-side copy deadlocked")
        self.assertEqual(self.server.get("nas", "share", "b.bin"), b"x" * 3000)

    def test_more_parallel_copies_than_connections_do_not_deadlock(self):
        self.server.latency = 0.002          # make the copies genuinely overlap
        b = self.make_backend(pool_size=2)
        self.addCleanup(b.close)
        from carefulcopy import register_backend
        register_backend("smb", lambda loc: b)
        files = [self.write_src(name=f"s{i}.bin", size=20 * 1024) for i in range(6)]
        errors = []

        def run(i):
            try:
                copy_file(files[i][0], self.uri(f"out/d{i}.bin"), options=self.opts())
            except Exception as exc:       # pragma: no cover
                errors.append(exc)
        ts = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(6)]
        [t.start() for t in ts]
        deadline = time.time() + 15                 # one shared deadline, not 15 s per thread
        [t.join(max(0.0, deadline - time.time())) for t in ts]
        self.assertFalse(any(t.is_alive() for t in ts), "copies deadlocked")
        self.assertEqual(errors, [])
        for i, (_, data) in enumerate(files):
            self.assertEqual(self.server.get("nas", "share", f"out/d{i}.bin"), data)
        self.assertLessEqual(b._pool.stats()["total"], 2)
        self.assertEqual(self.server.handles, [])

    def test_simultaneous_failures_produce_one_question(self):
        files = [self.write_src(name=f"s{i}.bin", size=8 * 1024) for i in range(3)]
        self.server.write_failures = [500] * 3
        asked, errors = [], []
        prompt = BatchedFailurePrompt(lambda srcs: asked.append(sorted(srcs)) or False, window=0.5)

        def run(i):
            try:
                copy_file(files[i][0], self.uri(f"d{i}.bin"), on_failure=prompt, options=self.opts(retries=1))
            except CopyError as exc:
                errors.append(exc)
        ts = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(3)]
        [t.start() for t in ts]
        deadline = time.time() + 15
        [t.join(max(0.0, deadline - time.time())) for t in ts]
        self.assertEqual(len(asked), 1)
        self.assertEqual(len(asked[0]), 3)
        self.assertEqual(len(errors), 3)


if __name__ == "__main__":
    unittest.main()
