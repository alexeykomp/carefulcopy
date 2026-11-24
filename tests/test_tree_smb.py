import os
import threading
import unittest

from carefulcopy import (AuthenticationError, BatchedFailurePrompt, StaticCredentials, TreeOptions, copy_many,
                         copy_tree, register_backend)

from helpers_smb import SMBCase
from test_tree import MT, TreeBase


class SMBTreeCase(SMBCase, TreeBase):
    """Local fixtures (TreeBase) + the fake SMB server (SMBCase)."""

    def put_tree(self, base, spec):
        for rel, size in spec:
            if size is None:
                self.server.dirs.add(("nas", "share") + tuple(f"{base}/{rel}".strip("/").split("/")))
            else:
                self.server.put("nas", "share", f"{base}/{rel}", os.urandom(size), mtime=MT)

    def server_snapshot(self, base):
        out, prefix = {}, ("nas", "share") + tuple(base.split("/"))
        for key, node in self.server.files.items():
            if key[:len(prefix)] == prefix and len(key) > len(prefix):
                out["/".join(key[len(prefix):])] = bytes(node.data)
        for key in self.server.dirs:
            if key[:len(prefix)] == prefix and len(key) > len(prefix):
                out["/".join(key[len(prefix):]) + "/"] = None
        return out


class UploadDownloadTests(SMBTreeCase):
    def test_upload_download_and_server_side_copy_of_a_tree(self):
        src = self.make_tree()
        rep = copy_tree(src, self.uri("backup"), options=self.opts(), tree=self.topts(workers=3))
        self.assertTrue(rep)
        self.assertEqual(self.server_snapshot("backup"), self.snapshot(src))
        self.assertEqual(self.server.names("nas", "share", "backup/sub/deep"), ["c.bin", "d.txt"])   # no part / lock

        back = self.path("restored")
        rep = copy_tree(self.uri("backup"), back, options=self.opts(), tree=self.topts(workers=3))
        self.assertTrue(rep)
        self.assertEqual(self.snapshot(back), self.snapshot(src))
        self.assertEqual(int(os.stat(os.path.join(back, "sub", "b.bin")).st_mtime), MT)

        rep = copy_tree(self.uri("backup"), self.uri("mirror"), options=self.opts(), tree=self.topts(workers=2))
        self.assertTrue(rep)
        self.assertEqual(self.server_snapshot("mirror"), self.snapshot(src))
        self.assertEqual(self.server.live_handles, [])

    def test_planning_a_remote_tree_costs_one_listing_per_directory_and_no_per_file_stat(self):
        self.put_tree("t", [(f"d{d}/f{i}.bin", 100) for d in range(3) for i in range(10)])
        before_stat, before_scan = self.server.stat_calls, self.server.scandir_calls
        entries = self.backend.scandir(self.loc("t/d0"))
        self.assertEqual(len(entries), 10)
        self.assertEqual({e.size for e in entries}, {100})
        self.assertEqual(self.server.stat_calls - before_stat, 0)       # sizes came with the listing
        self.assertEqual(self.server.scandir_calls - before_scan, 1)

    def test_backends_without_scandir_fall_back_to_listdir_plus_stat(self):
        self.put_tree("t", [("a.bin", 10), ("b.bin", 20)])
        server = self.server

        class NoScandir:
            def __getattr__(self, name):
                if name == "scandir":
                    raise AttributeError(name)
                return getattr(server.client, name)
        b = self.make_backend()
        b._smb = NoScandir()
        self.addCleanup(b.close)
        self.assertEqual(sorted((e.name, e.size) for e in b.scandir(self.loc("t"))), [("a.bin", 10), ("b.bin", 20)])

    def test_copy_many_mixes_local_and_remote_sources(self):
        local, data = self.write_src("l.bin", size=3000)
        self.server.put("nas", "share", "r/remote.bin", b"R" * 4000, mtime=MT)
        rep = copy_many([local, self.uri("r/remote.bin")], self.path("out"), options=self.opts())
        self.assertEqual(rep.files_copied, 2)
        self.assertEqual(self.read(self.path("out", "remote.bin")), b"R" * 4000)
        rep = copy_many([self.path("out", "l.bin"), self.uri("r/remote.bin")], self.uri("dest"), options=self.opts())
        self.assertEqual(self.server.get("nas", "share", "dest/l.bin"), data)

    def test_sidecars_left_on_the_server_by_earlier_runs_are_not_downloaded(self):
        self.put_tree("t", [("movie.bin", 500), ("movie.bin.lock", 5), ("movie.bin.part.500-1-ab12cd34", 100)])
        rep = copy_tree(self.uri("t"), self.path("down"), options=self.opts())
        self.assertEqual(os.listdir(self.path("down")), ["movie.bin"])
        self.assertEqual(sorted(s.reason for s in rep.skipped), ["sidecar", "sidecar"])

    def test_update_mode_over_smb_skips_what_is_already_there(self):
        src = self.make_tree()
        copy_tree(src, self.uri("backup"), options=self.opts())
        rep = copy_tree(src, self.uri("backup"), options=self.opts(), tree=self.topts(if_exists="update"))
        self.assertEqual((rep.files_copied, rep.files_skipped), (0, 5))
        self.assertTrue(rep)
        with open(os.path.join(src, "a.bin"), "ab") as f:
            f.write(b"x")
        os.utime(os.path.join(src, "a.bin"), (MT, MT))
        rep = copy_tree(src, self.uri("backup"), options=self.opts(), tree=self.topts(if_exists="update"))
        self.assertEqual((rep.files_copied, rep.files_skipped), (1, 4))

    def test_following_symlinks_is_refused_for_remote_sources(self):
        self.put_tree("t", [("a.bin", 5)])
        with self.assertRaises(ValueError):
            copy_tree(self.uri("t"), self.path("d"), options=self.opts(), tree=self.topts(follow_symlinks=True))
        self.assertFalse(os.path.exists(self.path("d")))

    def test_destination_inside_source_is_refused_on_the_server_too(self):
        self.put_tree("t", [("a.bin", 5)])
        with self.assertRaises(ValueError):
            copy_tree(self.uri("t"), self.uri("t/inner"), options=self.opts())


class ResilienceTests(SMBTreeCase):
    def test_drops_in_the_middle_of_a_tree_are_resumed_per_file(self):
        src = self.make_tree("src", [(f"f{i}.bin", 12_000) for i in range(6)])
        self.server.write_failures = [3000, 5000, 3000]   # 3000+5000+3000 < 12000: all three really fire
        rep = copy_tree(src, self.uri("backup"), options=self.opts(retries=4), tree=self.topts(workers=1))
        self.assertTrue(rep)
        self.assertEqual(self.server.write_failures, [])
        self.assertEqual(self.server_snapshot("backup"), self.snapshot(src))
        modes = [m for p, m, s in self.server.open_log if ".part." in p]
        self.assertEqual(modes.count("r+b"), 3)                               # three resumes

    def test_wrong_password_aborts_the_whole_tree_at_once(self):
        b = self.make_backend(StaticCredentials("alice", "WRONG"))
        self.addCleanup(b.close)
        register_backend("smb", lambda loc: b)
        src = self.make_tree("src", [(f"f{i}.bin", 100) for i in range(8)])
        with self.assertRaises(AuthenticationError):
            copy_tree(src, self.uri("backup"), options=self.opts(), tree=self.topts(workers=3, errors="collect"))
        self.assertLessEqual(len(self.server.register_calls), 3)               # not one attempt per file

    def test_many_workers_on_a_small_pool_do_not_deadlock(self):
        b = self.make_backend(pool_size=2)
        self.addCleanup(b.close)
        register_backend("smb", lambda loc: b)
        self.server.latency = 0.001
        src = self.make_tree("src", [(f"d{i % 3}/f{i}.bin", 10_000) for i in range(18)])
        result = []
        t = threading.Thread(target=lambda: result.append(
            copy_tree(src, self.uri("backup"), options=self.opts(), tree=self.topts(workers=6))), daemon=True)
        t.start()
        t.join(20)
        self.assertFalse(t.is_alive(), "tree copy deadlocked")
        self.assertTrue(result[0])
        self.assertEqual(self.server_snapshot("backup"), self.snapshot(src))
        self.assertLessEqual(b.pool_stats()["total"], 2)

    def test_one_dialog_for_a_whole_tree_of_failures(self):
        src = self.make_tree("src", [(f"f{i}.bin", 6_000) for i in range(4)])
        self.server.write_failures = [500] * 4
        asked = []
        prompt = BatchedFailurePrompt(lambda files: asked.append(sorted(files)) or False, window=0.5)
        rep = copy_tree(src, self.uri("backup"), options=self.opts(retries=1), on_failure=prompt,
                        tree=self.topts(workers=4, errors="collect"))
        self.assertFalse(rep)
        self.assertEqual(len(asked), 1)                                        # one question, not four
        self.assertEqual(len(asked[0]), 4)
        self.assertEqual(len(rep.failures), 4)


if __name__ == "__main__":
    unittest.main()
