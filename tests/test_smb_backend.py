import errno
import threading
import time
import unicodedata
import unittest
from unittest import mock

from carefulcopy import (AuthenticationError, Credentials, LockedError, Location, StaticCredentials,
                         UnsupportedSchemeError, configure_smb, get_backend, reset_backends)
from carefulcopy.backends import smb as smb_module
from carefulcopy.pool import PoolClosedError

import fake_smb
from fake_smb import SMBConnectionClosed, SMBOSError, ST
from helpers_smb import SMBCase


class PathTests(SMBCase):
    def test_unc(self):
        self.assertEqual(self.backend.unc(self.loc("dir/a b.txt")), r"\\nas\share\dir\a b.txt")
        self.assertEqual(self.backend.unc(Location.parse("smb://nas:4455/share")), r"\\nas\share")

    def test_location_without_share_or_host_is_rejected(self):
        with self.assertRaises(ValueError):
            self.backend.unc(Location.parse("smb://nas/"))
        with self.assertRaises(ValueError):
            self.backend.unc(Location.parse("smb:///share/x"))

    def test_backslash_in_name_is_rejected_unless_sanitizing(self):
        loc = Location("smb", "nas", __import__("pathlib").PurePosixPath("/share/a\\b"))
        with self.assertRaises(ValueError):
            self.backend.unc(loc)
        b = self.make_backend(sanitize_names=True)
        self.addCleanup(b.close)
        self.assertEqual(b.unc(loc), r"\\nas\share\a_b")

    def test_sanitize_leaves_share_name_alone(self):
        b = self.make_backend(sanitize_names=True)
        self.addCleanup(b.close)
        self.assertEqual(b.unc(self.loc("we?ird:name.txt")), r"\\nas\share\we_ird_name.txt")

    def test_unicode_is_normalised_to_nfc_by_default(self):
        nfd = unicodedata.normalize("NFD", "Müller.txt")
        self.assertNotEqual(nfd, "Müller.txt")
        self.assertEqual(self.backend.unc(self.loc(nfd)), "\\\\nas\\share\\Müller.txt")
        raw = self.make_backend(normalize_unicode=None)
        self.addCleanup(raw.close)
        self.assertEqual(raw.unc(self.loc(nfd)), "\\\\nas\\share\\" + nfd)


class OperationTests(SMBCase):
    def test_stat_and_missing(self):
        self.server.put("nas", "share", "a/f.bin", b"hello", mtime=1_700_000_123)
        info = self.backend.stat(self.loc("a/f.bin"))
        self.assertEqual((info.size, int(info.mtime), info.is_dir), (5, 1_700_000_123, False))
        self.assertTrue(self.backend.stat(self.loc("a")).is_dir)
        with self.assertRaises(FileNotFoundError):
            self.backend.stat(self.loc("nope"))
        self.assertTrue(self.backend.exists(self.loc("a/f.bin")))
        self.assertFalse(self.backend.exists(self.loc("a/zzz")))

    def test_listdir_makedirs_remove_utime(self):
        self.backend.makedirs(self.loc("x/y/z"))
        self.backend.makedirs(self.loc("x/y/z"), exist_ok=True)
        self.server.put("nas", "share", "x/y/z/f", b"1")
        self.assertEqual(self.backend.listdir(self.loc("x/y/z")), ["f"])
        self.backend.set_mtime(self.loc("x/y/z/f"), 1_600_000_000)
        self.assertEqual(int(self.backend.stat(self.loc("x/y/z/f")).mtime), 1_600_000_000)
        self.backend.remove(self.loc("x/y/z/f"))
        self.assertEqual(self.backend.listdir(self.loc("x/y/z")), [])

    def test_makedirs_on_share_root_is_a_noop(self):
        self.backend.makedirs(self.loc(""))

    def test_rename_replaces_existing_destination(self):
        self.server.put("nas", "share", "a", b"new")
        self.server.put("nas", "share", "b", b"old")
        self.backend.rename(self.loc("a"), self.loc("b"))
        self.assertEqual(self.server.get("nas", "share", "b"), b"new")
        self.assertIsNone(self.server.get("nas", "share", "a"))

    def test_rename_falls_back_when_smbclient_has_no_replace(self):
        server = self.server

        class NoReplace:
            def __getattr__(self, name):
                if name == "replace":
                    raise AttributeError(name)
                return getattr(server.client, name)
        b = self.make_backend()
        b._smb = NoReplace()
        self.addCleanup(b.close)
        self.server.put("nas", "share", "a", b"new")
        self.server.put("nas", "share", "b", b"old")
        b.rename(self.loc("a"), self.loc("b"))
        self.assertEqual(self.server.get("nas", "share", "b"), b"new")

    def test_rename_across_servers_is_refused(self):
        with self.assertRaises(ValueError):
            self.backend.rename(self.loc("a"), Location.parse("smb://other/share/a"))

    def test_open_write_resume_truncates_a_torn_tail(self):
        self.server.put("nas", "share", "p", b"0123456789")
        with self.backend.open_write(self.loc("p"), offset=4) as f:
            f.write(b"XY")
        self.assertEqual(self.server.get("nas", "share", "p"), b"0123XY")

    def test_open_write_resume_with_short_file_is_transient(self):
        from carefulcopy import TransientError
        self.server.put("nas", "share", "p", b"01")
        with self.assertRaises(TransientError):
            self.backend.open_write(self.loc("p"), offset=4)
        self.assertEqual(self.server.live_handles, [])   # nothing left open


class SessionTests(SMBCase):
    def test_one_registration_per_session(self):
        self.server.put("nas", "share", "f", b"x")
        with self.backend.session():
            for _ in range(3):
                self.backend.stat(self.loc("f"))
        self.assertEqual(len(self.server.register_calls), 1)
        self.assertEqual(self.backend._pool.stats()["total"], 1)

    def test_ops_outside_a_session_still_share_the_pooled_connection(self):
        self.server.put("nas", "share", "f", b"x")
        self.backend.stat(self.loc("f"))
        self.backend.stat(self.loc("f"))
        self.assertEqual(self.backend._pool.stats(), {"total": 1, "idle": 1, "size": 4})

    def test_transient_error_destroys_the_connection(self):
        self.server.put("nas", "share", "f", b"x")
        with self.assertRaises(SMBConnectionClosed):
            with self.backend.session():
                self.backend.stat(self.loc("f"))
                self.server.kill_sessions()
                self.backend.stat(self.loc("f"))
        self.assertEqual(self.backend._pool.stats()["total"], 0)
        self.assertEqual(self.server.reset_calls, 1)
        self.backend.stat(self.loc("f"))                      # a fresh connection works
        self.assertEqual(len(self.server.register_calls), 2)

    def test_ordinary_error_keeps_the_connection(self):
        with self.assertRaises(FileNotFoundError):
            with self.backend.session():
                self.backend.stat(self.loc("missing"))
        self.assertEqual(self.backend._pool.stats(), {"total": 1, "idle": 1, "size": 4})
        self.assertEqual(self.server.reset_calls, 0)

    def test_file_outside_a_session_owns_a_connection_until_closed(self):
        self.server.put("nas", "share", "f", b"data")
        f = self.backend.open_read(self.loc("f"))
        self.assertEqual(self.backend._pool.stats()["idle"], 0)
        self.assertEqual(f.read(), b"data")
        f.close()
        self.assertEqual(self.backend._pool.stats()["idle"], 1)

    def test_closing_a_file_on_a_dead_connection_does_not_touch_it(self):
        self.server.put("nas", "share", "f", b"data")
        with self.backend.session():
            f = self.backend.open_read(self.loc("f"))
            self.server.kill_sessions()
            with self.assertRaises(SMBConnectionClosed):
                f.read()
            self.assertEqual(self.server.dead_calls, 1)        # the read itself
            f.close()                                          # must not raise...
            self.assertEqual(self.server.dead_calls, 1)        # ...nor even try to talk to the dead link
        self.assertEqual(self.backend._pool.stats()["total"], 0)

    def test_exhausted_pool_makes_the_next_session_wait(self):
        b = self.make_backend(pool_size=1)
        self.addCleanup(b.close)
        order, hold, entered = [], threading.Event(), threading.Event()

        def first():
            with b.session():
                order.append("first in")
                entered.set()
                hold.wait(5)
                order.append("first out")

        def second():
            entered.wait(5)
            with b.session():
                order.append("second in")
        ts = [threading.Thread(target=first), threading.Thread(target=second)]
        [t.start() for t in ts]
        time.sleep(0.2)
        self.assertEqual(order, ["first in"])
        hold.set()
        [t.join(5) for t in ts]
        self.assertEqual(order, ["first in", "first out", "second in"])

    def test_pause_gate(self):
        self.backend.pause_new_connections()
        entered = threading.Event()

        def run():
            with self.backend.session():
                entered.set()
        t = threading.Thread(target=run)
        t.start()
        self.assertFalse(entered.wait(0.2))
        self.backend.resume_new_connections()
        self.assertTrue(entered.wait(2))
        t.join(2)


class LockTests(SMBCase):
    def test_second_locker_is_refused_then_allowed_after_release(self):
        # What smbclient's share_access=None means is not verified, so the lock must work either way.
        for default in ("", "rwd"):
            with self.subTest(default_share=default):
                self.server.default_share = default
                target = self.loc(f"d{len(default)}.bin")
                with self.backend.lock(target):
                    with self.assertRaises(LockedError):
                        with self.backend.lock(target):
                            pass
                    self.assertIn(f"d{len(default)}.bin.lock", self.server.names("nas", "share"))
                self.assertNotIn(f"d{len(default)}.bin.lock", self.server.names("nas", "share"))
                with self.backend.lock(target):
                    pass

    def test_lock_does_not_touch_the_destination(self):
        self.server.put("nas", "share", "d.bin", b"precious")
        with self.backend.lock(self.loc("d.bin")):
            self.assertEqual(self.server.get("nas", "share", "d.bin"), b"precious")

    def test_delete_pending_is_retried(self):
        self.server.delete_pending.add(("nas", "share", "d.bin.lock"))
        started = time.time()
        with self.backend.lock(self.loc("d.bin")):
            pass
        self.assertGreaterEqual(time.time() - started, 0.15)

    def test_lock_left_by_a_dead_connection_is_usable_again(self):
        target = self.loc("d.bin")
        with self.assertRaises(SMBConnectionClosed):
            with self.backend.session():
                with self.backend.lock(target):
                    self.server.kill_sessions()
                    self.backend.stat(target.parent)
        self.assertEqual(self.server.dead_calls, 1)                       # only the deliberate stat:
        #                                    releasing the lock did not poke the dead connection
        self.assertIn("d.bin.lock", self.server.names("nas", "share"))   # sidecar stays behind...
        with self.backend.lock(target):                                   # ...but is not a lock
            pass


class CredentialFlowTests(SMBCase):
    class Spy:
        def __init__(self, creds):
            self.creds, self.gets, self.remembered, self.invalidated = creds, 0, [], []

        def get(self, host):
            self.gets += 1
            time.sleep(0.02)
            return self.creds

        def remember(self, host, creds):
            self.remembered.append((host, creds))

        def invalidate(self, host):
            self.invalidated.append(host)

    def test_provider_asked_once_per_host_even_from_many_threads(self):
        spy = self.Spy(Credentials("alice", "secret"))
        b = self.make_backend(spy)
        self.addCleanup(b.close)
        self.server.put("nas", "share", "f", b"x")
        ts = [threading.Thread(target=lambda: b.stat(self.loc("f"))) for _ in range(8)]
        [t.start() for t in ts]
        [t.join(5) for t in ts]
        self.assertEqual(spy.gets, 1)
        self.assertEqual(spy.remembered, [("nas", Credentials("alice", "secret"))])   # once

    def test_rejected_login_raises_authentication_error_and_invalidates(self):
        spy = self.Spy(Credentials("alice", "WRONG"))
        b = self.make_backend(spy)
        self.addCleanup(b.close)
        with self.assertRaises(AuthenticationError):
            b.stat(self.loc("f"))
        self.assertEqual(spy.invalidated, ["nas"])
        self.assertEqual(spy.remembered, [])
        self.assertFalse(b.is_transient(AuthenticationError("x")))
        spy.creds = Credentials("alice", "secret")           # user fixes it; next try asks again
        self.server.put("nas", "share", "f", b"x")
        self.assertEqual(b.stat(self.loc("f")).size, 1)
        self.assertEqual(spy.gets, 2)

    def test_check_connection(self):
        self.assertTrue(self.backend.check_connection("smb://nas/share/anything"))
        self.assertFalse(self.backend.check_connection("smb://nas/share/x", Credentials("alice", "nope")))
        self.assertFalse(self.backend.check_connection("smb://nas/missing-share/x"))
        self.assertEqual(self.backend._pool.stats()["total"], 0)   # never touches the pool


class ClassificationTests(SMBCase):
    def test_table(self):
        t = self.backend.is_transient
        # NB: the 'credits are available' marker comes from the original code; the real
        # smbprotocol wording has not been verified.
        yes = [SMBConnectionClosed("x"), SMBOSError(ST["PIPE_BROKEN"]), ConnectionResetError(),
               TimeoutError(), OSError(errno.EINVAL, "Invalid argument"), BrokenPipeError(),
               Exception("... until credits are available ..."), SMBOSError(0xC00000B5)]
        no = [SMBOSError(ST["NOT_FOUND"]), SMBOSError(ST["SHARING"]), PermissionError(), ValueError(),
              AuthenticationError("x"), LockedError("x"), FileNotFoundError(), SMBOSError(ST["ACCESS_DENIED"])]
        for e in yes:
            self.assertTrue(t(e), repr(e))
        for e in no:
            self.assertFalse(t(e), repr(e))

    def test_status_is_found_in_the_message_when_there_is_no_attribute(self):
        self.assertTrue(self.backend.is_transient(OSError("Unable to read: STATUS_NETWORK_NAME_DELETED")))
        self.assertTrue(self.backend.is_transient(OSError("failed with 0xC000020C")))
        self.assertFalse(self.backend.is_transient(OSError("denied 0xc0000022")))


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        reset_backends()

    def test_missing_smbprotocol_gives_an_actionable_error(self):
        reset_backends()
        with mock.patch.object(smb_module, "_smbclient", None):
            with self.assertRaises(UnsupportedSchemeError) as cm:
                get_backend(Location.parse("smb://nas/share/x"))
        self.assertIn("carefulcopy[smb]", str(cm.exception))

    def test_unknown_scheme(self):
        with self.assertRaises(UnsupportedSchemeError):
            get_backend(Location.parse("ftp://h/x"))

    def test_configure_smb_replaces_and_closes_the_previous_backend(self):
        client = fake_smb.FakeServer().client
        first = configure_smb(smbclient_module=client, auto_reap=False)
        self.assertIs(get_backend(Location.parse("smb://nas/share/x")), first)
        second = configure_smb(smbclient_module=client, auto_reap=False)
        self.assertIsNot(first, second)
        self.assertIs(get_backend(Location.parse("smb://nas/share/x")), second)
        with self.assertRaises(PoolClosedError):
            first._pool.acquire()


if __name__ == "__main__":
    unittest.main()
