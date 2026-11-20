"""The contract every storage backend implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, BinaryIO, ContextManager, List, Optional

from ..location import Location
from ..retry import is_transient_error


@dataclass(frozen=True)
class FileInfo:
    size: int
    mtime: float
    is_dir: bool = False
    mtime_ns: Optional[int] = None   # exact modification time, if the backend knows it

    def mtime_key(self) -> int:
        """Modification time in nanoseconds, for fingerprints (derived from ``mtime`` if needed)."""
        return self.mtime_ns if self.mtime_ns is not None else int(round(self.mtime * 1_000_000_000))


class Backend(ABC):
    """Minimal filesystem-like interface used by the copy engine.

    Every method takes a :class:`~carefulcopy.location.Location`. Missing paths
    must raise :class:`FileNotFoundError`.
    """

    #: True if reads/writes cross a network. Controls which timings are
    #: reported to a throttle.
    is_remote: bool = False

    # -- metadata ---------------------------------------------------------
    @abstractmethod
    def stat(self, loc: Location) -> FileInfo: ...

    def exists(self, loc: Location) -> bool:
        try:
            self.stat(loc)
            return True
        except FileNotFoundError:
            return False

    @abstractmethod
    def listdir(self, loc: Location) -> List[str]: ...

    @abstractmethod
    def set_mtime(self, loc: Location, mtime: float) -> None: ...

    # -- data -------------------------------------------------------------
    @abstractmethod
    def open_read(self, loc: Location) -> BinaryIO:
        """Open for binary reading. Must support ``seek``."""

    @abstractmethod
    def open_write(self, loc: Location, offset: int = 0) -> BinaryIO:
        """Open for binary writing.

        ``offset == 0``: create or truncate.
        ``offset > 0``: the file must exist; position at *offset* and discard
        anything after it, so a resumed transfer overwrites any torn tail.
        The handle must also be **readable** and seekable (the engine re-reads
        the tail of a partial file to verify it before resuming).
        """

    # -- namespace --------------------------------------------------------
    @abstractmethod
    def makedirs(self, loc: Location, exist_ok: bool = True) -> None: ...

    @abstractmethod
    def remove(self, loc: Location) -> None: ...

    @abstractmethod
    def rename(self, src: Location, dst: Location) -> None:
        """Move *src* to *dst*, atomically replacing *dst* if it exists."""

    # -- concurrency ------------------------------------------------------
    @abstractmethod
    def lock(self, loc: Location, owner: Optional[str] = None) -> AbstractContextManager:
        """Return a context manager holding an exclusive write lock for *loc*.

        Entering must raise :class:`~carefulcopy.errors.LockedError` immediately
        (not block) if another operation already holds it.

        *owner* is a token that stays the same across the retries of one copy.
        Backends whose locks can outlive a dropped connection (SMB) use it to
        recognise their own stale lock and take it over.
        """

    # -- connection lifetime ----------------------------------------------
    def session(self) -> ContextManager[Any]:
        """Bracket a group of operations that should share one connection.

        The engine wraps every copy attempt in it. Backends without
        connections (local disk) keep the default no-op. If an exception
        that :meth:`is_transient` leaves the ``with`` block, the connection
        must be treated as broken and dropped, not reused.
        """
        return nullcontext()

    # -- error handling ---------------------------------------------------
    def is_transient(self, exc: BaseException) -> bool:
        """Whether *exc* is worth retrying/resuming after."""
        return is_transient_error(exc)
