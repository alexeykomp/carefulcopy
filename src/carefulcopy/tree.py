"""Copying directory trees and batches of files: :func:`copy_tree` and :func:`copy_many`.

How a run works
---------------
1. **Plan.** The source is walked with ``Backend.scandir`` (one round trip per directory,
   sizes included), the destination directories are listed too when ``if_exists`` needs it.
   This yields the list of files to copy, the directories to create and what was skipped,
   and so the **total** for the progress report.
2. **Copy.** Files are copied by :func:`carefulcopy.copy_file` - atomic, resumable, retrying -
   by ``workers`` threads that share one throttle and one connection pool.
3. **Report.** A :class:`CopyReport` says what was copied, skipped and what failed.

Re-running an interrupted copy with ``if_exists="update"`` copies only what is missing or
changed; partial files are resumed. That makes huge copies restartable.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

from .abort import is_aborted
from .backends import Backend, get_backend
from .backends.base import Entry
from .copier import FailureCallback, copy_file
from .errors import AuthenticationError, UnsupportedSchemeError
from .location import Location, PathOrLocation
from .options import CopyOptions
from .throttle import Throttle

logger = logging.getLogger("carefulcopy.tree")

_FATAL = (AuthenticationError, UnsupportedSchemeError)   # retrying other files cannot help
_MAX_CURRENT = 8

# Leftovers of carefulcopy itself, which must not be copied when a destination is later used as a source.
_PART_RE = re.compile(r"\.part\.\d+-\d+(-[0-9a-f]{8})?$")
_DEAD_LOCK_RE = re.compile(r"^(?P<base>.+)\.lock\.dead-[0-9a-f]{8}$")


# --------------------------------------------------------------------------
# Public types
# --------------------------------------------------------------------------
@dataclass
class TreeOptions:
    """How a tree/batch copy behaves (per-file behaviour is in :class:`CopyOptions`)."""

    workers: int = 1
    """Files copied in parallel. 1 = sequential, in the calling thread. Over a network 4-8 helps a
    lot with many small files; keep it <= the SMB pool size."""

    if_exists: str = "overwrite"
    """What to do when a destination file exists:
    ``"overwrite"`` replace it; ``"skip"`` leave it; ``"error"`` raise ``FileExistsError``;
    ``"update"`` skip it when size and mtime match (within ``mtime_tolerance``), else replace."""

    errors: str = "raise"
    """``"raise"``: stop at the first failed file and raise its error. ``"collect"``: carry on and
    list failures in the report. Authentication failures are always fatal."""

    follow_symlinks: bool = False
    """Copy what symbolic links point to. Default: skip them. Only for **local** sources: loops are
    detected there, whereas on a remote source they could not be, so it is refused (``ValueError``)."""

    exclude: Sequence[str] = ()
    """``fnmatch`` patterns. A pattern with ``/`` is matched against the path relative to the
    copied root, any other against the entry's name. Matching directories are not entered."""

    skip_sidecars: bool = True
    """Skip carefulcopy's own leftovers (``*.part.<n>-<n>-<hash>``, and ``x.lock`` /
    ``x.lock.dead-*`` next to an ``x``)."""

    max_depth: int = 100
    """Directories nested deeper than this are skipped (reason ``"too deep"``). A safety net against
    endless recursion, e.g. symbolic-link cycles on a remote source, where they cannot be detected."""

    mtime_tolerance: float = 2.0
    """Seconds of difference still considered "same time" for ``if_exists="update"``
    (FAT and some NAS firmware keep only 2 s resolution)."""

    progress_interval: float = 0.1
    """Minimum seconds between progress callbacks (the first and the last are always sent)."""

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        if self.max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        if self.if_exists not in ("overwrite", "skip", "update", "error"):
            raise ValueError("if_exists must be 'overwrite', 'skip', 'update' or 'error'")
        if self.errors not in ("raise", "collect"):
            raise ValueError("errors must be 'raise' or 'collect'")
        if isinstance(self.exclude, str):
            raise TypeError("exclude must be a sequence of patterns, not a single string")


@dataclass(frozen=True)
class TreeProgress:
    """Snapshot sent to the ``progress`` callback."""

    phase: str                      # "scanning" | "copying" | "done"
    bytes_copied: int
    bytes_total: int                # while scanning: found so far
    files_done: int
    files_total: int
    current: Tuple[str, ...] = ()   # files being copied right now (at most 8)

    @property
    def fraction(self) -> float:
        """Share of the bytes done, 0..1 (1.0 for an empty job that is finished)."""
        if self.bytes_total:
            return min(1.0, self.bytes_copied / self.bytes_total)
        return 1.0 if self.phase == "done" else 0.0


@dataclass(frozen=True)
class Failure:
    source: str
    error: BaseException


@dataclass(frozen=True)
class Skipped:
    source: str
    reason: str                     # "exists" | "unchanged" | "excluded" | "sidecar" | "symlink" | ...


@dataclass
class CopyReport:
    files_copied: int = 0
    bytes_copied: int = 0
    skipped: List[Skipped] = field(default_factory=list)
    failures: List[Failure] = field(default_factory=list)
    aborted: bool = False

    @property
    def files_skipped(self) -> int:
        return len(self.skipped)

    @property
    def ok(self) -> bool:
        """Everything planned was copied: not aborted and nothing failed."""
        return not self.aborted and not self.failures

    def __bool__(self) -> bool:
        return self.ok


TreeProgressCallback = Callable[[TreeProgress], None]


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
def copy_tree(
    source: PathOrLocation,
    destination: PathOrLocation,
    *,
    progress: Optional[TreeProgressCallback] = None,
    abort: Any = None,
    options: Optional[CopyOptions] = None,
    tree: Optional[TreeOptions] = None,
    throttle: Optional[Throttle] = None,
    on_failure: Optional[FailureCallback] = None,
) -> CopyReport:
    """Copy *source* (a directory, or a single file) to the path *destination*.

    Like ``shutil.copytree``, *destination* is the path of the copy itself (it is created),
    not the directory to copy into. Returns a :class:`CopyReport`; ``bool(report)`` is true
    when everything was copied. Aborting returns a report with ``aborted=True``.
    """
    src, dst = Location.parse(source), Location.parse(destination)
    return _run([(src, dst)], progress, abort, options, tree, throttle, on_failure)


def copy_many(
    sources: Sequence[PathOrLocation],
    destination_dir: PathOrLocation,
    *,
    progress: Optional[TreeProgressCallback] = None,
    abort: Any = None,
    options: Optional[CopyOptions] = None,
    tree: Optional[TreeOptions] = None,
    throttle: Optional[Throttle] = None,
    on_failure: Optional[FailureCallback] = None,
) -> CopyReport:
    """Copy several files and/or directories *into* the directory *destination_dir*.

    Each source keeps its own name; two sources with the same name are refused. Progress is
    aggregated over everything.
    """
    dest_dir = Location.parse(destination_dir)
    pairs: List[Tuple[Location, Location]] = []
    seen: Dict[str, str] = {}
    for item in sources:
        src = Location.parse(item)
        name = src.name
        if not name:
            raise ValueError(f"Cannot copy {src} by name (it has none, e.g. a root or share)")
        if name in seen:
            raise ValueError(f"Two sources would both become {name!r}: {seen[name]} and {src}")
        seen[name] = str(src)
        pairs.append((src, dest_dir / name))
    return _run(pairs, progress, abort, options, tree, throttle, on_failure)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
@dataclass
class _Item:
    index: int
    src: Location
    dst: Location
    size: int


@dataclass
class _Plan:
    dirs: List[Location] = field(default_factory=list)
    files: List[_Item] = field(default_factory=list)
    total_bytes: int = 0


def _same_endpoint(a: Location, b: Location) -> bool:
    return a.scheme == b.scheme and a.netloc.lower() == b.netloc.lower()


def _norm_parts(loc: Location) -> Tuple[str, ...]:
    if loc.is_local:
        return PurePath(os.path.normcase(os.path.realpath(str(loc.path)))).parts
    return tuple(p.casefold() for p in loc.path.parts)


def _is_inside_or_same(child: Location, parent: Location) -> bool:
    if not _same_endpoint(child, parent):
        return False
    c, p = _norm_parts(child), _norm_parts(parent)
    return c[:len(p)] == p


def _is_sidecar(name: str, siblings: FrozenSet[str]) -> bool:
    if _PART_RE.search(name):
        return True
    if name.endswith(".lock") and name[:-5] in siblings:
        return True
    m = _DEAD_LOCK_RE.match(name)
    return bool(m and m.group("base") in siblings)


class _Planner:
    def __init__(self, tree: TreeOptions, abort: Any, agg: "_Aggregator", failures: List[Failure],
                 skipped: List[Skipped]) -> None:
        self.tree, self.abort, self.agg = tree, abort, agg
        self.failures, self.skipped = failures, skipped
        self.plan = _Plan()
        self._existing_cache: Dict[str, Dict[str, Entry]] = {}

    # -- helpers ------------------------------------------------------------
    def _fail(self, source: Location, exc: Exception) -> None:
        if isinstance(exc, _FATAL) or self.tree.errors == "raise":
            raise exc
        self.failures.append(Failure(str(source), exc))

    def _existing(self, directory: Location) -> Dict[str, Entry]:
        """Entries already present in a destination directory (only asked for when needed)."""
        if self.tree.if_exists not in ("skip", "update"):
            return {}
        key = str(directory)
        if key not in self._existing_cache:
            try:
                entries = get_backend(directory).scandir(directory)
            except (FileNotFoundError, NotADirectoryError):
                entries = []
            self._existing_cache[key] = {e.name: e for e in entries}
        return self._existing_cache[key]

    def _existing_decision(self, name: str, size: int, mtime: float, existing: Dict[str, Entry]) -> Optional[str]:
        mode = self.tree.if_exists
        if mode not in ("skip", "update"):
            return None
        cur = existing.get(name)
        if cur is None or cur.is_dir:
            return None
        if mode == "skip":
            return "exists"
        if cur.size == size and abs(cur.mtime - mtime) <= self.tree.mtime_tolerance:
            return "unchanged"
        return None

    def _add_file(self, src: Location, dst: Location, size: int) -> None:
        self.plan.files.append(_Item(len(self.plan.files), src, dst, size))
        self.plan.total_bytes += size
        self.agg.scanning(len(self.plan.files), self.plan.total_bytes)

    def _excluded(self, name: str, rel: str) -> bool:
        for pattern in self.tree.exclude:
            target = rel if "/" in pattern else name
            if fnmatch.fnmatchcase(target, pattern):
                return True
        return False

    # -- walking --------------------------------------------------------------
    def add_root(self, src: Location, dst: Location) -> None:
        if self.tree.follow_symlinks and not src.is_local:
            raise ValueError("follow_symlinks is only supported for local sources "
                             f"(symlink loops cannot be detected on {src.scheme}://)")
        sb = get_backend(src)
        info = sb.stat(src)                     # a missing root is a usage error: always raised
        if not info.is_dir:
            self._add_root_file(src, dst, info.size, info.mtime)
            return
        if _is_inside_or_same(dst, src):
            raise ValueError(f"Cannot copy {src} into itself ({dst})")
        self._walk(sb, src, dst)

    def _add_root_file(self, src: Location, dst: Location, size: int, mtime: float) -> None:
        reason = self._existing_decision(dst.name, size, mtime, self._existing(dst.parent))
        if reason:
            self.skipped.append(Skipped(str(src), reason))
        else:
            self._add_file(src, dst, size)

    def _walk(self, sb: Backend, root_src: Location, root_dst: Location) -> None:
        follow_guard = self.tree.follow_symlinks and root_src.is_local
        start_anc: FrozenSet[str] = frozenset()
        if follow_guard:
            start_anc = frozenset([os.path.realpath(str(root_src.path))])
        stack: List[Tuple[Location, Location, str, FrozenSet[str]]] = [(root_src, root_dst, "", start_anc)]
        while stack:
            if is_aborted(self.abort):
                return
            src_dir, dst_dir, rel, ancestors = stack.pop()
            self.plan.dirs.append(dst_dir)
            try:
                entries = sb.scandir(src_dir)
            except Exception as exc:
                if isinstance(exc, _FATAL):
                    raise
                self._fail(src_dir, exc)
                continue
            names = frozenset(e.name for e in entries)
            existing = self._existing(dst_dir)
            subdirs: List[Tuple[Location, Location, str, FrozenSet[str]]] = []
            for e in sorted(entries, key=lambda x: x.name):
                child_rel = f"{rel}/{e.name}" if rel else e.name
                child_src = src_dir / e.name
                if self._excluded(e.name, child_rel):
                    self.skipped.append(Skipped(str(child_src), "excluded"))
                    continue
                if self.tree.skip_sidecars and _is_sidecar(e.name, names):
                    self.skipped.append(Skipped(str(child_src), "sidecar"))
                    continue
                if e.is_symlink and not self.tree.follow_symlinks:
                    self.skipped.append(Skipped(str(child_src), "symlink"))
                    continue
                if e.is_other:
                    self.skipped.append(Skipped(str(child_src), "not a regular file"))
                    continue
                if e.is_dir:
                    if child_rel.count("/") + 1 > self.tree.max_depth:
                        self.skipped.append(Skipped(str(child_src), "too deep"))
                        continue
                    child_anc = ancestors
                    if follow_guard:
                        real = os.path.realpath(str(child_src.path))
                        if real in ancestors:
                            self.skipped.append(Skipped(str(child_src), "symlink loop"))
                            continue
                        child_anc = ancestors | {real}
                    subdirs.append((child_src, dst_dir / e.name, child_rel, child_anc))
                    continue
                reason = self._existing_decision(e.name, e.size, e.mtime, existing)
                if reason:
                    self.skipped.append(Skipped(str(child_src), reason))
                else:
                    self._add_file(child_src, dst_dir / e.name, e.size)
            stack.extend(reversed(subdirs))     # depth-first, alphabetical


# --------------------------------------------------------------------------
# Aggregated progress
# --------------------------------------------------------------------------
class _Aggregator:
    """Combines the per-file progress of all workers into one stream of snapshots.

    Calls to the user's callback are serialised (never concurrent) and rate-limited. If the
    callback raises, the run is stopped and the exception is re-raised at the end.
    """

    def __init__(self, callback: Optional[TreeProgressCallback], interval: float) -> None:
        self._cb, self._interval = callback, interval
        self._lock = threading.Lock()
        self._phase = "scanning"
        self._files_total = self._bytes_total = 0
        self._files_done = self._bytes_done = 0
        self._inflight: Dict[int, Tuple[str, int]] = {}
        self._last: Optional[float] = None
        self.error: Optional[BaseException] = None
        self.on_error: Optional[Callable[[], None]] = None

    def _emit(self, force: bool = False) -> None:
        if self._cb is None or self.error is not None:
            return
        now = time.monotonic()
        if not force and self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        copied = self._bytes_done + sum(c for _, c in self._inflight.values())
        snapshot = TreeProgress(
            self._phase, copied, self._bytes_total, self._files_done, self._files_total,
            tuple(s for s, _ in list(self._inflight.values())[:_MAX_CURRENT]))
        try:
            self._cb(snapshot)
        except Exception as exc:
            self.error = exc
            if self.on_error:
                self.on_error()

    def scanning(self, files: int, bytes_: int) -> None:
        with self._lock:
            self._files_total, self._bytes_total = files, bytes_
            self._emit()

    def start_copying(self, files: int, bytes_: int) -> None:
        with self._lock:
            self._phase = "copying"
            self._files_total, self._bytes_total = files, bytes_
            self._emit(force=True)

    def progress(self, key: int, src: str, copied: int) -> None:
        with self._lock:
            self._inflight[key] = (src, copied)
            self._emit()

    def done(self, key: int, size: int) -> None:
        with self._lock:
            self._inflight.pop(key, None)
            self._bytes_done += size
            self._files_done += 1
            self._emit()

    def dropped(self, key: int) -> None:
        with self._lock:
            self._inflight.pop(key, None)

    def finish(self) -> None:
        with self._lock:
            self._phase = "done"
            self._emit(force=True)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
def _run(
    pairs: List[Tuple[Location, Location]],
    progress: Optional[TreeProgressCallback],
    abort: Any,
    options: Optional[CopyOptions],
    tree: Optional[TreeOptions],
    throttle: Optional[Throttle],
    on_failure: Optional[FailureCallback],
) -> CopyReport:
    tree = tree or TreeOptions()
    file_opts = dataclasses.replace(options or CopyOptions(), overwrite=(tree.if_exists != "error"))
    report = CopyReport()
    agg = _Aggregator(progress, tree.progress_interval)
    stop = threading.Event()
    agg.on_error = stop.set

    # ---- 1. plan ----
    if is_aborted(abort):
        report.aborted = True
        return report
    planner = _Planner(tree, abort, agg, report.failures, report.skipped)
    for src, dst in pairs:
        planner.add_root(src, dst)
        if is_aborted(abort):
            report.aborted = True
            agg.finish()
            return report
    plan = planner.plan
    agg.start_copying(len(plan.files), plan.total_bytes)

    def combined_abort() -> bool:
        return stop.is_set() or is_aborted(abort)

    # ---- 2. directories (so that empty ones exist too) ----
    for directory in plan.dirs:
        if combined_abort():
            break
        try:
            get_backend(directory).makedirs(directory, exist_ok=True)
        except Exception as exc:
            if isinstance(exc, _FATAL) or tree.errors == "raise":
                raise
            report.failures.append(Failure(str(directory), exc))

    # ---- 3. files ----
    lock = threading.Lock()
    first_error: List[BaseException] = []
    fatal_error: List[BaseException] = []

    def work(item: _Item) -> None:
        if combined_abort():
            return
        try:
            ok = copy_file(
                item.src, item.dst,
                progress=lambda s, c, t, _k=item.index: agg.progress(_k, s, c),
                abort=combined_abort, options=file_opts, throttle=throttle, on_failure=on_failure)
        except Exception as exc:
            agg.dropped(item.index)
            with lock:
                report.failures.append(Failure(str(item.src), exc))
                if isinstance(exc, _FATAL):
                    fatal_error.append(exc)
                    stop.set()
                elif tree.errors == "raise":
                    first_error.append(exc)
                    stop.set()
            return
        if ok:
            with lock:
                report.files_copied += 1
                report.bytes_copied += item.size
            agg.done(item.index, item.size)
        else:
            agg.dropped(item.index)

    if tree.workers == 1 or len(plan.files) <= 1:
        for item in plan.files:
            if combined_abort():
                break
            work(item)
    else:
        pool = ThreadPoolExecutor(max_workers=tree.workers, thread_name_prefix="carefulcopy-tree")
        try:
            for fut in [pool.submit(work, item) for item in plan.files]:
                fut.result()
        except BaseException:
            stop.set()                                   # e.g. KeyboardInterrupt in the caller
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    # ---- 4. report ----
    agg.finish()
    if fatal_error:
        raise fatal_error[0]
    if first_error:
        raise first_error[0]
    if agg.error is not None:
        raise agg.error
    report.aborted = is_aborted(abort) and report.files_copied < len(plan.files)
    return report
