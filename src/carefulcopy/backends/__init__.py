"""Backend registry: maps a Location's scheme to a Backend instance."""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from ..errors import UnsupportedSchemeError
from ..location import Location
from .base import Backend, FileInfo
from .local import LocalBackend

BackendFactory = Callable[[Location], Backend]

_local = LocalBackend()
_factories: Dict[str, BackendFactory] = {}
_smb_lock = threading.Lock()
_smb_backend: Optional[Backend] = None


def register_backend(scheme: str, factory: BackendFactory) -> None:
    """Register *factory* for URIs with the given *scheme*.

    The factory receives the Location being resolved (so it can pick a host)
    and should return a (preferably cached) :class:`Backend`.
    """
    _factories[scheme.lower()] = factory


def unregister_backend(scheme: str) -> None:
    _factories.pop(scheme.lower(), None)


def get_backend(loc: Location) -> Backend:
    if loc.is_local:
        return _local
    try:
        factory = _factories[loc.scheme]
    except KeyError:
        raise UnsupportedSchemeError(
            f"No backend registered for scheme {loc.scheme!r} (location: {loc})"
        ) from None
    return factory(loc)


# -- SMB ------------------------------------------------------------------
def configure_smb(credentials: Any = None, **options: Any) -> Backend:
    """Create the shared SMB backend and register it for ``smb://``.

    Call this once at start-up to choose credentials and pool settings; if you
    never do, a default backend (credentials from the environment, or ambient
    authentication) is created on first use. Calling it again replaces - and
    closes - the previous backend. ``options`` are passed to
    :class:`carefulcopy.backends.smb.SMBBackend`.
    """
    global _smb_backend
    from .smb import SMBBackend  # imports fine without smbprotocol; __init__ raises ImportError

    backend = SMBBackend(credentials, **options)
    with _smb_lock:
        old, _smb_backend = _smb_backend, backend
        register_backend("smb", lambda loc: backend)
    if old is not None:
        old.close()  # type: ignore[attr-defined]
    return backend


def _default_smb(loc: Location) -> Backend:
    with _smb_lock:
        existing = _smb_backend
    if existing is not None:
        return existing
    try:
        return configure_smb()
    except ImportError as exc:
        raise UnsupportedSchemeError(
            "smb:// locations need the optional SMB support: pip install 'carefulcopy[smb]'"
        ) from exc


def reset_backends() -> None:
    """Forget every registration (closing the SMB backend) and restore the defaults."""
    global _smb_backend
    with _smb_lock:
        old, _smb_backend = _smb_backend, None
        _factories.clear()
        _factories["smb"] = _default_smb
    if old is not None:
        old.close()  # type: ignore[attr-defined]


reset_backends()

__all__ = [
    "Backend", "FileInfo", "LocalBackend",
    "register_backend", "unregister_backend", "get_backend", "configure_smb", "reset_backends",
]
