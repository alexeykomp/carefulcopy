import os
import threading
import time
import unittest
from pathlib import Path

from carefulcopy import (AuthenticationError, CopyOptions, CopyReport, FixedRateThrottle, Location,
                         TreeOptions, TreeProgress, copy_many, copy_tree)

from helpers import Base

MT = 1_700_000_000


class TreeBase(Base):
    SPEC = [("a.bin", 5000), ("sub/b.bin", 7000), ("sub/deep/c.bin", 3000), ("sub/deep/d.txt", 1), ("zero.bin", 0), ("empty/", None)]

    def make_tree(self, name="src", spec=None):
        root = self.path(name)
        os.makedirs(root, exist_ok=True)
        for rel, size in (spec or self.SPEC):
            p = os.path.join(root, rel)
            if size is None:
                os.makedirs(p, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(os.urandom(size))
            os.utime(p, (MT, MT))
        return root

    def snapshot(self, root):
        out = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for d in dirnames:
                out[os.path.relpath(os.path.join(dirpath, d), root).replace(os.sep, "/") + "/"] = None
            for f in filenames:
                p = os.path.join(dirpath, f)
                out[os.path.relpath(p, root).replace(os.sep, "/")] = self.read(p)
        return out

    def topts(self, **kw):
        kw.setdefault("progress_interval", 0)
        return TreeOptions(**kw)

    def cp(self, src, dst, **kw):
        kw.setdefault("options", self.opts())
        return copy_tree(src, dst, **kw)


class BasicTreeTests(TreeBase):
    def test_copies_structure_content_mtime_and_empty_dirs(self):
        src = self.make_tree()
        dst = self.path("dst")
        rep = self.cp(src, dst)
        self.assertIsInstance(rep, CopyReport)
        self.assertTrue(rep)
        self.assertEqual(self.snapshot(dst), self.snapshot(src))
        self.assertEqual((rep.files_copied, rep.bytes_copied), (5, 15001))
        self.assertEqual(int(os.stat(os.path.join(dst, "sub", "deep", "c.bin")).st_mtime), MT)
        self.assertFalse([n for _, _, fs in os.walk(dst) for n in fs if ".part." in n or n.endswith(".lock")])

    def test_destination_is_the_copy_itself_like_copytree(self):
        src = self.make_tree()
        self.cp(src, self.path("out", "renamed"))
        self.assertTrue(os.path.exists(self.path("out", "renamed", "a.bin")))

    def test_single_file_source(self):
        p, data = self.write_src("one.bin", size=3000)
        rep = self.cp(p, self.path("o", "copy.bin"))
        self.assertEqual(rep.files_copied, 1)
        self.assertEqual(self.read(self.path("o", "copy.bin")), data)

    def test_empty_source_directory(self):
        os.makedirs(self.path("e"))
        rep = self.cp(self.path("e"), self.path("e2"))
        self.assertTrue(rep)
        self.assertTrue(os.path.isdir(self.path("e2")))

    def test_missing_source_is_always_an_error(self):
        with self.assertRaises(FileNotFoundError):
            self.cp(self.path("nope"), self.path("d"), tree=self.topts(errors="collect"))

    def test_copying_a_directory_into_itself_is_refused(self):
        src = self.make_tree()
        for dst in (src, os.path.join(src, "sub"), os.path.join(src, "new")):
            with self.assertRaises(ValueError):
                self.cp(src, dst)
        self.assertFalse(os.path.exists(os.path.join(src, "new")))     # nothing was created

    def test_sibling_with_a_common_prefix_is_not_mistaken_for_inside(self):
        src = self.make_tree("data")
        rep = self.cp(src, self.path("data-backup"))
        self.assertTrue(rep)


class CopyManyTests(TreeBase):
    def test_files_and_directories_land_in_the_destination_dir(self):
        d = self.make_tree("album")
        f, data = self.write_src("song.bin", size=2000)
        rep = copy_many([d, f], self.path("out"), options=self.opts(), tree=self.topts())
        self.assertEqual(rep.files_copied, 6)
        self.assertEqual(self.read(self.path("out", "song.bin")), data)
        self.assertEqual(self.snapshot(self.path("out", "album")), self.snapshot(d))

    def test_duplicate_names_are_refused_before_anything_is_copied(self):
        a, _ = self.write_src("x.bin")
        os.makedirs(self.path("other"))
        b = self.path("other", "x.bin")
        with open(b, "wb") as fh:
            fh.write(b"y")
        with self.assertRaises(ValueError):
            copy_many([a, b], self.path("out"), options=self.opts())
        self.assertFalse(os.path.exists(self.path("out")))

    def test_nameless_source_is_refused(self):
        with self.assertRaises(ValueError):
            copy_many(["/"], self.path("out"))

    def test_aggregated_progress_covers_all_sources(self):
        d = self.make_tree("album")
        f, _ = self.write_src("song.bin", size=2000)
        snaps = []
        copy_many([d, f], self.path("out"), options=self.opts(), tree=self.topts(), progress=snaps.append)
        self.assertEqual((snaps[-1].files_total, snaps[-1].bytes_total), (6, 17001))
        self.assertEqual((snaps[-1].files_done, snaps[-1].bytes_copied), (6, 17001))


class ProgressTests(TreeBase):
    def test_phases_totals_and_final_snapshot(self):
        src = self.make_tree()
        snaps = []
        self.cp(src, self.path("dst"), progress=snaps.append, tree=self.topts())
        phases = [s.phase for s in snaps]
        self.assertEqual(phases[0], "scanning")
        self.assertEqual(phases[-1], "done")
        self.assertEqual(sorted(set(phases), key=phases.index), ["scanning", "copying", "done"])
        first_copy = next(s for s in snaps if s.phase == "copying")
        self.assertEqual((first_copy.files_total, first_copy.bytes_total), (5, 15001))
        self.assertEqual(first_copy.bytes_copied, 0)
        last = snaps[-1]
        self.assertEqual((last.files_done, last.files_total, last.bytes_copied, last.bytes_total), (5, 5, 15001, 15001))
        self.assertEqual(last.fraction, 1.0)
        self.assertEqual(last.current, ())

    def test_bytes_never_go_backwards_without_retries(self):
        src = self.make_tree()
        snaps = []
        self.cp(src, self.path("dst"), progress=snaps.append, tree=self.topts(workers=3))
        copied = [s.bytes_copied for s in snaps if s.phase != "scanning"]
        self.assertEqual(copied, sorted(copied))
        self.assertTrue(all(s.bytes_copied <= s.bytes_total for s in snaps if s.phase != "scanning"))

    def test_current_lists_files_being_copied(self):
        src = self.make_tree()
        snaps = []
        self.cp(src, self.path("dst"), progress=snaps.append, tree=self.topts())
        seen = {c for s in snaps for c in s.current}
        self.assertTrue(any(c.endswith("b.bin") for c in seen))

    def test_callback_is_rate_limited_but_first_and_last_always_arrive(self):
        src = self.make_tree()
        snaps = []
        self.cp(src, self.path("dst"), progress=snaps.append, tree=TreeOptions(progress_interval=60))
        self.assertLessEqual(len(snaps), 3)
        self.assertEqual(snaps[-1].phase, "done")
        self.assertEqual(snaps[-1].files_done, 5)

    def test_callbacks_never_overlap_even_with_many_workers(self):
        src = self.make_tree("big", [(f"f{i}.bin", 20_000) for i in range(12)])
        depth, worst = [0], [0]

        def cb(_snap):
            depth[0] += 1
            worst[0] = max(worst[0], depth[0])
            time.sleep(0.0005)
            depth[0] -= 1
        self.cp(src, self.path("dst"), progress=cb, tree=self.topts(workers=6))
        self.assertEqual(worst[0], 1)

    def test_a_raising_callback_stops_the_run_and_its_error_surfaces(self):
        src = self.make_tree("big", [(f"f{i}.bin", 20_000) for i in range(6)])
        calls = []

        def cb(snap):
            calls.append(snap)
            if snap.phase == "copying" and snap.files_done >= 1:
                raise RuntimeError("ui is gone")
        with self.assertRaises(RuntimeError):
            self.cp(src, self.path("dst"), progress=cb, tree=self.topts())
        self.assertFalse(os.path.exists(self.path("dst", "f5.bin")))   # it really stopped

    def test_totals_exclude_skipped_files(self):
        src = self.make_tree()
        self.cp(src, self.path("dst"))
        snaps = []
        self.cp(src, self.path("dst"), progress=snaps.append, tree=self.topts(if_exists="update"))
        self.assertEqual((snaps[-1].files_total, snaps[-1].bytes_total), (0, 0))
        self.assertEqual(snaps[-1].fraction, 1.0)


class WorkersTests(TreeBase):
    def test_parallel_copy_is_correct_and_really_parallel(self):
        src = self.make_tree("big", [(f"d{i % 4}/f{i}.bin", 8_000) for i in range(24)])
        self.flaky.write_delay = 0.003
        dst = Location("flaky", "", Path(self.path("dst")))
        rep = copy_tree(src, dst, options=self.opts(), tree=self.topts(workers=4))
        self.assertTrue(rep)
        self.assertEqual(self.snapshot(self.path("dst")), self.snapshot(src))
        self.assertGreaterEqual(self.flaky.max_active, 2)
        self.assertLessEqual(self.flaky.max_active, 4)

    def test_sequential_by_default(self):
        src = self.make_tree("big", [(f"f{i}.bin", 5_000) for i in range(8)])
        self.flaky.write_delay = 0.001
        copy_tree(src, Location("flaky", "", Path(self.path("dst"))), options=self.opts(), tree=self.topts())
        self.assertEqual(self.flaky.max_active, 1)

    def test_workers_share_one_throttle(self):
        src = self.make_tree("big", [(f"f{i}.bin", 3_000) for i in range(6)])
        entered = []

        class T:
            def worker(self, s, d):
                import contextlib
                entered.append((str(s), str(d)))
                return contextlib.nullcontext(type("W", (), {"acquire": lambda *a, **k: None, "report": lambda *a: None})())
        copy_tree(src, self.path("dst"), options=self.opts(), tree=self.topts(workers=3), throttle=T())
        self.assertEqual(len(entered), 6)

    def test_fixed_rate_throttle_limits_the_whole_tree(self):
        src = self.make_tree("big", [(f"f{i}.bin", 20_000) for i in range(4)])
        self.flaky.write_delay = 0.0001           # makes the destination count as "remote" for the throttle
        t = FixedRateThrottle(80_000, burst=1_000)
        started = time.time()
        copy_tree(src, Location("flaky", "", Path(self.path("dst"))), options=self.opts(),
                  tree=self.topts(workers=2), throttle=t)
        self.assertGreaterEqual(time.time() - started, 0.7)       # 80 kB at 80 kB/s, shared by both workers


class AbortTests(TreeBase):
    def test_abort_before_start(self):
        src = self.make_tree()
        rep = self.cp(src, self.path("dst"), abort=True)
        self.assertTrue(rep.aborted)
        self.assertFalse(rep)
        self.assertFalse(os.path.exists(self.path("dst")))

    def test_abort_midway_stops_cleanly_and_leaves_no_partial_files(self):
        src = self.make_tree("big", [(f"f{i}.bin", 30_000) for i in range(10)])
        ev = threading.Event()
        rep = self.cp(src, self.path("dst"), abort=ev, tree=self.topts(workers=1),
                       progress=lambda s: ev.set() if s.files_done >= 3 else None)
        self.assertTrue(rep.aborted)
        self.assertGreaterEqual(rep.files_copied, 3)
        self.assertLess(rep.files_copied, 10)
        left = os.listdir(self.path("dst"))
        self.assertEqual(len(left), rep.files_copied)              # only complete files, no .part/.lock
        for name in left:
            self.assertEqual(self.read(self.path("dst", name)), self.read(self.path("big", name)))

    def test_abort_stops_parallel_workers_quickly(self):
        src = self.make_tree("big", [(f"f{i}.bin", 40_000) for i in range(20)])
        self.flaky.write_delay = 0.002
        ev = threading.Event()
        threading.Timer(0.15, ev.set).start()
        started = time.time()
        rep = copy_tree(src, Location("flaky", "", Path(self.path("dst"))), abort=ev, options=self.opts(),
                        tree=self.topts(workers=4))
        self.assertTrue(rep.aborted)
        self.assertLess(time.time() - started, 5)
        self.assertEqual([n for n in os.listdir(self.path("dst")) if ".part." in n or n.endswith(".lock")], [])


class ErrorPolicyTests(TreeBase):
    def setUp(self):
        super().setUp()
        self.src = self.make_tree("src", [(f"f{i}.bin", 4000) for i in range(6)])
        self.flaky.fail_names["f2.bin"] = PermissionError("denied by test")
        self.dst = Location("flaky", "", Path(self.path("dst")))

    def test_raise_stops_at_the_first_failure_and_raises_the_original_error(self):
        with self.assertRaises(PermissionError):
            copy_tree(self.src, self.dst, options=self.opts(), tree=self.topts())
        self.assertTrue(os.path.exists(self.path("dst", "f1.bin")))
        self.assertFalse(os.path.exists(self.path("dst", "f3.bin")))      # never started

    def test_raise_with_workers_still_stops_and_raises(self):
        with self.assertRaises(PermissionError):
            copy_tree(self.src, self.dst, options=self.opts(), tree=self.topts(workers=3))
        self.assertEqual([n for n in os.listdir(self.path("dst")) if ".part." in n or n.endswith(".lock")], [])

    def test_collect_carries_on_and_reports_every_failure(self):
        rep = copy_tree(self.src, self.dst, options=self.opts(), tree=self.topts(errors="collect"))
        self.assertFalse(rep)
        self.assertFalse(rep.aborted)
        self.assertEqual(rep.files_copied, 5)
        self.assertEqual([f.source.endswith("f2.bin") for f in rep.failures], [True])
        self.assertIsInstance(rep.failures[0].error, PermissionError)
        self.assertFalse(os.path.exists(self.path("dst", "f2.bin")))
        self.assertTrue(os.path.exists(self.path("dst", "f5.bin")))

    def test_authentication_failures_are_fatal_even_when_collecting(self):
        self.flaky.fail_names["f1.bin"] = AuthenticationError("bad login")
        with self.assertRaises(AuthenticationError):
            copy_tree(self.src, self.dst, options=self.opts(), tree=self.topts(errors="collect"))
        self.assertFalse(os.path.exists(self.path("dst", "f4.bin")))

    def test_unreadable_source_directory_follows_the_policy(self):
        os.makedirs(self.path("src", "locked"))
        from carefulcopy.backends.local import LocalBackend
        real = LocalBackend.scandir

        def scandir(self_, loc):
            if loc.name == "locked":
                raise PermissionError("no listing for you")
            return real(self_, loc)
        LocalBackend.scandir = scandir
        self.addCleanup(setattr, LocalBackend, "scandir", real)
        self.flaky.fail_names.clear()
        with self.assertRaises(PermissionError):
            self.cp(self.src, self.path("d1"))
        rep = self.cp(self.src, self.path("d2"), tree=self.topts(errors="collect"))
        self.assertEqual(rep.files_copied, 6)
        self.assertEqual(len(rep.failures), 1)

    def test_on_failure_hook_reaches_every_file(self):
        self.flaky.fail_names.clear()
        self.flaky.failures = [500, 500, 500]            # 3 first attempts drop the connection
        asked = []
        rep = copy_tree(self.src, self.dst, options=self.opts(retries=1), tree=self.topts(errors="collect"),
                        on_failure=lambda s, e: asked.append(s) or True)
        self.assertTrue(rep)
        self.assertEqual(len(asked), 3)


class ExistingFilesTests(TreeBase):
    def test_skip_leaves_existing_files_alone(self):
        src = self.make_tree()
        dst = self.path("dst")
        os.makedirs(dst)
        with open(os.path.join(dst, "a.bin"), "wb") as f:
            f.write(b"mine")
        rep = self.cp(src, dst, tree=self.topts(if_exists="skip"))
        self.assertEqual(self.read(os.path.join(dst, "a.bin")), b"mine")
        self.assertEqual(rep.files_copied, 4)
        self.assertEqual([(os.path.basename(s.source), s.reason) for s in rep.skipped], [("a.bin", "exists")])

    def test_error_mode_raises_file_exists(self):
        src = self.make_tree()
        self.cp(src, self.path("dst"))
        with self.assertRaises(FileExistsError):
            self.cp(src, self.path("dst"), tree=self.topts(if_exists="error"))

    def test_overwrite_is_the_default(self):
        src = self.make_tree()
        dst = self.path("dst")
        self.cp(src, dst)
        with open(os.path.join(dst, "a.bin"), "wb") as f:
            f.write(b"vandalised")
        rep = self.cp(src, dst)
        self.assertEqual(rep.files_copied, 5)
        self.assertEqual(self.snapshot(dst), self.snapshot(src))

    def test_update_skips_unchanged_and_recopies_changed(self):
        src = self.make_tree()
        dst = self.path("dst")
        self.cp(src, dst)
        with open(os.path.join(src, "a.bin"), "ab") as f:                  # changed size
            f.write(b"more")
        os.utime(os.path.join(src, "a.bin"), (MT, MT))
        os.utime(os.path.join(src, "sub", "b.bin"), (MT + 3600, MT + 3600))  # same size, an hour newer
        os.utime(os.path.join(src, "sub", "deep", "c.bin"), (MT + 1, MT + 1))  # a second apart: inside the tolerance
        rep = self.cp(src, dst, tree=self.topts(if_exists="update"))
        self.assertEqual(rep.files_copied, 2)
        self.assertEqual(sorted(s.reason for s in rep.skipped), ["unchanged"] * 3)
        self.assertEqual(self.read(os.path.join(dst, "a.bin")), self.read(os.path.join(src, "a.bin")))
        mtime = lambda *p: int(os.stat(os.path.join(dst, *p)).st_mtime)
        self.assertEqual(mtime("sub", "b.bin"), MT + 3600)                  # re-copied (mtime carried over)
        self.assertEqual(mtime("sub", "deep", "c.bin"), MT)                 # left alone

    def test_update_tolerance_is_configurable(self):
        src = self.make_tree()
        dst = self.path("dst")
        self.cp(src, dst)
        os.utime(os.path.join(src, "a.bin"), (MT + 1, MT + 1))
        self.assertEqual(self.cp(src, dst, tree=self.topts(if_exists="update", mtime_tolerance=0.5)).files_copied, 1)
        self.assertEqual(self.cp(src, dst, tree=self.topts(if_exists="update", mtime_tolerance=0.5)).files_copied, 0)

    def test_rerun_after_an_interruption_copies_only_what_is_missing(self):
        src = self.make_tree("big", [(f"f{i}.bin", 30_000) for i in range(10)])
        dst = self.path("dst")
        ev = threading.Event()
        first = self.cp(src, dst, abort=ev, tree=self.topts(),
                        progress=lambda s: ev.set() if s.files_done >= 4 else None)
        self.assertTrue(first.aborted)
        done_before = first.files_copied
        second = self.cp(src, dst, tree=self.topts(if_exists="update"))
        self.assertTrue(second)
        self.assertEqual(second.files_copied, 10 - done_before)
        self.assertEqual(second.files_skipped, done_before)
        self.assertEqual(self.snapshot(dst), self.snapshot(src))

    def test_interrupted_file_is_resumed_not_restarted_on_rerun(self):
        src = self.make_tree("big", [("only.bin", 40_000)])
        dst = self.path("dst")
        ev = threading.Event()
        copy_tree(src, dst, abort=ev, options=self.opts(keep_partial_on_abort=True),
                  tree=self.topts(), progress=lambda s: ev.set() if s.bytes_copied >= 20_000 else None)
        seen = []
        copy_tree(src, dst, options=self.opts(), tree=self.topts(if_exists="update"),
                  progress=lambda s: seen.append(s.bytes_copied))
        self.assertTrue(any(b >= 20_000 for b in seen[:3]))          # it began where the partial file ended
        self.assertEqual(self.snapshot(dst), self.snapshot(src))


class FilterTests(TreeBase):
    def test_exclude_patterns_by_name_and_by_relative_path(self):
        src = self.make_tree("src", [("keep.bin", 10), ("skip.tmp", 10), ("sub/skip.tmp", 10), ("sub/keep.bin", 10),
                                     ("cache/x.bin", 10), ("sub/cache/y.bin", 10)])
        rep = self.cp(src, self.path("dst"), tree=self.topts(exclude=["*.tmp", "cache/*"]))
        snap = self.snapshot(self.path("dst"))
        self.assertEqual(sorted(k for k, v in snap.items() if v is not None), ["keep.bin", "sub/cache/y.bin", "sub/keep.bin"])
        self.assertEqual(sorted(s.reason for s in rep.skipped), ["excluded"] * 3)

    def test_excluded_directory_is_not_entered(self):
        src = self.make_tree("src", [("keep.bin", 10), ("node_modules/a/b.bin", 10)])
        rep = self.cp(src, self.path("dst"), tree=self.topts(exclude=["node_modules"]))
        self.assertFalse(os.path.exists(self.path("dst", "node_modules")))
        self.assertEqual(rep.files_copied, 1)

    def test_exclude_must_be_a_list_not_a_string(self):
        with self.assertRaises(TypeError):
            TreeOptions(exclude="*.tmp")

    def test_carefulcopy_leftovers_are_not_copied_but_lookalikes_are(self):
        src = self.make_tree("src", [
            ("movie.mp4", 10),
            ("movie.mp4.part.100-1700000000000000000-ab12cd34", 10),     # a leftover partial file
            ("movie.mp4.lock", 5),                                       # our lock sidecar (movie.mp4 is next to it)
            ("movie.mp4.lock.dead-0f0f0f0f", 5),
            ("package.lock", 5),                                         # a user's own lock file: no sibling "package"
            ("notes.part.txt", 5),                                       # merely looks similar
        ])
        rep = self.cp(src, self.path("dst"))
        names = sorted(k for k, v in self.snapshot(self.path("dst")).items() if v is not None)
        self.assertEqual(names, ["movie.mp4", "notes.part.txt", "package.lock"])
        self.assertEqual(sorted(s.reason for s in rep.skipped), ["sidecar"] * 3)

    def test_sidecar_filter_can_be_disabled(self):
        src = self.make_tree("src", [("a.bin", 5), ("a.bin.lock", 5)])
        rep = self.cp(src, self.path("dst"), tree=self.topts(skip_sidecars=False))
        self.assertEqual(rep.files_copied, 2)


class DepthTests(TreeBase):
    def test_directories_below_max_depth_are_skipped_with_a_reason(self):
        src = self.make_tree("src", [("a/b/c/d/e/deep.bin", 10), ("a/b/shallow.bin", 10), ("top.bin", 10)])
        rep = self.cp(src, self.path("dst"), tree=self.topts(max_depth=3))
        names = sorted(k for k, v in self.snapshot(self.path("dst")).items() if v is not None)
        self.assertEqual(names, ["a/b/shallow.bin", "top.bin"])
        self.assertEqual([(os.path.basename(s.source), s.reason) for s in rep.skipped], [("d", "too deep")])

    def test_default_depth_is_generous_and_validated(self):
        self.assertEqual(TreeOptions().max_depth, 100)
        with self.assertRaises(ValueError):
            TreeOptions(max_depth=0)


class SymlinkTests(TreeBase):
    def cp_guarded(self, src, dst, **kw):
        """Run with a deadline: a traversal that does not terminate must fail, not hang the suite."""
        ev, out = threading.Event(), []
        t = threading.Thread(target=lambda: out.append(self.cp(src, dst, abort=ev, **kw)), daemon=True)
        t.start()
        t.join(10)
        if t.is_alive():
            ev.set()
            t.join(5)
            self.fail("the symlink traversal did not terminate")
        return out[0]

    def setUp(self):
        super().setUp()
        self.src = self.make_tree("src", [("real/file.bin", 100), ("top.bin", 50)])
        try:
            os.symlink(self.path("src", "real"), self.path("src", "link-to-dir"))
            os.symlink(self.path("src", "top.bin"), self.path("src", "link-to-file"))
            os.symlink(self.path("src", "nowhere"), self.path("src", "broken"))
            os.symlink(self.path("src"), self.path("src", "real", "loop"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")

    def test_symlinks_are_skipped_by_default_and_reported(self):
        rep = self.cp_guarded(self.src, self.path("dst"))
        names = sorted(k for k in self.snapshot(self.path("dst")))
        self.assertEqual(names, ["real/", "real/file.bin", "top.bin"])
        self.assertEqual(sorted(os.path.basename(s.source) for s in rep.skipped if s.reason == "symlink"),
                         ["broken", "link-to-dir", "link-to-file", "loop"])

    def test_following_symlinks_copies_content_and_survives_loops(self):
        self.cp_guarded(self.src, self.path("dst"), tree=self.topts(follow_symlinks=True, errors="collect"))
        snap = self.snapshot(self.path("dst"))
        self.assertEqual(snap["link-to-file"], snap["top.bin"])
        self.assertEqual(snap["link-to-dir/file.bin"], snap["real/file.bin"])
        self.assertNotIn("real/loop/real/", snap)                       # the loop was cut, not followed forever

    def test_a_broken_link_is_never_followed(self):
        rep = self.cp_guarded(self.src, self.path("dst"), tree=self.topts(follow_symlinks=True, errors="collect"))
        self.assertNotIn("broken", self.snapshot(self.path("dst")))
        self.assertIn("broken", [os.path.basename(s.source) for s in rep.skipped])


if __name__ == "__main__":
    unittest.main()
