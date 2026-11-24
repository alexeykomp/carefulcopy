"""SMB backend built on ``smbprotocol``'s high-level ``smbclient`` API.

Install with ``pip install carefulcopy[smb]``. Locations look like
``smb://host[:port]/share/dir/file``.

Design notes
------------
* **Sessions.** ``smbclient`` keeps connections in a plain ``dict`` (the
  *connection cache*). We keep a :class:`~carefulcopy.pool.ConnectionPool` of
  such dicts. The copy engine wraps each attempt in ``backend.session()``,
  which binds one cache to the current thread, so source, destination, lock
  and all metadata calls of one attempt travel over **one** connection and
  the pool can never deadlock on a single copy. When a transient error leaves
  the session, the cache is destroyed instead of being reused.
* **Locks.** ``<name>.lock`` is created, then re-opened with a share mode that
  forbids other writers; a competitor's attempt to create it fails with
  ``STATUS_SHARING_VIOLATION`` -> :class:`LockedError`. (The original design
  locked the destination file itself, which truncated an existing file the
  moment the lock was taken; a sidecar keeps the old file intact until the
  final rename.)
* **Errors.** Classification uses NT status codes, not message substrings;
  see ``_STATUS``.

Everything that touches ``smbclient`` is in this module so that it is easy to
audit against the library's documentation.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import socket
import stat as stat_mod
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional, Set, Tuple

try:  # optional dependency
    import smbclient as _smbclient
except ImportError:  # pragma: no cover - exercised only without smbprotocol
    _smbclient = None

from ..credentials import CredentialProvider, Credentials, EnvCredentials
from ..errors import AuthenticationError, CopyError, LockedError, TransientError
from ..location import Location
from ..pool import ConnectionPool
from ..retry import is_transient_error
from .base import Backend, Entry, FileInfo

logger = logging.getLogger("carefulcopy.smb")

# NT status codes (MS-ERREF) used for classification.
_STATUS: Dict[str, int] = {
    "STATUS_OBJECT_NAME_NOT_FOUND": 0xC0000034,
    "STATUS_OBJECT_PATH_NOT_FOUND": 0xC000003A,
    "STATUS_NO_SUCH_FILE": 0xC000000F,
    "STATUS_BAD_NETWORK_NAME": 0xC00000CC,
    "STATUS_OBJECT_NAME_COLLISION": 0xC0000035,
    "STATUS_ACCESS_DENIED": 0xC0000022,
    "STATUS_SHARING_VIOLATION": 0xC0000043,
    "STATUS_DELETE_PENDING": 0xC0000056,
    "STATUS_LOGON_FAILURE": 0xC000006D,
    "STATUS_DISK_FULL": 0xC000007F,
    # connection-level problems: worth resuming after
    "STATUS_PIPE_BROKEN": 0xC000014B,
    "STATUS_CONNECTION_RESET": 0xC000020D,
    "STATUS_CONNECTION_DISCONNECTED": 0xC000020C,
    "STATUS_IO_TIMEOUT": 0xC00000B5,
    "STATUS_NETWORK_NAME_DELETED": 0xC00000C9,
    "STATUS_NETWORK_UNREACHABLE": 0xC000023C,
    "STATUS_HOST_UNREACHABLE": 0xC000023D,
    "STATUS_USER_SESSION_DELETED": 0xC0000203,
    "STATUS_NETWORK_SESSION_EXPIRED": 0xC000035C,
}
_BY_VALUE = {v: k for k, v in _STATUS.items()}
_NOT_FOUND = {_STATUS[n] for n in ("STATUS_OBJECT_NAME_NOT_FOUND", "STATUS_OBJECT_PATH_NOT_FOUND",
                                   "STATUS_NO_SUCH_FILE")}
_TRANSIENT = {_STATUS[n] for n in (
    "STATUS_PIPE_BROKEN", "STATUS_CONNECTION_RESET", "STATUS_CONNECTION_DISCONNECTED",
    "STATUS_IO_TIMEOUT", "STATUS_NETWORK_NAME_DELETED", "STATUS_NETWORK_UNREACHABLE",
    "STATUS_HOST_UNREACHABLE", "STATUS_USER_SESSION_DELETED", "STATUS_NETWORK_SESSION_EXPIRED")}
_SHARING_VIOLATION = _STATUS["STATUS_SHARING_VIOLATION"]
_DELETE_PENDING = _STATUS["STATUS_DELETE_PENDING"]
_LOGON_FAILURE = _STATUS["STATUS_LOGON_FAILURE"]

# Errors that smbprotocol raises without a usable status code.
_TRANSIENT_MESSAGES = ("timed out", "credits are available", "Connection reset", "Broken pipe")
# Socket-level OSErrors (no NT status attached) seen on dropped links.
_TRANSIENT_SOCKET_ERRNOS = {errno.EINVAL}

_HEX_RE = re.compile(r"0x([0-9a-fA-F]{8})")
_NAME_RE = re.compile(r"STATUS_[A-Z0-9_]+")
_ILLEGAL_CHARS = '<>:"/\\|?*'
_SANITIZE_TABLE = str.maketrans(_ILLEGAL_CHARS, "_" * len(_ILLEGAL_CHARS))


def _ntstatus(exc: BaseException) -> Optional[int]:
    """Best-effort extraction of the NT status code from an smbprotocol error."""
    for attr in ("ntstatus", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value & 0xFFFFFFFF
    text = str(exc)
    m = _HEX_RE.search(text)
    if m:
        return int(m.group(1), 16)
    m = _NAME_RE.search(text)
    if m:
        return _STATUS.get(m.group(0))
    return None


# Who is allowed to claim a stale lock as "mine"? Only locks written by this very process whose
# owner is not currently inside a ``lock()`` block (so: the copy that wrote it is over, and the
# server-side handle is a leftover of a dead connection). Another thread's live lock is never touched.
_PROCESS_ID = uuid.uuid4().hex
_ACTIVE_OWNERS: Dict[str, int] = {}
_ACTIVE_LOCK = threading.Lock()


@contextmanager
def _active(owner: str) -> Iterator[None]:
    with _ACTIVE_LOCK:
        _ACTIVE_OWNERS[owner] = _ACTIVE_OWNERS.get(owner, 0) + 1
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            n = _ACTIVE_OWNERS.get(owner, 1) - 1
            if n <= 0:
                _ACTIVE_OWNERS.pop(owner, None)
            else:
                _ACTIVE_OWNERS[owner] = n


def _owner_is_active(owner: Any) -> bool:
    with _ACTIVE_LOCK:
        return owner in _ACTIVE_OWNERS


class _Session:
    """One pooled connection cache, plus what we registered on it."""

    __slots__ = ("cache", "hosts", "broken")

    def __init__(self, cache: dict) -> None:
        self.cache = cache
        self.hosts: Set[Tuple[str, Optional[int]]] = set()
        self.broken = False


class _SMBFile:
    """Wraps an smbclient file: tracks connection health, never closes a dead one."""

    def __init__(self, raw: BinaryIO, backend: "SMBBackend", session: _Session, detached: bool) -> None:
        self._raw, self._backend, self._session, self._detached = raw, backend, session, detached
        self._closed = False

    def _guard(self, fn: Callable, *args):
        try:
            return fn(*args)
        except Exception as exc:
            if self._backend.is_transient(exc):
                self._backend._mark_broken(self._session, exc, "file I/O")
            raise

    def read(self, n: int = -1) -> bytes:
        return self._guard(self._raw.read, n)

    def write(self, data: bytes) -> int:
        return self._guard(self._raw.write, data)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._guard(self._raw.seek, offset, whence)

    def tell(self) -> int:
        return self._guard(self._raw.tell)

    def flush(self) -> None:
        self._guard(self._raw.flush)

    def truncate(self, size: Optional[int] = None):
        return self._guard(self._raw.truncate, size) if size is not None else self._guard(self._raw.truncate)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        error: Optional[BaseException] = None
        try:
            if not self._session.broken:
                try:
                    self._raw.close()
                except Exception as exc:
                    if self._backend.is_transient(exc):
                        self._backend._mark_broken(self._session, exc, "closing a file")
                    error = exc
            # else: the connection is presumed dead; talking to it could hang.
        finally:
            if self._detached:
                self._backend._end_session(self._session)
        if error is not None:
            raise error

    def __enter__(self) -> "_SMBFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
            return
        try:  # do not let a failing close() hide the original error
            self.close()
        except Exception as close_exc:
            logger.debug("ignoring error while closing after failure: %s", close_exc)


class SMBBackend(Backend):
    """Talk to SMB2/3 shares.

    Args:
        credentials: a provider (see :mod:`carefulcopy.credentials`). The default
            reads ``CAREFULCOPY_SMB_USERNAME`` / ``_PASSWORD`` and otherwise
            leaves authentication to ``smbclient`` (e.g. Kerberos).
        pool_size: maximum number of simultaneous connections.
        idle_timeout: close pooled connections idle for this many seconds.
        connection_timeout: seconds to wait when connecting.
        read_share / write_share: SMB share-access masks used when opening the
            source and the ``.part`` file (``r``, ``w``, ``d`` letters).
        sanitize_names: replace characters Windows forbids (``<>:"/\\|?*``)
            by ``_`` in path components below the share.
        normalize_unicode: Unicode normalisation form applied to remote path
            components (``"NFC"`` by default, ``None`` to disable). This
            avoids mismatches with names that come from macOS (NFD).
        smbclient_module: inject a replacement for ``smbclient`` (testing).
    """

    is_remote = True

    def __init__(self, credentials: Optional[CredentialProvider] = None, *,
                 pool_size: int = 8, idle_timeout: float = 30.0, connection_timeout: float = 5.0,
                 read_share: str = "r", write_share: str = "rwd",
                 sanitize_names: bool = False, normalize_unicode: Optional[str] = "NFC",
                 smbclient_module: Any = None, auto_reap: bool = True) -> None:
        self._smb = smbclient_module if smbclient_module is not None else _smbclient
        if self._smb is None:
            raise ImportError("SMB support requires smbprotocol: pip install 'carefulcopy[smb]'")
        self._provider: CredentialProvider = credentials if credentials is not None else EnvCredentials()
        self._connection_timeout = connection_timeout
        self._read_share, self._write_share = read_share, write_share
        self._sanitize, self._normalize = sanitize_names, normalize_unicode
        self._creds: Dict[str, Optional[Credentials]] = {}
        self._confirmed: Set[str] = set()
        self._cred_lock = threading.Lock()
        self._tls = threading.local()
        self._gate = threading.Event()
        self._gate.set()
        self._pool: ConnectionPool[dict] = ConnectionPool(
            factory=dict, closer=self._close_cache, size=pool_size,
            idle_timeout=idle_timeout, auto_reap=auto_reap)

    # ------------------------------------------------------------------
    # lifecycle / flow control
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Close every pooled connection and stop the background reaper."""
        self._pool.close()

    def __enter__(self) -> "SMBBackend":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def pause_new_connections(self) -> None:
        """Make new sessions wait (e.g. while a "retry?" dialog is shown)."""
        self._gate.clear()

    def resume_new_connections(self) -> None:
        self._gate.set()

    def _close_cache(self, cache: dict) -> None:
        try:
            self._smb.reset_connection_cache(connection_cache=cache)
        except Exception as exc:
            logger.warning("error closing SMB connection cache: %s", exc)

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def _begin_session(self) -> _Session:
        self._gate.wait()
        return _Session(self._pool.acquire())

    def _end_session(self, s: _Session) -> None:
        if s.broken:
            logger.debug("connection discarded (it was marked broken); pool: %s", self._pool.stats())
            self._pool.discard(s.cache)   # also closes the connections
        else:
            self._pool.release(s.cache)
            logger.debug("connection returned to the pool; pool: %s", self._pool.stats())

    def _mark_broken(self, s: _Session, exc: BaseException, where: str) -> None:
        if not s.broken:
            logger.debug("connection marked broken (%s): %r", where, exc)
        s.broken = True

    def _note_failure(self, s: _Session, exc: BaseException) -> None:
        if self.is_transient(exc):
            self._mark_broken(s, exc, "operation failed")

    def pool_stats(self) -> Dict[str, int]:
        """``{'total', 'idle', 'size'}`` of the connection pool (diagnostics)."""
        return self._pool.stats()

    @contextmanager
    def session(self) -> Iterator[_Session]:
        current: Optional[_Session] = getattr(self._tls, "session", None)
        if current is not None:          # nested: join the thread's session
            try:
                yield current
            except BaseException as exc:
                self._note_failure(current, exc)
                raise
            return
        s = self._begin_session()
        self._tls.session = s
        try:
            yield s
        except BaseException as exc:
            self._note_failure(s, exc)
            raise
        finally:
            self._tls.session = None
            self._end_session(s)

    # ------------------------------------------------------------------
    # credentials
    # ------------------------------------------------------------------
    def _credentials_for(self, host: str) -> Optional[Credentials]:
        with self._cred_lock:             # one prompt at a time, then cached
            if host not in self._creds:
                self._creds[host] = self._provider.get(host)
            return self._creds[host]

    def _forget_credentials(self, host: str) -> None:
        with self._cred_lock:
            self._creds.pop(host, None)
            self._confirmed.discard(host)
        hook = getattr(self._provider, "invalidate", None)
        if hook:
            hook(host)

    def _confirm_credentials(self, host: str, creds: Optional[Credentials]) -> None:
        with self._cred_lock:
            if host in self._confirmed:
                return
            self._confirmed.add(host)
        hook = getattr(self._provider, "remember", None)
        if hook:
            try:
                hook(host, creds)
            except Exception as exc:
                logger.warning("credential 'remember' hook failed: %s", exc)

    def _ensure_registered(self, s: _Session, host: str, port: Optional[int]) -> None:
        key = (host, port)
        if key in s.hosts:
            return
        creds = self._credentials_for(host)
        kwargs: Dict[str, Any] = {}
        if creds is not None:
            kwargs.update(username=creds.username, password=creds.password)
        if port:
            kwargs["port"] = port
        try:
            self._smb.register_session(
                host, connection_cache=s.cache, connection_timeout=self._connection_timeout, **kwargs)
        except Exception as exc:
            if self._is_auth_failure(exc):
                self._forget_credentials(host)
                raise AuthenticationError(f"Login to {host} was rejected") from exc
            raise
        s.hosts.add(key)
        self._confirm_credentials(host, creds)

    @staticmethod
    def _is_auth_failure(exc: BaseException) -> bool:
        return type(exc).__name__ == "LogonFailure" or _ntstatus(exc) == _LOGON_FAILURE

    def check_connection(self, location: Any, credentials: Optional[Credentials] = None) -> bool:
        """Try to log in and list the share root; True on success. Uses a throw-away
        connection, never the pool. Handy for a "Test connection" button."""
        loc = Location.parse(location)
        host, port = self._split_host(loc.netloc)
        share = self._components(loc)[0]
        creds = credentials if credentials is not None else self._credentials_for(host)
        cache: dict = {}
        try:
            kwargs: Dict[str, Any] = {}
            if creds is not None:
                kwargs.update(username=creds.username, password=creds.password)
            if port:
                kwargs["port"] = port
            self._smb.register_session(host, connection_cache=cache,
                                       connection_timeout=self._connection_timeout, **kwargs)
            self._smb.listdir(rf"\\{host}\{share}", connection_cache=cache, **({"port": port} if port else {}))
            return True
        except Exception as exc:
            logger.info("connection check for %s failed: %s", host, exc)
            return False
        finally:
            self._close_cache(cache)

    # ------------------------------------------------------------------
    # path handling
    # ------------------------------------------------------------------
    @staticmethod
    def _split_host(netloc: str) -> Tuple[str, Optional[int]]:
        if not netloc:
            raise ValueError("SMB location needs a host: smb://host/share/path")
        host, _, port = netloc.partition(":")
        return host, int(port) if port else None

    def _clean(self, part: str) -> str:
        if self._normalize:
            part = unicodedata.normalize(self._normalize, part)
        if self._sanitize:
            part = part.translate(_SANITIZE_TABLE)
        elif "\\" in part:
            raise ValueError(f"Backslash in path component {part!r}; use sanitize_names=True to replace it")
        return part

    def _components(self, loc: Location) -> List[str]:
        comps = [p for p in loc.path.parts if p not in ("/", "")]
        if not comps:
            raise ValueError(f"SMB location needs a share: smb://{loc.netloc}/<share>/path")
        return [comps[0]] + [self._clean(c) for c in comps[1:]]

    def unc(self, loc: Location) -> str:
        """``smb://h/share/a/b`` -> ``\\\\h\\share\\a\\b``."""
        host, _ = self._split_host(loc.netloc)
        return "\\\\" + host + "\\" + "\\".join(self._components(loc))

    def _is_share_root(self, loc: Location) -> bool:
        return len(self._components(loc)) == 1

    # ------------------------------------------------------------------
    # operation plumbing
    # ------------------------------------------------------------------
    @contextmanager
    def _conn(self, loc: Location) -> Iterator[Tuple[str, Dict[str, Any]]]:
        host, port = self._split_host(loc.netloc)
        unc = self.unc(loc)
        with self.session() as s:
            self._ensure_registered(s, host, port)
            kwargs: Dict[str, Any] = {"connection_cache": s.cache}
            if port:
                kwargs["port"] = port
            try:
                yield unc, kwargs
            except Exception as exc:
                translated = self._translate(exc, unc)
                if translated is exc:
                    raise
                raise translated from exc

    @staticmethod
    def _translate(exc: Exception, path: str = "") -> Exception:
        """Map NT status errors to the builtin exceptions callers expect."""
        if isinstance(exc, (FileNotFoundError, FileExistsError, PermissionError)):
            return exc
        status = _ntstatus(exc)
        if status in _NOT_FOUND or (status is None and isinstance(exc, OSError) and exc.errno == errno.ENOENT):
            return FileNotFoundError(errno.ENOENT, f"No such file or directory: {path}")
        if status == _STATUS["STATUS_OBJECT_NAME_COLLISION"]:
            return FileExistsError(errno.EEXIST, f"File exists: {path}")
        if status == _STATUS["STATUS_ACCESS_DENIED"]:
            return PermissionError(errno.EACCES, f"Permission denied: {path}")
        return exc

    def is_transient(self, exc: BaseException) -> bool:
        if isinstance(exc, (AuthenticationError, LockedError)):
            return False
        if type(exc).__name__ == "SMBConnectionClosed":
            return True
        status = _ntstatus(exc)
        if status is not None:
            return status in _TRANSIENT
        if any(m in str(exc) for m in _TRANSIENT_MESSAGES):
            return True
        if isinstance(exc, OSError) and exc.errno in _TRANSIENT_SOCKET_ERRNOS:
            return True
        return is_transient_error(exc)

    # ------------------------------------------------------------------
    # Backend API: metadata
    # ------------------------------------------------------------------
    def stat(self, loc: Location) -> FileInfo:
        with self._conn(loc) as (unc, kw):
            st = self._smb.stat(unc, **kw)
        return FileInfo(size=st.st_size, mtime=st.st_mtime, is_dir=stat_mod.S_ISDIR(st.st_mode),
                        mtime_ns=getattr(st, "st_mtime_ns", None))

    def listdir(self, loc: Location) -> List[str]:
        with self._conn(loc) as (unc, kw):
            return [n for n in self._smb.listdir(unc, **kw) if n not in (".", "..")]

    def scandir(self, loc: Location) -> List[Entry]:
        """One directory query returns names *and* sizes/times, so a big tree costs one round
        trip per directory instead of one per file."""
        if not hasattr(self._smb, "scandir"):
            return super().scandir(loc)
        out: List[Entry] = []
        with self._conn(loc) as (unc, kw):
            for e in self._smb.scandir(unc, **kw):
                if e.name in (".", ".."):
                    continue
                try:
                    st = e.stat()
                    is_dir = stat_mod.S_ISDIR(st.st_mode)
                    symlink = bool(e.is_symlink())
                except Exception as exc:
                    if self.is_transient(exc):
                        raise
                    continue                          # vanished while we listed
                out.append(Entry(e.name, is_dir, st.st_size, st.st_mtime, symlink,
                                 mtime_ns=getattr(st, "st_mtime_ns", None)))
        return out

    def set_mtime(self, loc: Location, mtime: float) -> None:
        with self._conn(loc) as (unc, kw):
            self._smb.utime(unc, times=(mtime, mtime), **kw)

    # ------------------------------------------------------------------
    # Backend API: data
    # ------------------------------------------------------------------
    def _open_file(self, loc: Location, mode: str, share: Optional[str]) -> _SMBFile:
        host, port = self._split_host(loc.netloc)
        unc = self.unc(loc)
        current: Optional[_Session] = getattr(self._tls, "session", None)
        detached = current is None        # outside engine/session(): the file owns a connection
        s = current if current is not None else self._begin_session()
        try:
            self._ensure_registered(s, host, port)
            kwargs: Dict[str, Any] = {"connection_cache": s.cache}
            if share is not None:
                kwargs["share_access"] = share
            if port:
                kwargs["port"] = port
            raw = self._smb.open_file(unc, mode=mode, **kwargs)
        except Exception as exc:
            self._note_failure(s, exc)
            if detached:
                self._end_session(s)
            translated = self._translate(exc, unc)
            if translated is exc:
                raise
            raise translated from exc
        return _SMBFile(raw, self, s, detached)

    def open_read(self, loc: Location) -> BinaryIO:
        return self._open_file(loc, "rb", self._read_share)  # type: ignore[return-value]

    def open_write(self, loc: Location, offset: int = 0) -> BinaryIO:
        if offset <= 0:
            return self._open_file(loc, "wb", self._write_share)  # type: ignore[return-value]
        f = self._open_file(loc, "r+b", self._write_share)
        try:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            if end > offset:
                try:
                    f.truncate(offset)
                except (AttributeError, NotImplementedError, OSError) as exc:
                    if not self.is_transient(exc):
                        raise CopyError(f"Cannot truncate {loc} to {offset} bytes") from exc
                    raise
            elif end < offset:
                raise TransientError(f"{loc} has {end} bytes, expected at least {offset}")
            f.seek(offset)
        except BaseException:
            try:
                f.close()
            except Exception:
                pass
            raise
        return f  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Backend API: namespace
    # ------------------------------------------------------------------
    def makedirs(self, loc: Location, exist_ok: bool = True) -> None:
        if self._is_share_root(loc):
            return                      # a share always exists (or the next call fails clearly)
        with self._conn(loc) as (unc, kw):
            self._smb.makedirs(unc, exist_ok=exist_ok, **kw)

    def remove(self, loc: Location) -> None:
        with self._conn(loc) as (unc, kw):
            self._smb.remove(unc, **kw)

    def rename(self, src: Location, dst: Location) -> None:
        if src.netloc != dst.netloc:
            raise ValueError("Cannot rename across SMB servers")
        with self._conn(src) as (src_unc, kw):
            dst_unc = self.unc(dst)
            if hasattr(self._smb, "replace"):
                self._smb.replace(src_unc, dst_unc, **kw)
            else:                        # older smbprotocol: emulate replace
                try:
                    self._smb.remove(dst_unc, **kw)
                except Exception as exc:
                    if not isinstance(self._translate(exc, dst_unc), FileNotFoundError):
                        raise
                self._smb.rename(src_unc, dst_unc, **kw)

    # ------------------------------------------------------------------
    # Backend API: locking
    # ------------------------------------------------------------------
    @contextmanager
    def lock(self, loc: Location, owner: Optional[str] = None) -> Iterator[None]:
        """Exclusive write lock on *loc*, held as an open ``<name>.lock`` handle.

        The sidecar also contains a small JSON token (owner, process, host, pid). It is purely
        advisory - the real exclusion is the share mode of the open handle - but it lets us tell
        apart three situations when the lock is busy:

        * the holder is **our own stale handle** (same process, and the copy that took it is no
          longer running - typically left behind by a connection that dropped without the server
          noticing yet): we take it over;
        * the holder is **someone else** (another process or machine, or a copy that is still
          running here): we raise :class:`LockedError` naming the holder, and the caller waits;
        * the token is unreadable: treated like someone else.
        """
        owner = owner or uuid.uuid4().hex
        lock_loc = loc.with_name(loc.name + ".lock")
        with _active(owner):
            handle = self._acquire_lock(lock_loc, owner)
            try:
                yield
            finally:
                self._release_lock(handle, lock_loc)

    def _acquire_lock(self, lock_loc: Location, owner: str) -> _SMBFile:
        took_over = False
        while True:
            try:
                handle = self._open_lock_handle(lock_loc)
            except LockedError as busy:
                holder = self._read_lock_token(lock_loc)
                if not took_over and self._is_own_stale_lock(holder, owner) and self._take_over(lock_loc):
                    took_over = True
                    continue
                raise LockedError(self._describe_holder(lock_loc, holder)) from busy
            self._write_lock_token(handle, owner)
            return handle

    def _open_lock_handle(self, lock_loc: Location) -> _SMBFile:
        # 1) make sure the sidecar exists (and is empty)
        for _ in range(5):
            try:
                self._open_file(lock_loc, "wb", None).close()
                break
            except Exception as exc:
                status = _ntstatus(exc)
                if status == _DELETE_PENDING:   # a previous owner is still deleting it
                    time.sleep(0.2)
                    continue
                if status == _SHARING_VIOLATION:
                    raise LockedError(f"{lock_loc.name} is held by another operation") from exc
                raise
        else:
            raise LockedError(f"{lock_loc.name}: still being deleted by another operation")
        # 2) hold it open, refusing write access to everybody else
        try:
            return self._open_file(lock_loc, "r+b", "rd")
        except Exception as exc:
            if _ntstatus(exc) in (_SHARING_VIOLATION, _DELETE_PENDING):
                raise LockedError(f"{lock_loc.name} is held by another operation") from exc
            raise

    def _write_lock_token(self, handle: _SMBFile, owner: str) -> None:
        token = {"v": 1, "owner": owner, "process": _PROCESS_ID, "host": socket.gethostname(),
                 "pid": os.getpid(), "started": time.time()}
        try:
            handle.seek(0)
            handle.write(json.dumps(token).encode("utf-8"))
            handle.flush()
        except Exception as exc:
            if self.is_transient(exc):
                raise
            logger.debug("could not write the lock token: %s", exc)   # the lock itself still works

    def _read_lock_token(self, lock_loc: Location) -> Optional[dict]:
        """Read the holder's token, or None if it is missing/unreadable. The holder has the file
        open for writing, so we must allow write sharing ('rwd') to be admitted."""
        try:
            with self._open_file(lock_loc, "rb", "rwd") as f:
                raw = f.read(4096)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as exc:
            if self.is_transient(exc):
                raise                              # a dead link is not "someone else's lock"
            return None

    @staticmethod
    def _is_own_stale_lock(holder: Optional[dict], owner: str) -> bool:
        if not holder or holder.get("process") != _PROCESS_ID:
            return False                           # other process / machine / restarted app
        holder_owner = holder.get("owner")
        return holder_owner == owner or not _owner_is_active(holder_owner)

    def _take_over(self, lock_loc: Location) -> bool:
        """Move the stale lock out of the way (its handle follows it; the server drops the
        leftover whenever it notices the dead connection) so a fresh one can be created."""
        tombstone = lock_loc.with_name(f"{lock_loc.name}.dead-{uuid.uuid4().hex[:8]}")
        try:
            self.rename(lock_loc, tombstone)
        except Exception as exc:
            if self.is_transient(exc):
                raise
            logger.info("could not take over stale lock %s: %s", lock_loc.name, exc)
            return False
        logger.info("took over our own stale lock %s", lock_loc.name)
        try:
            self.remove(tombstone)
        except Exception as exc:                   # delete-pending until the zombie handle goes away
            logger.debug("could not remove %s yet: %s", tombstone.name, exc)
        return True

    @staticmethod
    def _describe_holder(lock_loc: Location, holder: Optional[dict]) -> str:
        if not holder:
            return f"{lock_loc.name} is held by another operation"
        since = ""
        if isinstance(holder.get("started"), (int, float)):
            since = f" since {time.strftime('%H:%M:%S', time.localtime(holder['started']))}"
        return (f"{lock_loc.name} is held by {holder.get('host', '?')} "
                f"(pid {holder.get('pid', '?')}){since}")

    def _release_lock(self, handle: _SMBFile, lock_loc: Location) -> None:
        broken = handle._session.broken
        try:
            handle.close()
        except Exception as exc:
            logger.debug("error closing lock file: %s", exc)
        if broken:
            return                      # dead connection: leave the empty sidecar behind
        try:
            self.remove(lock_loc)
        except Exception as exc:        # someone else already re-locked it, or it is gone
            logger.debug("could not remove lock file %s: %s", lock_loc, exc)
