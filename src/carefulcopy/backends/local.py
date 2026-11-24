"""Backend for the local filesystem."""
from __future__ import annotations

import errno
import os
import stat as stat_mod
from contextlib import contextmanager
from typing import BinaryIO, Iterator, List, Optional

from ..errors import LockedError
from ..location import Location
from .base import Backend, Entry, FileInfo

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _try_lock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh) -> None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

_LOCK_BUSY = {errno.EAGAIN, errno.EACCES, errno.EDEADLK, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}


class LocalBackend(Backend):
    is_remote = False

    @staticmethod
    def _p(loc: Location) -> str:
        return str(loc.path)

    def stat(self, loc: Location) -> FileInfo:
        st = os.stat(self._p(loc))
        return FileInfo(size=st.st_size, mtime=st.st_mtime, is_dir=stat_mod.S_ISDIR(st.st_mode),
                        mtime_ns=st.st_mtime_ns)

    def listdir(self, loc: Location) -> List[str]:
        return os.listdir(self._p(loc))

    def scandir(self, loc: Location) -> List[Entry]:
        out: List[Entry] = []
        with os.scandir(self._p(loc)) as it:
            for e in it:
                try:
                    symlink = e.is_symlink()
                except OSError:
                    symlink = False
                try:
                    st = e.stat()                     # follows symlinks
                except OSError:
                    if symlink:                       # broken link: report it, the caller decides
                        out.append(Entry(e.name, False, is_symlink=True, is_other=True))
                    continue                          # otherwise it vanished while we listed
                is_dir = stat_mod.S_ISDIR(st.st_mode)
                out.append(Entry(e.name, is_dir, st.st_size, st.st_mtime, symlink,
                                 is_other=not (is_dir or stat_mod.S_ISREG(st.st_mode)),
                                 mtime_ns=st.st_mtime_ns))
        return out

    def set_mtime(self, loc: Location, mtime: float) -> None:
        os.utime(self._p(loc), (mtime, mtime))

    def open_read(self, loc: Location) -> BinaryIO:
        return open(self._p(loc), "rb")

    def open_write(self, loc: Location, offset: int = 0) -> BinaryIO:
        if offset <= 0:
            return open(self._p(loc), "wb")
        fh = open(self._p(loc), "r+b")
        try:
            fh.seek(offset)
            fh.truncate()
        except BaseException:
            fh.close()
            raise
        return fh

    def makedirs(self, loc: Location, exist_ok: bool = True) -> None:
        os.makedirs(self._p(loc), exist_ok=exist_ok)

    def remove(self, loc: Location) -> None:
        os.remove(self._p(loc))

    def rename(self, src: Location, dst: Location) -> None:
        os.replace(self._p(src), self._p(dst))

    # -- locking ----------------------------------------------------------
    @contextmanager
    def lock(self, loc: Location, owner: Optional[str] = None) -> Iterator[None]:
        """Advisory lock on a ``<name>.lock`` sidecar file.

        The OS drops the lock automatically if the process dies, so a crash
        never leaves a stale lock behind (only an empty sidecar file), so *owner*
        is not needed here.
        """
        lock_path = self._p(loc.with_name(loc.name + ".lock"))
        fh = self._acquire(lock_path)
        try:
            yield
        finally:
            self._release(fh, lock_path)

    @staticmethod
    def _acquire(lock_path: str):
        for _ in range(5):
            fh = open(lock_path, "a+b")
            try:
                _try_lock(fh)
            except OSError as exc:
                fh.close()
                if exc.errno in _LOCK_BUSY:
                    raise LockedError(f"Locked by another operation: {lock_path}") from exc
                raise
            # The previous owner deletes the sidecar on release. If we locked
            # an already-unlinked inode, someone else may create a fresh file
            # at the same path, so make sure ours is still the one on disk.
            try:
                if os.path.samestat(os.fstat(fh.fileno()), os.stat(lock_path)):
                    return fh
            except FileNotFoundError:
                pass
            _unlock(fh)
            fh.close()
        raise LockedError(f"Could not acquire lock (contention): {lock_path}")

    @staticmethod
    def _release(fh, lock_path: str) -> None:
        if os.name == "nt":  # pragma: no cover
            _unlock(fh)
            fh.close()
            _remove_quietly(lock_path)
        else:
            # Unlink while still holding the lock; waiters notice via samestat.
            _remove_quietly(lock_path)
            _unlock(fh)
            fh.close()


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
