"""Exception hierarchy.

All copy-specific errors derive from :class:`CopyError`, which itself is an
:class:`OSError`, so existing ``except OSError`` handlers keep working.
"""
from __future__ import annotations


class CopyError(OSError):
    """Base class for errors raised by carefulcopy."""


class TransientError(CopyError):
    """A failure that is expected to go away on retry (e.g. a short read or a
    size mismatch after transfer). The engine treats it like a dropped
    connection: it keeps the partial file and resumes."""


class SourceChangedError(CopyError):
    """The source file was modified while it was being copied. Never retried."""


class LockedError(CopyError):
    """The destination is being written by another operation."""


class UnsupportedSchemeError(CopyError):
    """No backend is registered for the URI scheme."""


class AuthenticationError(CopyError):
    """The server rejected the credentials, or none could be obtained. Never retried."""
