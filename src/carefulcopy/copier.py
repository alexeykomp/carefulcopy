"""The copy engine: atomic, resumable, retrying single-file copy."""
from __future__ import annotations

import logging
import time
from contextlib import ExitStack
from typing import Any, Callable, Optional

from .abort import is_aborted, sleep_abortable
from .backends import Backend, get_backend
from .errors import CopyError, LockedError, SourceChangedError, TransientError
from .location import Location, PathOrLocation
from .options import CopyOptions
from .throttle import Throttle

logger = logging.getLogger("carefulcopy")

ProgressCallback = Callable[[str, int, int], None]
"""``progress(source, bytes_copied, bytes_total)`` - called from the copying thread."""

FailureCallback = Callable[[str, BaseException], bool]
"""``on_failure(source, error) -> retry?`` - consulted once retries are exhausted."""


class _State:
    """Per-call state that survives retries."""

    def __init__(self) -> None:
        self.fingerprint: Optional[tuple] = None


def copy_file(
    source: PathOrLocation,
    destination: PathOrLocation,
    *,
    progress: Optional[ProgressCallback] = None,
    abort: Any = None,
    options: Optional[CopyOptions] = None,
    throttle: Optional[Throttle] = None,
    on_failure: Optional[FailureCallback] = None,
) -> bool:
    """Copy one file.

    The data is written to ``<destination>.part.<size>-<mtime>`` and renamed
    over the destination only when complete and verified, so readers never see
    a half-written file. If the transfer is interrupted by a transient error
    (dropped connection, timeout, ...) it is resumed from the last byte
    written, up to ``options.retries`` times.

    Returns:
        ``True`` on success, ``False`` if *abort* fired (partial data is
        removed unless ``options.keep_partial_on_abort``).

    Raises:
        CopyError: retries exhausted (or the caller declined to retry),
            or the destination stayed locked past ``lock_wait_timeout``.
        SourceChangedError: the source was modified during the copy.
        FileExistsError: ``overwrite=False`` and the destination exists.
        OSError: any non-transient I/O error, unchanged.
    """
    opts = options or CopyOptions()
    src, dst = Location.parse(source), Location.parse(destination)
    sb, db = get_backend(src), get_backend(dst)

    if is_aborted(abort):
        return False

    state = _State()
    attempts_left = opts.retries
    lock_wait_started: Optional[float] = None

    while True:
        try:
            return _copy_once(src, dst, sb, db, opts, state, progress, abort, throttle)
        except LockedError as exc:
            now = time.monotonic()
            if lock_wait_started is None:
                lock_wait_started = now
            if now - lock_wait_started >= opts.lock_wait_timeout:
                raise CopyError(
                    f"{dst} stayed locked by another operation for "
                    f"{opts.lock_wait_timeout:g}s"
                ) from exc
            logger.info("%s is locked by another operation, waiting...", dst)
            if sleep_abortable(opts.lock_retry_interval, abort):
                return False
        except SourceChangedError:
            raise
        except Exception as exc:
            if not (sb.is_transient(exc) or db.is_transient(exc)):
                raise
            lock_wait_started = None
            attempts_left -= 1
            logger.warning("Transient error copying %s: %s (attempts left: %d)", src, exc, attempts_left)
            if attempts_left <= 0:
                if on_failure is not None and on_failure(str(src), exc):
                    attempts_left = opts.retries
                else:
                    raise CopyError(
                        f"Could not copy {src} to {dst} after {opts.retries} attempts: {exc}"
                    ) from exc
            if sleep_abortable(opts.retry_delay, abort):
                return False


def _copy_once(
    src: Location, dst: Location, sb: Backend, db: Backend, opts: CopyOptions,
    state: _State, progress: Optional[ProgressCallback], abort: Any,
    throttle: Optional[Throttle],
) -> bool:
    """One attempt. Both backends share one connection for its whole duration."""
    with ExitStack() as sessions:
        sessions.enter_context(sb.session())
        if db is not sb:
            sessions.enter_context(db.session())
        return _copy_attempt(src, dst, sb, db, opts, state, progress, abort, throttle)


def _copy_attempt(
    src: Location, dst: Location, sb: Backend, db: Backend, opts: CopyOptions,
    state: _State, progress: Optional[ProgressCallback], abort: Any,
    throttle: Optional[Throttle],
) -> bool:
    info = sb.stat(src)
    if info.is_dir:
        raise IsADirectoryError(f"Source is a directory: {src}")

    fingerprint = (info.size, int(info.mtime))
    if state.fingerprint is None:
        state.fingerprint = fingerprint
    elif state.fingerprint != fingerprint:
        raise SourceChangedError(f"{src} was modified during the copy")

    if not opts.overwrite and db.exists(dst):
        raise FileExistsError(f"Destination exists: {dst}")
    db.makedirs(dst.parent, exist_ok=True)

    direct = info.size < opts.direct_write_threshold
    part = dst.with_name(f"{dst.name}.part.{info.size}-{int(info.mtime)}")
    target = dst if direct else part

    with ExitStack() as held:
        if opts.use_locks:
            held.enter_context(db.lock(dst))
            _remove_stale_parts(db, dst, part)

        offset = 0 if direct else _resume_offset(db, target, info.size, opts)
        if offset:
            logger.info("Resuming %s from byte %d of %d", dst.name, offset, info.size)

        completed = _transfer(src, sb, dst, target, db, offset, info.size, opts, abort, progress, throttle)
        if not completed:
            _discard_partial(db, dst, part, direct, keep=opts.keep_partial_on_abort)
            return False

        after = sb.stat(src)
        if (after.size, int(after.mtime)) != fingerprint:
            _remove_quietly(db, target)
            raise SourceChangedError(f"{src} was modified during the copy")

        if opts.verify_size:
            written = db.stat(target).size
            if written != info.size:
                _remove_quietly(db, target)
                raise TransientError(
                    f"Size mismatch for {target}: expected {info.size}, got {written}"
                )

        if opts.preserve_mtime:
            try:
                db.set_mtime(target, info.mtime)
            except OSError as exc:  # not all servers allow it; never fatal
                logger.debug("Could not preserve mtime on %s: %s", target, exc)

        if not direct:
            db.rename(part, dst)  # last step: if it fails we resume instantly
    return True


def _transfer(
    src: Location, sb: Backend, dst: Location, target: Location, db: Backend,
    offset: int, total: int, opts: CopyOptions, abort: Any,
    progress: Optional[ProgressCallback], throttle: Optional[Throttle],
) -> bool:
    """Move bytes. Returns False if aborted, True once the source hit EOF."""
    src_str = str(src)
    copied = offset
    sample_bytes, sample_time = 0, 0.0
    timed = sb.is_remote or db.is_remote

    if progress:
        progress(src_str, copied, total)

    with ExitStack() as stack:
        # Entered first, so it is released last (after the files are closed).
        worker = stack.enter_context(throttle.worker(src, dst)) if throttle else None
        sf = stack.enter_context(sb.open_read(src))
        df = stack.enter_context(db.open_write(target, offset))
        if offset:
            sf.seek(offset)
        while True:
            if is_aborted(abort):
                return False

            t0 = time.monotonic()
            data = sf.read(opts.block_size)
            t1 = time.monotonic()
            if not data:
                break

            if worker:
                worker.acquire(len(data), abort)  # pace the stream; may sleep
                if is_aborted(abort):
                    return False

            t2 = time.monotonic()
            df.write(data)
            t3 = time.monotonic()

            copied += len(data)
            if progress:
                progress(src_str, copied, total)

            if worker and timed:
                # Only time spent on the wire counts, never time spent waiting in acquire().
                sample_bytes += len(data)
                sample_time += (t1 - t0 if sb.is_remote else 0.0) + (t3 - t2 if db.is_remote else 0.0)
                if sample_bytes >= opts.feedback_after:
                    worker.report(sample_bytes, sample_time)
                    sample_bytes, sample_time = 0, 0.0

    if copied > total:
        raise SourceChangedError(f"{src} grew during the copy")
    if copied < total:
        raise TransientError(f"Source ended early: got {copied} of {total} bytes from {src}")
    return True


def _resume_offset(db: Backend, part: Location, total: int, opts: CopyOptions) -> int:
    if not opts.resume:
        return 0
    try:
        st = db.stat(part)
    except FileNotFoundError:
        return 0
    if st.is_dir:
        raise IsADirectoryError(f"Expected a file: {part}")
    return st.size if 0 < st.size <= total else 0


def _remove_stale_parts(db: Backend, dst: Location, current: Location) -> None:
    """Delete ``<dst>.part.*`` files left by copies of a different source version."""
    prefix = dst.name + ".part."
    try:
        names = db.listdir(dst.parent)
    except FileNotFoundError:
        return
    for name in names:
        if name.startswith(prefix) and name != current.name:
            logger.info("Removing stale partial file %s", name)
            _remove_quietly(db, dst.with_name(name))


def _discard_partial(db: Backend, dst: Location, part: Location, direct: bool, keep: bool) -> None:
    if direct:
        _remove_quietly(db, dst)  # the destination was being written in place
    elif not keep:
        _remove_quietly(db, part)


def _remove_quietly(db: Backend, loc: Location) -> None:
    try:
        db.remove(loc)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove %s: %s", loc, exc)
