from __future__ import annotations

from dataclasses import dataclass

MiB = 1024 * 1024


@dataclass
class CopyOptions:
    """Tunables for :func:`carefulcopy.copy_file`."""

    block_size: int = 2 * MiB
    """Bytes read/written per iteration."""

    retries: int = 3
    """Attempts on transient errors before ``on_failure`` is consulted / the error is raised."""

    retry_delay: float = 5.0
    """Seconds to wait between attempts."""

    resume: bool = True
    """Continue from an existing ``.part`` file instead of starting over."""

    overwrite: bool = True
    """If False, raise ``FileExistsError`` when the destination exists."""

    use_locks: bool = True
    """Take an exclusive lock on the destination so two writers cannot collide.
    Stale ``.part`` files are only cleaned up while the lock is held."""

    lock_wait_timeout: float = 60.0
    """How long to wait for another writer before giving up (does not consume retries)."""

    lock_retry_interval: float = 2.0

    preserve_mtime: bool = True
    verify_size: bool = True

    direct_write_threshold: int = 0
    """Files smaller than this many bytes are written straight to the destination
    (no ``.part`` + rename). Saves round-trips on high-latency links at the cost of
    atomicity. ``0`` (default) disables the shortcut."""

    keep_partial_on_abort: bool = False
    """Keep the ``.part`` file when aborted so a later run can resume it."""

    feedback_after: int = 8 * MiB
    """Report a speed sample to the throttle after this many bytes."""

    def __post_init__(self) -> None:
        if self.block_size <= 0:
            raise ValueError("block_size must be positive")
        if self.retries < 1:
            raise ValueError("retries must be >= 1")
