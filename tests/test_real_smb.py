"""Smoke tests against a REAL SMB server. Skipped unless configured:

    pip install 'carefulcopy[smb]'
    export CAREFULCOPY_TEST_SMB_URI='smb://nas.local/share/carefulcopy-tests'   # a scratch folder!
    export CAREFULCOPY_TEST_SMB_USER=alice
    export CAREFULCOPY_TEST_SMB_PASSWORD=secret
    cd tests && PYTHONPATH=../src python -m unittest test_real_smb -v

Each test checks an assumption the in-memory fake (tests/fake_smb.py) merely encodes:
share-mode locking, truncating a resumed file, atomic replace, mtime round-trip.
"""
import os
import tempfile
import unittest
import uuid

from carefulcopy import (CopyOptions, LockedError, Location, SMBBackend, StaticCredentials, TreeOptions, copy_file,
                         copy_tree, register_backend, reset_backends)

import helpers  # noqa: F401

URI = os.environ.get("CAREFULCOPY_TEST_SMB_URI")
USER = os.environ.get("CAREFULCOPY_TEST_SMB_USER")
PASSWORD = os.environ.get("CAREFULCOPY_TEST_SMB_PASSWORD")

try:
    import smbclient  # noqa: F401
    HAVE_SMB = True
except ImportError:
    HAVE_SMB = False


@unittest.skipUnless(URI and HAVE_SMB, "set CAREFULCOPY_TEST_SMB_URI (+ _USER/_PASSWORD) and install smbprotocol")
class RealServerTests(unittest.TestCase):
    def setUp(self):
        self.backend = SMBBackend(StaticCredentials(USER, PASSWORD) if USER else None, pool_size=4)
        register_backend("smb", lambda loc: self.backend)
        self.addCleanup(reset_backends)
        self.addCleanup(self.backend.close)
        self.base = Location.parse(URI) / f"run-{uuid.uuid4().hex[:8]}"
        self.backend.makedirs(self.base)
        self.addCleanup(self.cleanup_remote)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def cleanup_remote(self):
        # Removes the files; the (empty) run-xxxx folder is left for you to delete.
        try:
            for name in self.backend.listdir(self.base):
                try:
                    self.backend.remove(self.base / name)
                except Exception:
                    pass
        except Exception:
            pass

    def local(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(data)
        os.utime(p, (1_700_000_000, 1_700_000_000))
        return p

    def test_roundtrip_keeps_content_and_mtime(self):
        data = os.urandom(3 * 1024 * 1024 + 123)
        src = self.local("a.bin", data)
        remote = str(self.base / "a.bin")
        self.assertTrue(copy_file(src, remote, options=CopyOptions(block_size=1024 * 1024)))
        back = os.path.join(self.tmp.name, "back.bin")
        self.assertTrue(copy_file(remote, back))
        with open(back, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(self.backend.listdir(self.base), ["a.bin"])        # no .part / .lock left
        print("\n  server mtime:", int(self.backend.stat(self.base / "a.bin").mtime), "(expected 1700000000)")

    def test_tree_roundtrip_scandir_sizes_and_update_mode(self):
        """Planning relies on smbclient.scandir() entries carrying real sizes/times, and 'update'
        mode on the server keeping the mtime we set (within the 2 s tolerance)."""
        files = {"a.bin": 1500, "sub/b.bin": 70_000, "sub/deep/c.bin": 3}
        root = os.path.join(self.tmp.name, "tree")
        for rel, size in files.items():
            p = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(os.urandom(size))
            os.utime(p, (1_700_000_000, 1_700_000_000))
        os.makedirs(os.path.join(root, "empty"))
        remote = str(self.base / "tree")
        opts = CopyOptions(block_size=32 * 1024)
        snaps = []
        rep = copy_tree(root, remote, options=opts, tree=TreeOptions(workers=3), progress=snaps.append)
        self.assertTrue(rep, rep.failures)
        self.assertEqual(snaps[-1].bytes_copied, sum(files.values()))

        listing = {e.name: e for e in self.backend.scandir(Location.parse(remote) / "sub")}
        self.assertEqual(listing["b.bin"].size, 70_000, "scandir did not return the real file size")
        self.assertTrue(listing["deep"].is_dir)

        again = copy_tree(root, remote, options=opts, tree=TreeOptions(if_exists="update"))
        print("\n  second run: copied", again.files_copied, "skipped", again.files_skipped,
              "(expected 0 copied / 3 skipped; otherwise the server changes mtimes by more than 2 s)")
        self.assertEqual(again.files_copied, 0)

        back = os.path.join(self.tmp.name, "back")
        self.assertTrue(copy_tree(remote, back, options=opts, tree=TreeOptions(workers=3)))
        for rel, size in files.items():
            self.assertEqual(os.path.getsize(os.path.join(back, *rel.split("/"))), size)
        self.assertTrue(os.path.isdir(os.path.join(back, "empty")))

    def test_lock_excludes_a_second_writer(self):
        target = self.base / "locked.bin"
        with self.backend.lock(target):
            with self.assertRaises(LockedError):
                with self.backend.lock(target):
                    pass
        with self.backend.lock(target):      # free again
            pass

    def test_resume_truncates_a_longer_part_file(self):
        part = self.base / "p.bin"
        with self.backend.open_write(part) as f:
            f.write(b"0123456789")
        with self.backend.open_write(part, offset=4) as f:    # needs truncate() on an 'r+b' handle
            f.write(b"XY")
        with self.backend.open_read(part) as f:
            self.assertEqual(f.read(), b"0123XY")

    def test_a_held_lock_can_be_read_and_renamed_from_another_connection(self):
        """The assumptions behind taking over our own stale lock after a dropped connection."""
        other = SMBBackend(StaticCredentials(USER, PASSWORD) if USER else None, pool_size=2)
        self.addCleanup(other.close)
        target = self.base / "stale.bin"
        lock_loc = target.with_name("stale.bin.lock")
        with self.backend.lock(target, owner="holder-1"):
            token = other._read_lock_token(lock_loc)                 # needs share mode 'rwd' to be admitted
            self.assertIsNotNone(token, "could not read the token of a held lock")
            self.assertEqual(token["owner"], "holder-1")
            with self.assertRaises(LockedError):                      # still really locked
                with other.lock(target, owner="competitor"):
                    pass
            self.assertTrue(other._take_over(lock_loc),               # needs delete sharing ('d') to be allowed
                            "server refused to rename a lock file held open with share mode 'rd'")
            with other.lock(target, owner="competitor"):              # a fresh lock can now be taken
                pass

    def test_rename_replaces_an_existing_file(self):
        for name, data in (("new", b"NEW"), ("old", b"OLD")):
            with self.backend.open_write(self.base / name) as f:
                f.write(data)
        self.backend.rename(self.base / "new", self.base / "old")
        with self.backend.open_read(self.base / "old") as f:
            self.assertEqual(f.read(), b"NEW")

    def test_missing_file_maps_to_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            self.backend.stat(self.base / "does-not-exist")

    def test_wrong_password_is_an_authentication_error(self):
        from carefulcopy import AuthenticationError
        bad = SMBBackend(StaticCredentials(USER or "nobody", "definitely-wrong"))
        self.addCleanup(bad.close)
        with self.assertRaises(AuthenticationError):
            bad.stat(self.base)


if __name__ == "__main__":
    unittest.main()
