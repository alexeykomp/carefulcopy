"""A small in-memory imitation of the parts of ``smbclient`` that carefulcopy uses.

It models what matters for our logic - UNC paths, share-access modes
(``r``/``w``/``d``), STATUS_SHARING_VIOLATION, connections that die mid-write,
DELETE_PENDING, logins - NOT the real wire protocol. It encodes our *understanding*
of smbclient's behaviour, so tests against it cannot prove that understanding
correct; that needs a real server (see README, "Testing against a real server").
"""
import errno
import stat as stat_mod
import threading
import time

ST = dict(
    NOT_FOUND=0xC0000034, PATH_NOT_FOUND=0xC000003A, COLLISION=0xC0000035,
    ACCESS_DENIED=0xC0000022, SHARING=0xC0000043, DELETE_PENDING=0xC0000056,
    LOGON=0xC000006D, PIPE_BROKEN=0xC000014B,
)
_ERRNO = {ST["NOT_FOUND"]: errno.ENOENT, ST["PATH_NOT_FOUND"]: errno.ENOENT,
          ST["COLLISION"]: errno.EEXIST, ST["ACCESS_DENIED"]: errno.EACCES}


class SMBOSError(OSError):
    def __init__(self, status, path=""):
        super().__init__(_ERRNO.get(status, errno.EIO), f"[NtStatus 0x{status:08x}] {path}")
        self.ntstatus = status


class SMBConnectionClosed(Exception):
    pass


class LogonFailure(Exception):
    status = ST["LOGON"]


class _Node:
    def __init__(self):
        self.data = bytearray()
        self.mtime = time.time()


class _Sess:
    def __init__(self):
        self.dead = False


class _Handle:
    def __init__(self, key, sess, share, reads, writes):
        self.key, self.sess, self.share, self.reads, self.writes = key, sess, share, reads, writes


class FakeServer:
    def __init__(self):
        self.lock = threading.RLock()
        self.files = {}                 # (host, share, rel...) -> _Node
        self.dirs = set()
        self.shares = {("nas", "share")}
        self.users = {"alice": "secret"}        # None => anonymous allowed
        self.handles = []
        self.sessions = []
        self.write_failures = []        # byte limits for the next data-file write-opens (None = healthy)
        self.delete_pending = set()     # keys that answer STATUS_DELETE_PENDING once
        self.register_calls = []
        self.reset_calls = 0
        self.open_log = []              # (path, mode, share)
        # False = a silent drop: the server keeps the dead connection's handles until it notices
        # (call expire_zombies()). True = Samba-style cleanup on TCP reset.
        self.release_handles_on_drop = False
        self.dead_calls = 0             # operations attempted on an already-dead connection
        self.latency = 0.0              # seconds per read/write call (makes parallel copies overlap)
        self.default_share = ""         # what share_access=None means; real smbclient's default is unverified
        self.client = FakeClient(self)

    # -- helpers for tests ---------------------------------------------
    def put(self, host, share, rel, data, mtime=1_700_000_000):
        rel = tuple(rel.strip("/").split("/"))
        with self.lock:
            for i in range(1, len(rel)):
                self.dirs.add((host, share) + rel[:i])
            n = self.files[(host, share) + rel] = _Node()
            n.data = bytearray(data)
            n.mtime = mtime

    def get(self, host, share, rel):
        node = self.files.get((host, share) + tuple(rel.strip("/").split("/")))
        return None if node is None else bytes(node.data)

    def names(self, host, share, rel=""):
        base = (host, share) + (tuple(rel.strip("/").split("/")) if rel else ())
        with self.lock:
            out = set()
            for k in list(self.files) + list(self.dirs):
                if len(k) == len(base) + 1 and k[:len(base)] == base:
                    out.add(k[-1])
        return sorted(out)

    @property
    def live_handles(self):
        return [h for h in self.handles if not h.sess.dead]

    @property
    def zombie_handles(self):
        return [h for h in self.handles if h.sess.dead]

    def expire_zombies(self):
        """The server finally notices the dead connections and closes their handles."""
        with self.lock:
            self.handles = [h for h in self.handles if not h.sess.dead]

    def kill_sessions(self):
        with self.lock:
            for s in self.sessions:
                self._kill(s)

    def _kill(self, sess):
        sess.dead = True
        if self.release_handles_on_drop:
            self.handles = [h for h in self.handles if h.sess is not sess]


class FakeClient:
    """Stands in for the ``smbclient`` module."""

    def __init__(self, server):
        self.s = server

    # -- sessions -------------------------------------------------------
    def register_session(self, server, username=None, password=None, port=445,
                         connection_cache=None, connection_timeout=60, **kw):
        s = self.s
        with s.lock:
            s.register_calls.append((server, username, port))
            if s.users is not None and s.users.get(username) != password:
                raise LogonFailure("bad credentials")
            sess = connection_cache.get(server)
            if sess is None or sess.dead:
                sess = _Sess()
                connection_cache[server] = sess
                s.sessions.append(sess)

    def reset_connection_cache(self, fail_on_error=True, connection_cache=None):
        with self.s.lock:
            self.s.reset_calls += 1
            for sess in list(connection_cache.values()):
                self.s._kill(sess)
            connection_cache.clear()

    # -- plumbing ---------------------------------------------------------
    def _parse(self, unc):
        parts = unc.lstrip("\\").split("\\")
        return parts[0], parts[1], tuple(parts[2:])

    def _sess(self, cache, host):
        sess = cache.get(host)
        assert sess is not None, "operation on an unregistered host - backend forgot register_session"
        if sess.dead:
            self.s.dead_calls += 1
            raise SMBConnectionClosed("connection is closed")
        return sess

    def _check_share(self, host, share, path):
        if (host, share) not in self.s.shares:
            raise SMBOSError(0xC00000CC, path)

    # -- metadata -------------------------------------------------------
    def stat(self, path, connection_cache=None, **kw):
        host, share, rel = self._parse(path)
        with self.s.lock:
            self._sess(connection_cache, host)
            self._check_share(host, share, path)
            key = (host, share) + rel
            if not rel or key in self.s.dirs:
                return _Stat(0, stat_mod.S_IFDIR | 0o755, time.time())
            node = self.s.files.get(key)
            if node is None:
                raise SMBOSError(ST["NOT_FOUND"], path)
            return _Stat(len(node.data), stat_mod.S_IFREG | 0o644, node.mtime)

    def listdir(self, path, connection_cache=None, **kw):
        host, share, rel = self._parse(path)
        with self.s.lock:
            self._sess(connection_cache, host)
            self._check_share(host, share, path)
            return self.s.names(host, share, "/".join(rel))

    def utime(self, path, times=None, ns=None, connection_cache=None, **kw):
        host, share, rel = self._parse(path)
        with self.s.lock:
            self._sess(connection_cache, host)
            node = self.s.files.get((host, share) + rel)
            if node is None:
                raise SMBOSError(ST["NOT_FOUND"], path)
            node.mtime = times[1]

    # -- namespace ---------------------------------------------------------
    def makedirs(self, path, exist_ok=False, connection_cache=None, **kw):
        host, share, rel = self._parse(path)
        with self.s.lock:
            self._sess(connection_cache, host)
            self._check_share(host, share, path)
            if (host, share) + rel in self.s.files:
                raise SMBOSError(ST["COLLISION"], path)
            if (host, share) + rel in self.s.dirs and not exist_ok:
                raise SMBOSError(ST["COLLISION"], path)
            for i in range(1, len(rel) + 1):
                self.s.dirs.add((host, share) + rel[:i])

    def _busy(self, key, need_delete=True):
        return any(h.key == key and 'd' not in h.share for h in self.s.handles)

    def remove(self, path, connection_cache=None, **kw):
        host, share, rel = self._parse(path)
        key = (host, share) + rel
        with self.s.lock:
            self._sess(connection_cache, host)
            if key not in self.s.files:
                raise SMBOSError(ST["NOT_FOUND"], path)
            if self._busy(key):
                raise SMBOSError(ST["SHARING"], path)
            del self.s.files[key]

    def replace(self, src, dst, connection_cache=None, **kw):
        self._move(src, dst, connection_cache, overwrite=True)

    def rename(self, src, dst, connection_cache=None, **kw):
        self._move(src, dst, connection_cache, overwrite=False)

    def _move(self, src, dst, cache, overwrite):
        host, share, rel = self._parse(src)
        dhost, dshare, drel = self._parse(dst)
        sk, dk = (host, share) + rel, (dhost, dshare) + drel
        with self.s.lock:
            self._sess(cache, host)
            if sk not in self.s.files:
                raise SMBOSError(ST["NOT_FOUND"], src)
            if self._busy(sk):
                raise SMBOSError(ST["SHARING"], src)
            if dk in self.s.files:
                if not overwrite:
                    raise SMBOSError(ST["COLLISION"], dst)
                if self._busy(dk):
                    raise SMBOSError(ST["SHARING"], dst)
            self.s.files[dk] = self.s.files.pop(sk)
            for h in self.s.handles:               # open handles follow the file, not the name
                if h.key == sk:
                    h.key = dk

    # -- data -----------------------------------------------------------
    def open_file(self, path, mode="r", share_access=None, connection_cache=None, **kw):
        host, share, rel = self._parse(path)
        key = (host, share) + rel
        share_access = self.s.default_share if share_access is None else share_access
        writes = mode in ("wb", "r+b", "ab")
        reads = mode in ("rb", "r+b")
        with self.s.lock:
            sess = self._sess(connection_cache, host)
            self.s.open_log.append((path, mode, share_access))
            self._check_share(host, share, path)
            if key in self.s.delete_pending:
                self.s.delete_pending.discard(key)
                raise SMBOSError(ST["DELETE_PENDING"], path)
            if len(rel) > 1 and (host, share) + rel[:-1] not in self.s.dirs:
                raise SMBOSError(ST["PATH_NOT_FOUND"], path)
            exists = key in self.s.files
            if not exists and mode in ("rb", "r+b"):
                raise SMBOSError(ST["NOT_FOUND"], path)
            for h in self.s.handles:
                if h.key != key:
                    continue
                if (writes and 'w' not in h.share) or (reads and 'r' not in h.share) \
                        or (h.writes and 'w' not in share_access) or (h.reads and 'r' not in share_access):
                    raise SMBOSError(ST["SHARING"], path)
            node = self.s.files.get(key)
            if node is None:
                node = self.s.files[key] = _Node()
            if mode == "wb":
                node.data = bytearray()
                node.mtime = time.time()
            limit = None
            if writes and not path.endswith(".lock") and self.s.write_failures:
                limit = self.s.write_failures.pop(0)   # only data files; lock sidecars never fail
            handle = _Handle(key, sess, share_access, reads, writes)
            self.s.handles.append(handle)
            f = _File(self.s, node, handle, mode, limit)
            if mode == "ab":
                f.pos = len(node.data)
            return f


class _Stat:
    def __init__(self, size, mode, mtime):
        self.st_size, self.st_mode, self.st_mtime = size, mode, mtime


class _File:
    def __init__(self, server, node, handle, mode, limit):
        self.s, self.node, self.h, self.mode, self.limit = server, node, handle, mode, limit
        self.pos = 0
        self.closed = False

    def _live(self):
        if self.h.sess.dead:
            self.s.dead_calls += 1
            raise SMBConnectionClosed("connection is closed")

    def read(self, n=-1):
        if self.s.latency:
            time.sleep(self.s.latency)
        with self.s.lock:
            self._live()
            end = len(self.node.data) if n is None or n < 0 else self.pos + n
            out = bytes(self.node.data[self.pos:end])
            self.pos += len(out)
            return out

    def write(self, data):
        if self.s.latency:
            time.sleep(self.s.latency)
        with self.s.lock:
            self._live()
            if self.mode == "ab":
                self.pos = len(self.node.data)
            if self.limit is not None and len(data) > self.limit:
                part = data[:self.limit]
                self._put(part)
                self.limit = 0
                self.s._kill(self.h.sess)           # the link dies mid-write
                raise SMBOSError(ST["PIPE_BROKEN"], "write")
            if self.limit is not None:
                self.limit -= len(data)
            self._put(data)
            return len(data)

    def _put(self, data):
        d = self.node.data
        if self.pos > len(d):
            d.extend(b"\0" * (self.pos - len(d)))
        d[self.pos:self.pos + len(data)] = data
        self.pos += len(data)
        self.node.mtime = time.time()

    def seek(self, offset, whence=0):
        with self.s.lock:
            self._live()
            base = {0: 0, 1: self.pos, 2: len(self.node.data)}[whence]
            self.pos = base + offset
            return self.pos

    def tell(self):
        return self.pos

    def truncate(self, size=None):
        with self.s.lock:
            self._live()
            size = self.pos if size is None else size
            del self.node.data[size:]
            return size

    def flush(self):
        self._live()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._live()                                  # like the real thing, closing needs the link
        with self.s.lock:
            if self.h in self.s.handles:
                self.s.handles.remove(self.h)
