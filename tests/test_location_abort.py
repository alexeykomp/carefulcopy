import threading
import unittest
from pathlib import Path, PurePosixPath

from carefulcopy import Location, is_aborted
from carefulcopy.abort import sleep_abortable


class LocationTests(unittest.TestCase):
    def test_plain_path_is_local(self):
        loc = Location.parse("/tmp/a b.txt")
        self.assertTrue(loc.is_local)
        self.assertEqual(loc.path, Path("/tmp/a b.txt"))

    def test_pathlib_is_local(self):
        self.assertTrue(Location.parse(Path("/x/y")).is_local)

    def test_windows_drive_is_not_a_uri(self):
        self.assertTrue(Location.parse("C://data/x").is_local)
        self.assertTrue(Location.parse("C:\\data\\x").is_local)

    def test_smb_uri(self):
        loc = Location.parse("smb://nas.local/Foto/2024/a b%41.jpg")
        self.assertEqual((loc.scheme, loc.netloc), ("smb", "nas.local"))
        self.assertEqual(loc.path, PurePosixPath("/Foto/2024/a b%41.jpg"))  # verbatim, no decoding
        self.assertEqual(str(loc), "smb://nas.local/Foto/2024/a b%41.jpg")

    def test_file_uri(self):
        self.assertEqual(Location.parse("file:///tmp/x").path, Path("/tmp/x"))
        with self.assertRaises(ValueError):
            Location.parse("file://otherhost/tmp/x")

    def test_navigation(self):
        loc = Location.parse("smb://h/s/d/f.txt")
        self.assertEqual(str(loc.parent), "smb://h/s/d")
        self.assertEqual(str(loc.with_name("g.txt")), "smb://h/s/d/g.txt")
        self.assertEqual(str(loc.parent / "x"), "smb://h/s/d/x")

    def test_idempotent(self):
        loc = Location.parse("smb://h/s")
        self.assertIs(Location.parse(loc), loc)


class AbortTests(unittest.TestCase):
    def test_forms(self):
        ev = threading.Event()
        self.assertFalse(is_aborted(None))
        self.assertFalse(is_aborted(ev))
        self.assertFalse(is_aborted(lambda: False))
        ev.set()
        self.assertTrue(is_aborted(ev))
        self.assertTrue(is_aborted(True))
        self.assertTrue(is_aborted(lambda: 1))

        class V:
            value = 1
        self.assertTrue(is_aborted(V()))

    def test_sleep_wakes_on_abort(self):
        ev = threading.Event()
        ev.set()
        self.assertTrue(sleep_abortable(5, ev))
        self.assertFalse(sleep_abortable(0.01, None))


if __name__ == "__main__":
    unittest.main()
